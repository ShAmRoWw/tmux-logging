#!/usr/bin/env python3
"""
VT100 screen-emulator filter for tmux-logging.

Maintains a fixed-size screen buffer (ROWS x COLS) that mirrors what the
real terminal displays.  A line is written to the log **only** when it
scrolls off the top of the screen.  This single rule makes every
problematic case correct by construction:

  Tab-completion menus  — drawn below the prompt, then erased in-place
                          with cursor-up + ESC[J.  They never scroll off,
                          so they never appear in the log.

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

import re
import sys

# ── Regex table for escape-sequence lexing ────────────────────────────────
_CSI  = re.compile(r'\x1b\[([0-9;?]*)([A-Za-z@`])')
_OSC  = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)')
_ESC3 = re.compile(r'\x1b[()][0-9A-Za-z]')   # charset designation
_ESC2 = re.compile(r'\x1b[^[\]78()]')          # generic 2-char (excl. 7/8)


# ── Helpers ───────────────────────────────────────────────────────────────
def _param(parts, idx=0, default=1):
    try:
        v = parts[idx]
        return v if v != 0 else default
    except IndexError:
        return default


def _parse_params(raw):
    s = raw.replace('?', '')
    if not s:
        return []
    return [int(p) if p.isdigit() else 0 for p in s.split(';')]


# ── Screen emulator ──────────────────────────────────────────────────────
class Screen:
    """Fixed-size VT100 screen with scroll-off-top logging."""

    def __init__(self, cols, rows, out):
        self.COLS = cols
        self.ROWS = rows
        self.out  = out

        self._buf = ['' for _ in range(rows)]
        self._r   = 0          # cursor row  (0-based)
        self._c   = 0          # cursor col  (0-based)

        self._scroll_top = 0          # scrolling region top (0-based)
        self._scroll_bot = rows - 1   # scrolling region bottom (0-based)

        self._saved     = None  # (row, col)
        self._in_alt    = False
        self._alt_state = None  # (buf, r, c, saved, stop, sbot)

    # ── scrolling ─────────────────────────────────────────────────────

    def _scroll_up(self):
        top = self._scroll_top
        bot = self._scroll_bot
        line = self._buf[top]
        # A line leaving the true top of the main screen → log it
        if not self._in_alt and top == 0:
            self.out.write(line.rstrip() + '\n')
        for i in range(top, bot):
            self._buf[i] = self._buf[i + 1]
        self._buf[bot] = ''

    def _scroll_down(self):
        top = self._scroll_top
        bot = self._scroll_bot
        for i in range(bot, top, -1):
            self._buf[i] = self._buf[i - 1]
        self._buf[top] = ''

    # ── index / reverse index ──────────────────────────────────────────

    def reverse_index(self):
        """ESC M — move cursor up; scroll down if at top of scroll region."""
        if self._r == self._scroll_top:
            self._scroll_down()
        else:
            self._r = max(0, self._r - 1)

    def index_down(self):
        """ESC D — move cursor down; scroll up if at bottom of scroll region."""
        if self._r == self._scroll_bot:
            self._scroll_up()
        else:
            self._r = min(self.ROWS - 1, self._r + 1)

    # ── character output ──────────────────────────────────────────────

    def write_char(self, ch):
        if self._c >= self.COLS:          # auto-wrap
            self._c = 0
            if self._r == self._scroll_bot:
                self._scroll_up()
            else:
                self._r = min(self.ROWS - 1, self._r + 1)

        line = self._buf[self._r]
        c = self._c
        if   c < len(line):  self._buf[self._r] = line[:c] + ch + line[c+1:]
        elif c > len(line):  self._buf[self._r] = line + ' ' * (c - len(line)) + ch
        else:                self._buf[self._r] = line + ch
        self._c += 1

    # ── cursor motion & control ───────────────────────────────────────

    def newline(self):
        if self._r == self._scroll_bot:
            self._scroll_up()
        else:
            self._r = min(self.ROWS - 1, self._r + 1)
        self._c = 0     # pipe-pane output uses bare LF without CR

    def carriage_return(self):  self._c = 0
    def backspace(self):        self._c = max(0, self._c - 1)
    def tab(self):              self._c = min(self.COLS - 1, (self._c // 8 + 1) * 8)

    def cursor_up(self, n):    self._r = max(self._scroll_top, self._r - n)
    def cursor_down(self, n):  self._r = min(self._scroll_bot, self._r + n)
    def cursor_right(self, n): self._c = min(self.COLS - 1, self._c + n)
    def cursor_left(self, n):  self._c = max(0, self._c - n)
    def cursor_col(self, n1):  self._c = max(0, min(self.COLS - 1, n1 - 1))

    def cursor_pos(self, r1, c1):   # 1-based
        self._r = max(0, min(self.ROWS - 1, r1 - 1))
        self._c = max(0, min(self.COLS - 1, c1 - 1))

    def save_cursor(self):     self._saved = (self._r, self._c)
    def restore_cursor(self):
        if self._saved is not None:
            self._r, self._c = self._saved

    # ── erase ─────────────────────────────────────────────────────────

    def erase_line(self, mode):
        r = self._r
        line = self._buf[r]
        if   mode == 0: self._buf[r] = line[:self._c]
        elif mode == 1: self._buf[r] = ' ' * self._c + line[self._c:]
        elif mode == 2: self._buf[r] = ''

    def erase_display(self, mode):
        if mode == 0:                        # cursor → end
            self._buf[self._r] = self._buf[self._r][:self._c]
            for i in range(self._r + 1, self.ROWS):
                self._buf[i] = ''
        elif mode == 1:                      # start → cursor
            for i in range(self._r):
                self._buf[i] = ''
            self._buf[self._r] = ' ' * self._c + self._buf[self._r][self._c:]
        elif mode in (2, 3):                 # whole screen
            for i in range(self.ROWS):
                self._buf[i] = ''

    # ── scrolling region ──────────────────────────────────────────────

    def set_scroll_region(self, top1, bot1):
        if top1 == 0 and bot1 == 0:          # reset
            self._scroll_top = 0
            self._scroll_bot = self.ROWS - 1
        else:
            self._scroll_top = max(0, top1 - 1)
            self._scroll_bot = min(self.ROWS - 1, bot1 - 1)
        self._r = 0
        self._c = 0

    # ── alternate screen buffer ───────────────────────────────────────

    def enter_alt_screen(self):
        if self._in_alt:
            return
        self._in_alt = True
        self._alt_state = (
            self._buf[:], self._r, self._c, self._saved,
            self._scroll_top, self._scroll_bot,
        )
        self._buf = ['' for _ in range(self.ROWS)]
        self._r = 0
        self._c = 0
        self._saved = None
        self._scroll_top = 0
        self._scroll_bot = self.ROWS - 1

    def leave_alt_screen(self):
        if not self._in_alt:
            return
        self._in_alt = False
        (self._buf, self._r, self._c, self._saved,
         self._scroll_top, self._scroll_bot) = self._alt_state
        self._alt_state = None

    # ── end-of-session flush ──────────────────────────────────────────

    def flush_all(self):
        """Write remaining visible lines to the log."""
        try:
            # Trim trailing empty rows but preserve internal blank lines
            last = len(self._buf) - 1
            while last >= 0 and not self._buf[last].rstrip():
                last -= 1
            for i in range(last + 1):
                self.out.write(self._buf[i].rstrip() + '\n')
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

                if   cmd == 'A': scr.cursor_up(    _param(prms))
                elif cmd == 'B': scr.cursor_down(  _param(prms))
                elif cmd == 'C': scr.cursor_right( _param(prms))
                elif cmd == 'D': scr.cursor_left(  _param(prms))
                elif cmd == 'G': scr.cursor_col(   _param(prms))
                elif cmd in ('H', 'f'):
                    scr.cursor_pos(_param(prms, 0, 1), _param(prms, 1, 1))
                elif cmd == 'd':                         # VPA – cursor row abs
                    scr.cursor_pos(_param(prms), scr._c + 1)
                elif cmd == 's': scr.save_cursor()
                elif cmd == 'u': scr.restore_cursor()
                elif cmd == 'K': scr.erase_line(    prms[0] if prms else 0)
                elif cmd == 'J': scr.erase_display( prms[0] if prms else 0)
                elif cmd == 'r':
                    scr.set_scroll_region(
                        _param(prms, 0, 0), _param(prms, 1, 0))
                elif cmd == 'S':                         # SU – scroll up
                    for _ in range(_param(prms)):
                        scr._scroll_up()
                elif cmd == 'T':                         # SD – scroll down
                    for _ in range(_param(prms)):
                        scr._scroll_down()
                elif cmd in ('h', 'l'):                  # mode set/reset
                    if '?' in raw:
                        for p_str in raw.replace('?', '').split(';'):
                            try:    mnum = int(p_str)
                            except: continue
                            if mnum in (47, 1047, 1049):
                                if cmd == 'h': scr.enter_alt_screen()
                                else:          scr.leave_alt_screen()
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

            # generic 2-char ESC
            m = _ESC2.match(text, i)
            if m: i = m.end(); continue

            i += 1   # lone ESC
            continue

        # ── C0 controls ───────────────────────────────────────────────
        if   ch == '\r': scr.carriage_return()
        elif ch == '\n': scr.newline()
        elif ch == '\b': scr.backspace()
        elif ch == '\t': scr.tab()
        elif ord(ch) >= 32:
            scr.write_char(ch)
        # other C0 → discard
        i += 1


# ── main ──────────────────────────────────────────────────────────────────
def main():
    cols = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 24

    stdin  = open(sys.stdin.fileno(), 'r', newline='',
                  encoding='utf-8', errors='replace', closefd=False)
    screen = Screen(cols, rows, sys.stdout)

    pending = ''
    try:
        while True:
            chunk = stdin.read(4096)
            if not chunk:
                break
            data = pending + chunk
            # Keep any trailing incomplete escape sequence for the next read.
            # Find the last ESC in the buffer; if it doesn't form a complete
            # sequence by the end of data, hold it back.
            pending = ''
            last_esc = data.rfind('\x1b')
            if last_esc >= 0:
                tail = data[last_esc:]
                # Check if the tail is a complete escape sequence (or plain ESC
                # followed by a non-ESC-intro char that forms a 2-char seq).
                # If the tail is just a lone ESC, or starts a CSI/OSC/charset
                # but hasn't reached the terminating character, hold it back.
                complete = False
                if len(tail) == 1:
                    # Lone ESC at very end — definitely incomplete
                    complete = False
                elif _CSI.match(tail):
                    complete = True
                elif _OSC.match(tail):
                    complete = True
                elif _ESC3.match(tail):
                    complete = True
                elif _ESC2.match(tail):
                    complete = True
                elif tail[1] in ('7', '8'):
                    complete = True
                elif tail[1] == '[' or tail[1] == ']' or tail[1] in ('(', ')'):
                    # Started a CSI/OSC/charset but may not have finished
                    complete = False
                else:
                    complete = True  # 2-char ESC sequence

                if not complete:
                    pending = tail
                    data = data[:last_esc]

            if data:
                process(data, screen)
                sys.stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass

    # Process any remaining pending bytes
    if pending:
        process(pending, screen)
    screen.flush_all()


if __name__ == '__main__':
    main()
