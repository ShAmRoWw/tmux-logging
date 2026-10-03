#!/usr/bin/env python3
"""
VT100 screen-emulator filter for tmux-logging.

Maintains a fixed-size screen buffer (ROWS x COLS) that mirrors what the
real terminal displays. Lines are recorded when they scroll off the top,
before a full main-screen clear, and when recording stops. Partial edits
keep only their final visible state:

  Tab-completion menus  — drawn below the prompt, then erased in-place
                          with cursor-up + ESC[J. Partial erases do not
                          commit them to the log.

  Full-screen apps      — nano/vim/less use the alternate screen buffer.
      (nano, vim, …)      Content written there never reaches the main
                          buffer and is discarded on exit.

  Backspace / auto-     — characters are overwritten in-place inside
    suggestions           the current line of the buffer.  Only the final
                          state scrolls off.

  Progress bars (\\r)    — carriage-return moves the cursor to column 0;
                          subsequent writes overwrite the same buffer row.

Usage:  cat raw_pty_bytes | python3 logging_filter.py [COLS ROWS] >> log
        COLS and ROWS default to 80 and 24.
"""

import array
import codecs
import ctypes
import fcntl
import functools
import os
import re
import select
import signal
import stat
import sys
import termios
import unicodedata

# ── Regex table for escape-sequence lexing ────────────────────────────────
_CSI  = re.compile(r'\x1b\[([0-9;?]*)([A-Za-z@`])')
# Stop at the first terminator; later OSCs may surround visible link text.
_OSC  = re.compile(r'\x1b\][^\x07]*?(?:\x07|\x1b\\)')
_ESC3 = re.compile(r'\x1b[()][0-9A-Za-z]')   # charset designation
_ESC2 = re.compile(r'\x1b[^[\]78()]')          # generic 2-char (excl. 7/8)

# Match tmux's input parser limits; malformed/oversized commands are ignored
# as a whole, never truncated into a different valid terminal operation.
MAX_CSI_PARAMETER_BYTES = 63
MAX_CSI_PARAMS = 23
MAX_CSI_VALUE = 2147483647
MAX_CSI_INTERMEDIATE_BYTES = 3
MAX_CSI_LENGTH = 2 + MAX_CSI_PARAMETER_BYTES + MAX_CSI_INTERMEDIATE_BYTES + 1


# ── Helpers ───────────────────────────────────────────────────────────────
# tmux uses the host wcwidth table, with overrides for emoji and regional
# indicators. Keep these values independent of the Python Unicode version.
try:
    _wcwidth = ctypes.CDLL(None).wcwidth
    _wcwidth.argtypes = [ctypes.c_wchar]
    _wcwidth.restype = ctypes.c_int
except (AttributeError, OSError):
    _wcwidth = None

_WIDE_OVERRIDES = frozenset(value for start, end in (
    (0x261D, 0x261D), (0x26F9, 0x26F9), (0x270A, 0x270D),
    (0x1F385, 0x1F385), (0x1F3C2, 0x1F3C4), (0x1F3C7, 0x1F3C7),
    (0x1F3CA, 0x1F3CC), (0x1F3FB, 0x1F3FF), (0x1F442, 0x1F443),
    (0x1F446, 0x1F450), (0x1F466, 0x1F469), (0x1F46B, 0x1F46E),
    (0x1F470, 0x1F478), (0x1F47C, 0x1F47C), (0x1F481, 0x1F483),
    (0x1F485, 0x1F487), (0x1F48F, 0x1F48F), (0x1F491, 0x1F491),
    (0x1F4AA, 0x1F4AA), (0x1F574, 0x1F575), (0x1F57A, 0x1F57A),
    (0x1F590, 0x1F590), (0x1F595, 0x1F596), (0x1F645, 0x1F647),
    (0x1F64B, 0x1F64F), (0x1F6A3, 0x1F6A3), (0x1F6B4, 0x1F6B6),
    (0x1F6C0, 0x1F6C0), (0x1F6CC, 0x1F6CC), (0x1F90C, 0x1F90C),
    (0x1F90F, 0x1F90F), (0x1F918, 0x1F91F), (0x1F926, 0x1F926),
    (0x1F930, 0x1F939), (0x1F93D, 0x1F93E), (0x1F977, 0x1F977),
    (0x1F9B5, 0x1F9B6), (0x1F9B8, 0x1F9B9), (0x1F9BB, 0x1F9BB),
    (0x1F9CD, 0x1F9CF), (0x1F9D1, 0x1F9DD), (0x1FAC3, 0x1FAC5),
    (0x1FAF0, 0x1FAF8),
) for value in range(start, end + 1))
_MODIFIER_BASES = frozenset(value for start, end in (
    (0x1F44B, 0x1F450), (0x1F466, 0x1F469), (0x1F46E, 0x1F46E),
    (0x1F470, 0x1F478), (0x1F47C, 0x1F47C), (0x1F481, 0x1F483),
    (0x1F485, 0x1F487), (0x1F4AA, 0x1F4AA), (0x1F575, 0x1F575),
    (0x1F57A, 0x1F57A), (0x1F590, 0x1F590), (0x1F595, 0x1F596),
    (0x1F645, 0x1F647), (0x1F64B, 0x1F64F), (0x1F6B4, 0x1F6B6),
    (0x1F926, 0x1F926), (0x1F937, 0x1F939), (0x1F93D, 0x1F93E),
    (0x1F9B5, 0x1F9B6), (0x1F9B8, 0x1F9B9), (0x1F9CD, 0x1F9CF),
    (0x1F9D1, 0x1F9DF),
) for value in range(start, end + 1))


@functools.lru_cache(maxsize=4096)
def character_width(ch):
    """Display columns for one code point, using tmux's default width rules."""
    value = ord(ch)
    if value < 0x20 or 0x7f <= value <= 0x9f or value == 0x3164:
        return 0
    if 0x20 <= value < 0x7f or 0x1F1E6 <= value <= 0x1F1FF:
        return 1
    if value in _WIDE_OVERRIDES:
        return 2
    if _wcwidth is not None:
        width = _wcwidth(ch)
        return width if 0 <= width <= 2 else 1
    # Platforms without libc wcwidth still get ordinary wide/combining text.
    if unicodedata.category(ch) in ('Mn', 'Me', 'Cf'):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def cluster_width(cluster):
    """Width of a previously assembled terminal cell."""
    if not cluster:
        return 0
    if '\ufe0f' in cluster:
        return 2
    if (0x1F1E6 <= ord(cluster[0]) <= 0x1F1FF and
            sum(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in cluster) == 2):
        return 2
    return character_width(cluster[0])


def combine_character(cluster, ch):
    """Return (text, columns) for a combined cell, or None for a new cell.

    This follows tmux's default VS16, ZWJ, regional indicator and supported
    emoji modifier handling. A terminal cell holds at most 32 UTF-8 bytes.
    """
    if not cluster or ord(ch) < 0x80:
        return None
    width = character_width(ch)
    if ch == '\u3164':
        return cluster, cluster_width(cluster)
    value, base = ord(ch), ord(cluster[0])
    regional_pair = (0x1F1E6 <= value <= 0x1F1FF and
                     0x1F1E6 <= base <= 0x1F1FF and
                     sum(0x1F1E6 <= ord(c) <= 0x1F1FF for c in cluster) == 1)
    modifier_pair = ((0x1F3FB <= value <= 0x1F3FF and base in _MODIFIER_BASES)
                     or (0x1F3FB <= base <= 0x1F3FF and value in _MODIFIER_BASES))
    if width and not (cluster.endswith('\u200d') or regional_pair or modifier_pair):
        return None
    if len((cluster + ch).encode('utf-8')) > 32:
        return (cluster, cluster_width(cluster)) if not width else None
    combined_width = (2 if ch == '\ufe0f' or regional_pair or modifier_pair
                      else cluster_width(cluster))
    return cluster + ch, combined_width


def _param(parts, idx=0, default=1):
    try:
        v = parts[idx]
        return v if v != 0 else default
    except IndexError:
        return default


def _parse_params(raw):
    private = raw.startswith('?')
    if len(raw) - private > MAX_CSI_PARAMETER_BYTES:
        return None
    s = raw[1:] if private else raw
    if not s:
        return []
    parts = s.split(';')
    if len(parts) > MAX_CSI_PARAMS:
        return None
    values = []
    maximum = str(MAX_CSI_VALUE)
    for part in parts:
        if not part:
            values.append(0)
            continue
        if not part.isascii() or not part.isdecimal():
            return None
        digits = part.lstrip('0') or '0'
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            return None
        values.append(int(digits))
    return values


MAX_DIMENSION = 10000
MAX_SCREEN_CELLS = 1000000


class ScreenSizeError(RuntimeError):
    """An unsupported screen size was rejected before changing the screen."""


def validate_dimensions(cols, rows):
    """Validate geometry without converting arbitrarily long decimal input."""
    dimensions = []
    for value in (cols, rows):
        if isinstance(value, str):
            if not (0 < len(value) <= 16 and value.isascii() and value.isdigit()):
                raise ScreenSizeError('screen dimensions must be decimal integers from 1 to 10000')
            value = int(value)
        if type(value) is not int or not 1 <= value <= MAX_DIMENSION:
            raise ScreenSizeError('screen dimensions must be decimal integers from 1 to 10000')
        dimensions.append(value)
    if dimensions[0] * dimensions[1] > MAX_SCREEN_CELLS:
        raise ScreenSizeError('screen size exceeds 1000000 cells')
    return tuple(dimensions)


def _validate_snapshot_dimensions(cols, rows, alternate):
    """Validate saved screens before load_snapshot changes any live state."""
    result = cols, rows = validate_dimensions(cols, rows)
    visited = set()
    while alternate is not None:
        if id(alternate) in visited:
            raise ScreenSizeError('recursive alternate screen metadata')
        visited.add(id(alternate))
        cols, rows = validate_dimensions(
            cols if alternate.get('cols') is None else alternate['cols'],
            rows if alternate.get('rows') is None else alternate['rows'])
        alternate = alternate.get('alternate')
    return result


# ── Screen emulator ──────────────────────────────────────────────────────
class Screen:
    """VT100 screen with resize/reflow and scroll/full-clear logging."""

    class _Row:
        """Keep emission provenance with cells as rows split and join."""

        def __init__(self, text='', wrapped=False, seen=False):
            self.cells = []
            lead = None
            for ch in text:
                if ch == '\u3164':
                    continue
                combined = (combine_character(self.cells[lead], ch)
                            if lead is not None else None)
                if combined is not None:
                    cluster, width = combined
                    self.cells[lead:] = [cluster] + [None] * (width - 1)
                else:
                    width = character_width(ch)
                    if not width:
                        continue
                    lead = len(self.cells)
                    self.cells.extend([ch] + [None] * (width - 1))
            self.wrapped = bool(wrapped)
            self.seen = bytearray([bool(seen)]) * self.width
            self.blank_seen = bool(seen)

        @property
        def text(self):
            return ''.join(ch for ch in self.cells if ch is not None)

        @property
        def width(self):
            return len(self.cells)

        def __eq__(self, other):
            return isinstance(other, type(self)) and vars(self) == vars(other)

        def piece(self, start, end, wrapped):
            row = type(self)('', wrapped)
            row.cells = self.cells[start:end]
            row.seen = self.seen[start:end]
            row.blank_seen = self.blank_seen
            return row

        def pending(self):
            if not self.cells:
                return ('', not self.blank_seen)
            return (''.join(ch for ch, seen in zip(self.cells, self.seen)
                            if ch is not None and not seen), 0 in self.seen)

    def __init__(self, cols, rows, out):
        self.COLS, self.ROWS = validate_dimensions(cols, rows)
        self.out = out
        self._lines = [self._Row() for _ in range(self.ROWS)]
        self._history = []
        self._history_limit = 2000
        self._history_scrolled = 0
        self._scroll_on_clear = False
        self._full_clear_count = 0
        self._r = self._c = 0
        self._scroll_top = 0
        self._scroll_bot = self.ROWS - 1
        self._saved = None
        self._in_alt = False
        self._alt_state = None
        self._autowrap = True
        self._origin = False
        self._insert = False
        # Preserve the standalone filter's historical bare-LF interface.
        # A tmux bootstrap supplies the actual terminal mode explicitly.
        self._newline_mode = True
        self._tabs = set(range(8, self.COLS, 8))

    @property
    def _buf(self):
        return [row.text for row in self._lines]

    @property
    def _wrapped(self):
        return [row.wrapped for row in self._lines]

    def _state(self):
        return {key: value for key, value in vars(self).items()
                if key not in ('out', '_in_alt', '_alt_state', '_full_clear_count')}

    def load_snapshot(self, lines, *, cols=None, rows=None, cursor=(0, 0),
                      wrapped=None, history=(), history_wrapped=None,
                      history_limit=2000, history_scrolled=None,
                      scroll_region=None, autowrap=True, alternate=None,
                      origin=False, insert=False, tabs=None, newline_mode=False,
                      scroll_on_clear=False):
        """Bootstrap captured cells without replaying them as fresh output.

        Coordinates and scrolling margins are zero-based; cursor is (x, y).
        ``alternate`` is a dictionary describing the saved main screen with
        the same arguments, including its old dimensions if known. Historical
        rows seed reflow but are already recorded and are never dumped on start.
        """
        self.COLS, self.ROWS = _validate_snapshot_dimensions(
            self.COLS if cols is None else cols,
            self.ROWS if rows is None else rows, alternate)
        wrapped = wrapped or ()
        self._lines = [self._Row(text, wrapped[i] if i < len(wrapped) else False)
                       for i, text in enumerate(lines[:self.ROWS])]
        self._lines.extend(self._Row() for _ in range(self.ROWS - len(self._lines)))
        history_wrapped = history_wrapped or ()
        self._history_limit = max(0, history_limit)
        self._scroll_on_clear = bool(scroll_on_clear)
        self._history = [self._Row(text,
                        history_wrapped[i] if i < len(history_wrapped) else False,
                        seen=True) for i, text in enumerate(history)]
        # A tmux reflow can temporarily make history larger than its limit.
        # Preserve that authoritative snapshot until normal history collection;
        # truncating it here would change subsequent cursor and height reflow.
        self._history_scrolled = min(len(self._history), max(0,
            len(self._history) if history_scrolled is None else history_scrolled))
        self._c = max(0, min(self.COLS, cursor[0]))
        self._r = max(0, min(self.ROWS - 1, cursor[1]))
        self._scroll_top, self._scroll_bot = scroll_region or (0, self.ROWS - 1)
        self._scroll_top = max(0, min(self.ROWS - 1, self._scroll_top))
        self._scroll_bot = max(self._scroll_top, min(self.ROWS - 1, self._scroll_bot))
        self._autowrap = bool(autowrap)
        self._origin = bool(origin)
        self._insert = bool(insert)
        self._newline_mode = bool(newline_mode)
        self._tabs = set(range(8, self.COLS, 8)) if tabs is None else set(tabs)
        self._saved = None
        self._in_alt = alternate is not None
        self._alt_state = None
        if alternate is not None:
            saved = Screen(self.COLS, self.ROWS, self.out)
            arguments = dict(alternate)
            arguments.setdefault('history', history)
            arguments.setdefault('history_wrapped', history_wrapped)
            arguments.setdefault('history_limit', history_limit)
            arguments.setdefault('history_scrolled', history_scrolled)
            arguments.setdefault('scroll_on_clear', scroll_on_clear)
            saved.load_snapshot(**arguments)
            self._alt_state = saved._state()
            self._history = []
            self._history_scrolled = 0

    def _emit(self, row):
        text, pending = row.pending()
        if pending:
            self.out.write(text.rstrip() + '\n')
            row.seen[:] = b'\1' * row.width
            row.blank_seen = True

    def _collect_history(self):
        # tmux collects the oldest tenth at the next normal scroll, rather
        # than throwing away a prefix during reflow itself.
        if self._history and len(self._history) >= self._history_limit:
            count = max(1, self._history_limit // 10)
            del self._history[:count]
            self._history_scrolled = min(self._history_scrolled, len(self._history))

    def _scroll_up(self):
        top, bot = self._scroll_top, self._scroll_bot
        line = self._lines.pop(top)
        if not self._in_alt and top == 0:
            self._emit(line)
            self._collect_history()
            if self._history_limit:
                self._history.append(line)
                self._history_scrolled += 1
        self._lines.insert(bot, self._Row())

    def _scroll_down(self):
        self._lines.pop(self._scroll_bot)
        self._lines.insert(self._scroll_top, self._Row())

    def reverse_index(self):
        if self._r == self._scroll_top:
            self._scroll_down()
        else:
            self._r = max(0, self._r - 1)

    def index_down(self):
        if self._r == self._scroll_bot:
            self._scroll_up()
        else:
            self._r = min(self.ROWS - 1, self._r + 1)

    @staticmethod
    def _replace_row_cells(row, cells, seen=None):
        """Keep unchanged provenance, but record edits to a previously logged row."""
        if cells == row.cells:
            return
        if any(row.seen):
            seen = bytearray(len(cells))
        row.cells = cells
        row.seen = bytearray(len(cells)) if seen is None else bytearray(seen)
        row.blank_seen = False

    def _put_cell(self, row, column, text, width):
        cells = row.cells[:]
        seen = row.seen[:]
        needed = column + width
        cells.extend(' ' for _ in range(max(0, needed - len(cells))))
        seen.extend(b'\0' * (len(cells) - len(seen)))
        # Overwriting either half clears the rest of that wide glyph. Do not
        # repair unrelated raw cells: tmux's ICH/DCH can leave detached halves.
        start, end = column, needed
        while start > 0 and cells[start] is None:
            start -= 1
        while end < len(cells) and cells[end] is None:
            end += 1
        cells[start:end] = [' '] * (end - start)
        cells[column:needed] = [text] + [None] * (width - 1)
        revised = any(flag and (i >= len(cells) or row.cells[i] != cells[i])
                      for i, flag in enumerate(row.seen))
        if revised:
            seen = bytearray(len(cells))
        else:
            for i in range(column, needed):
                if i >= row.width or row.cells[i] != cells[i]:
                    seen[i] = 0
        row.cells, row.seen = cells, seen
        row.blank_seen = False

    def write_char(self, ch):
        if ch == '\u3164':  # tmux ignores HANGUL FILLER.
            return
        row = self._lines[self._r]
        if self._c > 0:
            previous = self._c - 1
            while previous > 0 and previous < row.width and row.cells[previous] is None:
                previous -= 1
            prior = row.cells[previous] if previous < row.width else ' '
            old_width = self._c - previous
            # A combining character attaches only after an entire glyph,
            # never to a lead whose continuation lies at the cursor itself.
            combined = (combine_character(prior, ch)
                        if prior is not None and cluster_width(prior) == old_width else None)
            if combined is not None:
                text, width = combined
                self._put_cell(row, previous, text, width)
                if previous + width <= self.COLS:
                    self._c += width - old_width
                return
        width = character_width(ch)
        if width <= 0 or width > self.COLS:
            return
        if self._c + width > self.COLS:
            if self._autowrap:
                row.wrapped = True
                self._c = 0
                self.index_down()
            else:
                return
        if self._insert:
            self.insert_chars(width)
        self._put_cell(self._lines[self._r], self._c, ch, width)
        self._c = min(self._c + width, self.COLS if self._autowrap else self.COLS - 1)

    def newline(self):
        self.index_down()
        if self._newline_mode:
            self._c = 0

    def carriage_return(self): self._c = 0
    def backspace(self):
        if self._c:
            self._c -= 1
        elif self._r and self._lines[self._r - 1].wrapped:
            self._r -= 1
            self._c = self.COLS - 1
    def tab(self):
        self._c = min((stop for stop in self._tabs if stop > self._c),
                      default=self.COLS - 1)

    def cursor_up(self, n):
        top = 0 if self._r < self._scroll_top else self._scroll_top
        self._r = max(top, self._r - n)
        self._c = min(self._c, self.COLS - 1)
    def cursor_down(self, n):
        bottom = self.ROWS - 1 if self._r > self._scroll_bot else self._scroll_bot
        self._r = min(bottom, self._r + n)
        self._c = min(self._c, self.COLS - 1)
    def cursor_right(self, n): self._c = min(self.COLS - 1, self._c + n)
    def cursor_left(self, n): self._c = max(0, self._c - n)
    def cursor_col(self, n1): self._c = max(0, min(self.COLS - 1, n1 - 1))
    def cursor_pos(self, r1, c1):
        top, bot = (self._scroll_top, self._scroll_bot) if self._origin else (0, self.ROWS - 1)
        self._r = max(top, min(bot, top + r1 - 1))
        self._c = max(0, min(self.COLS - 1, c1 - 1))

    def save_cursor(self): self._saved = (self._r, self._c)
    def restore_cursor(self):
        if self._saved is not None:
            self._r = min(self.ROWS - 1, self._saved[0])
            self._c = min(self.COLS, self._saved[1])

    def erase_line(self, mode):
        row = self._lines[self._r]
        if (mode == 2 or (mode == 0 and self._c == 0) or
                (mode == 1 and self._c >= self.COLS - 1)):
            self._lines[self._r] = self._Row()
            if self._r:
                self._lines[self._r - 1].wrapped = False
            return
        elif mode == 0:
            cells = row.cells[:self._c] + [' '] * max(0, row.width - self._c)
        elif mode == 1:
            end = min(row.width, self._c + 1)
            cells = [' '] * end + row.cells[end:]
        else:
            return
        self._replace_row_cells(row, cells, row.seen[:len(cells)])

    def insert_chars(self, count):
        count = min(max(1, count), self.COLS - self._c)
        if count <= 0:
            return
        # tmux's grid move has no source cells at this boundary and is a
        # no-op, except that insertion in the last column clears that cell.
        if count == self.COLS - self._c and self._c < self.COLS - 1:
            return
        row = self._lines[self._r]
        cells = row.cells[:self.COLS] + [' '] * max(0, self.COLS - row.width)
        moved = self.COLS - self._c - count
        if moved:
            cells[self._c + count:] = cells[self._c:self._c + moved]
        clear = min(count, moved) if moved else 1
        cells[self._c:self._c + clear] = [' '] * clear
        self._replace_row_cells(row, cells)

    def delete_chars(self, count):
        count = min(max(1, count), self.COLS - self._c)
        if count <= 0:
            return
        row = self._lines[self._r]
        moved = self.COLS - self._c - count
        used = max(row.width, self.COLS - count) if moved else row.width
        cells = row.cells[:] + [' '] * max(0, self.COLS - row.width)
        cells[self._c:self._c + moved] = cells[self._c + count:self.COLS]
        cells[self.COLS - count:self.COLS] = [' '] * count
        self._replace_row_cells(row, cells[:used])

    def erase_chars(self, count):
        row = self._lines[self._r]
        end = min(row.width, self.COLS, self._c + max(1, count))
        if end <= self._c:
            return
        cells = row.cells[:]
        cells[self._c:end] = [' '] * (end - self._c)
        self._replace_row_cells(row, cells)

    def edit_lines(self, count, insert=False):
        in_region = self._scroll_top <= self._r <= self._scroll_bot
        bottom = self._scroll_bot if in_region else self.ROWS - 1
        count = min(max(1, count), bottom - self._r + 1)
        if insert and not in_region and count == bottom - self._r + 1:
            return
        blank = [self._Row() for _ in range(count)]
        if insert:
            if in_region:
                self._lines[self._r:bottom + 1] = blank + self._lines[self._r:bottom + 1 - count]
            else:
                moved = bottom - self._r + 1 - count
                self._lines[self._r + count:bottom + 1] = self._lines[self._r:self._r + moved]
                cleared = min(count, moved)
                self._lines[self._r:self._r + cleared] = blank[:cleared]
        else:
            self._lines[self._r:bottom + 1] = self._lines[self._r + count:bottom + 1] + blank
        if self._r:
            self._lines[self._r - 1].wrapped = False
        self._lines[bottom].wrapped = False

    def erase_display(self, mode):
        full_clear = (mode == 2 or
                      (mode == 0 and (self._r, self._c) == (0, 0)) or
                      (mode == 1 and self._r == self.ROWS - 1
                       and self._c >= self.COLS - 1))
        if full_clear:
            if not self._in_alt:
                self._full_clear_count += 1
                # Commit only pending visible text, without terminal padding.
                # Mark even skipped blank rows: tmux may move them to history,
                # where a later resize must not emit them as fresh output.
                last = len(self._lines) - 1
                while last >= 0 and not self._lines[last].pending()[0].rstrip():
                    last -= 1
                for row in self._lines[:last + 1]:
                    self._emit(row)
                for row in self._lines[last + 1:]:
                    row.seen[:] = b'\1' * row.width
                    row.blank_seen = True
                if self._scroll_on_clear and mode != 1:
                    used = len(self._lines)
                    while used and not self._lines[used - 1].text:
                        used -= 1
                    for row in self._lines[:used]:
                        self._collect_history()
                        if self._history_limit:
                            self._history.append(row)
                    if used:
                        self._history_scrolled = 0
            self._lines = [self._Row() for _ in range(self.ROWS)]
        elif mode == 0:
            self.erase_line(0)
            self._lines[self._r + 1:] = [self._Row() for _ in range(self.ROWS - self._r - 1)]
        elif mode == 1:
            self._lines[:self._r] = [self._Row() for _ in range(self._r)]
            self.erase_line(1)
        elif mode == 3:
            # ED3 erases scrollback only; it must not erase the visible pane.
            self._history_scrolled = 0
            self._history = []

    def set_scroll_region(self, top1, bot1):
        top = max(0, top1 - 1)
        bot = min(self.ROWS - 1, (bot1 or self.ROWS) - 1)
        if top >= bot:
            return
        self._scroll_top, self._scroll_bot = top, bot
        self._r = self._c = 0

    def enter_alt_screen(self):
        if self._in_alt:
            return
        self._alt_state = self._state()
        self._in_alt = True
        self._lines = [self._Row() for _ in range(self.ROWS)]
        self._history = []
        self._history_scrolled = 0
        self._r = self._c = 0
        self._saved = None
        self._scroll_top = 0
        self._scroll_bot = self.ROWS - 1

    def leave_alt_screen(self):
        if not self._in_alt:
            return
        cols, rows = self.COLS, self.ROWS
        vars(self).update(self._alt_state)
        self._in_alt = False
        self._alt_state = None
        self.resize(cols, rows)
        self._c = min(self._c, self.COLS - 1)

    @staticmethod
    def _logical_cursor(lines, cx, cy):
        x = y = 0
        for row in lines[:cy]:
            if row.wrapped:
                x += row.width
            else:
                x = 0
                y += 1
        return (None if cx >= lines[cy].width else x + cx, y)

    @staticmethod
    def _physical_cursor(lines, x, y):
        row = logical_y = 0
        while row < len(lines) - 1 and logical_y != y:
            if not lines[row].wrapped:
                logical_y += 1
            row += 1
        if x is None:
            while row < len(lines) - 1 and lines[row].wrapped:
                row += 1
            return lines[row].width, row
        while row < len(lines) - 1 and lines[row].wrapped and x >= lines[row].width:
            x -= lines[row].width
            row += 1
        return x, row

    def _reflow(self, lines, cols):
        """Translate tmux grid_reflow's split/join rules for text cells."""
        source = list(lines)
        result = []
        scrolled = self._history_scrolled
        for index, original in enumerate(source):
            if original is None:
                continue
            # tmux's grid reflow counts each stored cell's data width. Its
            # compact padding entries count as one, even though capture-pane
            # omits them when rendering text. Preserve those raw cell indices.
            advances = [1 if cell is None else max(1, cluster_width(cell))
                        for cell in original.cells]
            width = sum(advances)
            if width > cols:
                pieces = []
                start = 0
                while start < original.width:
                    end, width_used = start, 0
                    while end < original.width:
                        advance = advances[end]
                        if end > start and width_used + advance > cols:
                            break
                        width_used += advance
                        end += 1
                    piece = original.piece(start, end,
                                           True if end < original.width else original.wrapped)
                    pieces.append(piece)
                    start = end
                result.extend(pieces)
                if index <= scrolled:
                    scrolled += len(pieces) - 1
                row = result[-1]
            else:
                row = original.piece(0, None, original.wrapped)
                result.append(row)
            width_used = sum(1 if cell is None else max(1, cluster_width(cell))
                             for cell in row.cells)
            if width_used >= cols or not row.wrapped:
                continue
            consumed = 0
            last = None
            taken = 0
            last_wrapped = True
            for follow in range(index + 1, len(source)):
                following = source[follow]
                if following is None:
                    continue
                if not following.wrapped:
                    last_wrapped = False
                if not following.width:
                    if not last_wrapped:
                        break
                    consumed += 1
                    continue
                if width_used >= cols:
                    break
                taken = 0
                for cell in following.cells:
                    advance = 1 if cell is None else max(1, cluster_width(cell))
                    if width_used + advance > cols:
                        break
                    width_used += advance
                    taken += 1
                if not taken:
                    break
                row.cells.extend(following.cells[:taken])
                row.seen.extend(following.seen[:taken])
                row.blank_seen = row.blank_seen and following.blank_seen
                last = follow
                consumed += 1
                if not last_wrapped or taken < following.width or width_used == cols:
                    break
            if not consumed or last is None:
                continue
            remaining = source[last].width - taken
            if remaining:
                source[last] = source[last].piece(taken, None, source[last].wrapped)
                consumed -= 1
            elif not last_wrapped:
                row.wrapped = False
            for consumed_index in range(index + 1, index + 1 + consumed):
                source[consumed_index] = None
            destination = len(result) - 1
            if scrolled > destination + consumed:
                scrolled -= consumed
            elif scrolled > destination:
                scrolled = destination
        self._history_scrolled = scrolled
        return result

    def resize(self, cols, rows):
        """Apply tmux height changes, then width reflow, without duplicate logs."""
        cols, rows = validate_dimensions(cols, rows)
        old_cols, old_rows = self.COLS, self.ROWS
        if (cols, rows) == (old_cols, old_rows):
            return
        cx, cy = self._c, len(self._history) + self._r
        if rows < old_rows:
            needed = old_rows - rows
            below = min(needed, old_rows - 1 - self._r)
            if below:
                del self._lines[-below:]
                # Deleting the continuation also ends the preceding physical
                # line, even when its remaining cells still fill the width.
                self._lines[-1].wrapped = False
            needed -= below
            if needed:
                removed = self._lines[:needed]
                del self._lines[:needed]
                if self._in_alt:
                    cy -= needed
                else:
                    self._history.extend(removed)
                    self._history_scrolled += needed
        elif rows > old_rows:
            needed = rows - old_rows
            pull = 0 if self._in_alt else min(needed, self._history_scrolled, len(self._history))
            if pull:
                self._lines[:0] = self._history[-pull:]
                del self._history[-pull:]
                self._history_scrolled -= pull
            self._lines.extend(self._Row() for _ in range(needed - pull))
        self.COLS, self.ROWS = cols, rows
        if old_rows != rows:
            self._scroll_top, self._scroll_bot = 0, rows - 1
        if old_cols != cols:
            self._tabs = set(range(8, cols, 8))
            if not self._in_alt:
                combined = self._history + self._lines
                cursor = self._logical_cursor(combined, cx, cy)
                combined = self._reflow(combined, cols)
                combined.extend(self._Row() for _ in range(max(0, rows - len(combined))))
                cx, cy = self._physical_cursor(combined, *cursor)
                self._history = combined[:-rows]
                self._lines = combined[-rows:]
                self._history_scrolled = min(self._history_scrolled, len(self._history))
        if cy >= len(self._history):
            self._c, self._r = cx, min(rows - 1, cy - len(self._history))
        else:
            self._c = self._r = 0
        if not self._in_alt:
            for line in self._history:
                self._emit(line)

    def flush_all(self):
        """Flush remaining main-screen fragments without changing live state."""
        lines = self._alt_state['_lines'] if self._in_alt else self._lines
        try:
            pending = [row.pending() for row in lines]
            last = len(pending) - 1
            while last >= 0 and not pending[last][0].rstrip():
                last -= 1
            for text, fresh in pending[:last + 1]:
                if fresh:
                    self.out.write(text.rstrip() + '\n')
            self.out.flush()
        except BrokenPipeError:
            pass


# ── Stream processor ──────────────────────────────────────────────────────
def process(text, scr):
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        # ── ESC sequences ─────────────────────────────────────────────
        if ch == '\x1b':
            # 3-char charset
            m = _ESC3.match(text, i)
            if m: i = m.end(); continue

            # CSI
            m = _CSI.match(text, i)
            if m:
                raw  = m.group(1)
                cmd  = m.group(2)
                prms = _parse_params(raw)
                if prms is None or (raw.startswith('?') and cmd not in ('h', 'l')):
                    i = m.end()
                    continue

                if   cmd == 'A': scr.cursor_up(    _param(prms))
                elif cmd == 'B': scr.cursor_down(  _param(prms))
                elif cmd == 'C': scr.cursor_right( _param(prms))
                elif cmd == 'D': scr.cursor_left(  _param(prms))
                elif cmd in ('G', '`'): scr.cursor_col(_param(prms))
                elif cmd == 'E':
                    scr.cursor_down(_param(prms)); scr.carriage_return()
                elif cmd == 'F':
                    scr.cursor_up(_param(prms)); scr.carriage_return()
                elif cmd in ('H', 'f'):
                    scr.cursor_pos(_param(prms, 0, 1), _param(prms, 1, 1))
                elif cmd == 'd':                         # VPA – cursor row abs
                    scr.cursor_pos(_param(prms), scr._c + 1)
                elif cmd == 's': scr.save_cursor()
                elif cmd == 'u': scr.restore_cursor()
                elif cmd == 'K': scr.erase_line(    prms[0] if prms else 0)
                elif cmd == 'J': scr.erase_display( prms[0] if prms else 0)
                elif cmd == '@': scr.insert_chars(_param(prms))
                elif cmd == 'P': scr.delete_chars(_param(prms))
                elif cmd == 'X': scr.erase_chars(_param(prms))
                elif cmd == 'L': scr.edit_lines(_param(prms), insert=True)
                elif cmd == 'M': scr.edit_lines(_param(prms))
                elif cmd == 'r':
                    # tmux distinguishes an omitted bottom margin (ROWS)
                    # from explicit zero (clamped to row one, often invalid).
                    bottom = (scr.ROWS if len(prms) < 2 or raw.split(';')[1] == ''
                              else max(1, prms[1]))
                    scr.set_scroll_region(
                        _param(prms, 0, 1), bottom)
                elif cmd == 'S':                         # SU – scroll up
                    for _ in range(min(_param(prms), scr._scroll_bot - scr._scroll_top + 1)):
                        scr._scroll_up()
                elif cmd == 'T':                         # SD – scroll down
                    for _ in range(min(_param(prms), scr._scroll_bot - scr._scroll_top + 1)):
                        scr._scroll_down()
                elif cmd in ('h', 'l'):                  # mode set/reset
                    if raw.startswith('?'):
                        for mnum in prms:
                            if mnum in (47, 1047, 1049):
                                if cmd == 'h': scr.enter_alt_screen()
                                else:          scr.leave_alt_screen()
                            elif mnum == 7:
                                scr._autowrap = cmd == 'h'
                            elif mnum == 6:
                                scr._origin = cmd == 'h'
                                scr.cursor_pos(1, 1)
                    else:
                        if 4 in prms:
                            scr._insert = cmd == 'h'
                        if 20 in prms:
                            scr._newline_mode = cmd == 'h'
                # all other CSI → discard (colours, etc.)
                i = m.end(); continue

            # OSC
            m = _OSC.match(text, i)
            if m: i = m.end(); continue

            # DEC save / restore cursor, Reverse/Forward Index
            if i + 1 < n:
                nc = text[i + 1]
                if nc == '7': scr.save_cursor();    i += 2; continue
                if nc == '8': scr.restore_cursor();  i += 2; continue
                if nc == 'M': scr.reverse_index();   i += 2; continue
                if nc == 'D': scr.index_down();      i += 2; continue
                if nc == 'E':
                    scr.carriage_return(); scr.index_down(); i += 2; continue
                if nc == 'H': scr._tabs.add(scr._c); i += 2; continue

            # generic 2-char ESC
            m = _ESC2.match(text, i)
            if m: i = m.end(); continue

            i += 1   # lone ESC
            continue

        # ── C0 controls ───────────────────────────────────────────────
        if   ch == '\r': scr.carriage_return()
        elif ch in ('\n', '\v', '\f'): scr.newline()
        elif ch == '\b': scr.backspace()
        elif ch == '\t': scr.tab()
        elif ord(ch) >= 32 and ord(ch) != 127:
            scr.write_char(ch)
        # other C0 → discard
        i += 1


class StreamProcessor:
    """Keep escape-sequence state across decoded input chunks."""

    def __init__(self, screen):
        self.screen = screen
        self._state = 'text'
        self._sequence = []
        self._csi_parameter_bytes = 0
        self._csi_intermediate_bytes = 0
        self._csi_phase = 'prefix'

    def feed(self, text):
        ready = []
        for ch in text:
            if self._state.startswith('dcs_'):
                # tmux accepts arbitrary payload bytes after a DCS final
                # byte. In that state even CAN/SUB are payload; only an
                # unescaped ESC + backslash ends the string. Headers still
                # honour cancellation and a fresh escape sequence.
                if self._state == 'dcs_payload':
                    if ch == '\x1b':
                        self._state = 'dcs_escape'
                elif self._state == 'dcs_escape':
                    if ch == '\\':
                        self.finish()
                    else:
                        self._state = 'dcs_payload'
                elif ch in ('\x18', '\x1a'):
                    self.finish()
                elif ch == '\x1b':
                    self._state = 'escape'
                    self._sequence = [ch]
                elif self._state == 'dcs_ignore':
                    pass
                elif '@' <= ch <= '~':
                    self._state = 'dcs_payload'
                elif ' ' <= ch <= '/':
                    self._state = 'dcs_intermediate'
                elif '0' <= ch <= '?':
                    if (self._state == 'dcs_intermediate' or ch == ':'
                            or (self._state == 'dcs_parameter' and ch in '<=>?')):
                        self._state = 'dcs_ignore'
                    else:
                        self._state = 'dcs_parameter'
                continue

            if self._state == 'control_string':
                # SOS, PM and APC are invisible in tmux. Unlike OSC they
                # ignore BEL; CAN/SUB cancel them and ESC starts a fresh
                # escape sequence (including the ST terminator).
                if ch in ('\x18', '\x1a'):
                    self.finish()
                elif ch == '\x1b':
                    self._state = 'escape'
                    self._sequence = [ch]
                continue

            # OSC payload is never visible and need not be buffered. Remember
            # the ESC in a possible ST even when its backslash arrives later.
            if self._state == 'osc':
                if ch == '\x07':
                    self._state = 'text'
                elif ch == '\x1b':
                    self._state = 'osc_escape'
                continue
            if self._state == 'osc_escape':
                if ch in ('\\', '\x07'):
                    self._state = 'text'
                elif ch != '\x1b':
                    self._state = 'osc'
                continue

            if ch == '\x1b':
                self._state = 'escape'
                self._sequence = [ch]
            elif self._state == 'text':
                ready.append(ch)
            elif self._state == 'escape':
                if ch == ']':
                    self._state = 'osc'
                    self._sequence = []
                elif ch == 'P':
                    self._state = 'dcs_enter'
                    self._sequence = []
                elif ch in ('X', '^', '_'):
                    self._state = 'control_string'
                    self._sequence = []
                elif ch == '[':
                    self._state = 'csi'
                    self._sequence.append(ch)
                    self._csi_parameter_bytes = 0
                    self._csi_intermediate_bytes = 0
                    self._csi_phase = 'prefix'
                elif ch in ('(', ')'):
                    self._state = 'charset'
                    self._sequence.append(ch)
                else:
                    ready.append('\x1b' + ch)
                    self.finish()
            elif self._state == 'charset':
                sequence = ''.join(self._sequence) + ch
                if _ESC3.fullmatch(sequence):
                    ready.append(sequence)
                self.finish()
            elif self._state in ('csi', 'csi_ignore'):
                if '@' <= ch <= '~':
                    # Completion is independent of support: an unknown CSI
                    # must not swallow the visible text that follows it.
                    if self._state == 'csi':
                        sequence = ''.join(self._sequence) + ch
                        if _CSI.fullmatch(sequence):
                            ready.append(sequence)
                    self.finish()
                elif ch in ('\x18', '\x1a'):  # CAN / SUB cancel the sequence
                    self.finish()
                elif ord(ch) < 32:
                    ready.append(ch)
                elif ord(ch) >= 127 or self._state == 'csi_ignore':
                    # DEL/non-ASCII do not end CSI in tmux. In particular an
                    # invalid tail must not leak into text as a fresh command.
                    continue
                elif ' ' <= ch <= '/':
                    self._csi_intermediate_bytes += 1
                    self._csi_phase = 'intermediate'
                    if self._csi_intermediate_bytes > MAX_CSI_INTERMEDIATE_BYTES:
                        self._ignore_csi()
                    else:
                        self._sequence.append(ch)
                elif '0' <= ch <= '?':
                    if ch in '<=>?':
                        if self._csi_phase != 'prefix':
                            self._ignore_csi()
                        else:
                            self._sequence.append(ch)
                            self._csi_intermediate_bytes += 1
                            self._csi_phase = 'parameters'
                    elif self._csi_phase == 'intermediate':
                        self._ignore_csi()
                    else:
                        self._csi_parameter_bytes += 1
                        self._csi_phase = 'parameters'
                        if self._csi_parameter_bytes > MAX_CSI_PARAMETER_BYTES:
                            self._ignore_csi()
                        else:
                            self._sequence.append(ch)

        if ready:
            process(''.join(ready), self.screen)

    def _ignore_csi(self):
        self._state = 'csi_ignore'
        self._sequence = []

    def finish(self):
        """Discard any incomplete control sequence at EOF or shutdown."""
        self._state = 'text'
        self._sequence = []
        self._csi_parameter_bytes = 0
        self._csi_intermediate_bytes = 0
        self._csi_phase = 'prefix'


def _queued_input_bytes(fd):
    """Snapshot unread input so a live writer cannot prolong shutdown."""
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        return max(0, info.st_size - os.lseek(fd, 0, os.SEEK_CUR))
    if stat.S_ISCHR(info.st_mode) and not os.isatty(fd):
        # Devices such as /dev/null and /dev/zero have no finite input queue.
        return 0
    count = array.array('i', [0])
    fcntl.ioctl(fd, termios.FIONREAD, count, True)
    return max(0, count[0])


# ── main ──────────────────────────────────────────────────────────────────
def main():
    try:
        if len(sys.argv) > 3:
            raise ScreenSizeError('usage: logging_filter.py [COLS ROWS]')
        cols, rows = validate_dimensions(
            sys.argv[1] if len(sys.argv) > 1 else 80,
            sys.argv[2] if len(sys.argv) > 2 else 24)
    except ScreenSizeError as error:
        print('tmux-logging: ' + str(error), file=sys.stderr)
        return 2

    stdin_fd = sys.stdin.fileno()
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    screen = Screen(cols, rows, sys.stdout)
    stream = StreamProcessor(screen)
    stopping = False

    # Wake an idle select without interrupting a screen update or stdout write.
    # Python retries interrupted reads, so a signal flag alone is insufficient.
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    previous_wakeup = signal.set_wakeup_fd(wake_write)

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, request_stop)

    remaining = None
    try:
        try:
            while True:
                if stopping:
                    # Finish the fetched chunk first, then drain only the bytes
                    # already queued. Do not wait for the producer to close.
                    if remaining is None:
                        remaining = _queued_input_bytes(stdin_fd)
                    if not remaining:
                        break
                    chunk = os.read(stdin_fd, min(4096, remaining))
                    remaining -= len(chunk)
                else:
                    ready, _, _ = select.select([stdin_fd, wake_read], [], [])
                    if wake_read in ready:
                        os.read(wake_read, 4096)
                    if stopping or stdin_fd not in ready:
                        continue
                    chunk = os.read(stdin_fd, 4096)

                if not chunk:
                    break
                stream.feed(decoder.decode(chunk))
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass

        # EOF and shutdown share one finalization path, including partial UTF-8.
        stream.feed(decoder.decode(b'', final=True))
        stream.finish()
        screen.flush_all()
    except BrokenPipeError:
        pass
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        os.close(wake_read)
        os.close(wake_write)


if __name__ == '__main__':
    sys.exit(main())
