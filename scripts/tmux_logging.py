#!/usr/bin/env python3
"""Run the screen filter from tmux's ordered control-mode stream.

The capture commands and pipe installation share one tmux command queue. The
pipe reader reports byte counts, so control output after pipe-pane is stopped
cannot accidentally become part of the recording.
"""

import codecs
from collections import deque
import copy
from difflib import SequenceMatcher
import io
from itertools import chain, islice
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time

from logging_filter import Screen, ScreenSizeError, StreamProcessor, validate_dimensions


FIELDS = (
    'pane_width', 'pane_height', 'cursor_x', 'cursor_y',
    'scroll_region_upper', 'scroll_region_lower', 'alternate_on',
    'alternate_saved_x', 'alternate_saved_y', 'wrap_flag', 'history_size',
    'history_limit', 'window_id', 'pane_tabs', 'insert_flag', 'origin_flag',
    'scroll-on-clear',
)
OCTAL = re.compile(rb'\\([0-7]{3})')
CAPTURE_ESCAPE = re.compile(rb'\\\\|\\([0-7]{3})')
LEAF = re.compile(r'(\d+)x(\d+),\d+,\d+,(\d+)(?=[,}\]]|$)')


def unescape(data):
    return OCTAL.sub(lambda m: bytes((int(m[1], 8),)), data)


def capture_rows(lines, tabs=None):
    """The -F prefix also keeps captured text out of protocol framing."""
    rows, wrapped = [], []
    for line in lines:
        flags, separator, text = line.partition(b' ')
        if not separator or not re.fullmatch(rb'[-DHOPWX]+', flags):
            raise RuntimeError('invalid tmux capture response')
        # Unlike %output, capture-pane -C escapes literal backslashes as \\.
        text = CAPTURE_ESCAPE.sub(lambda m: b'\\' if m[1] is None
                                  else bytes((int(m[1], 8),)), text)
        text = text.decode('utf-8', errors='replace')
        if '\t' in text:
            expanded = ''
            for character in text:
                if character == '\t':
                    width = Screen._Row(expanded).width
                    column = next((stop for stop in sorted(tabs or ())
                                   if stop > width), (width // 8 + 1) * 8)
                    expanded += ' ' * (column - width)
                else:
                    expanded += character
            text = expanded
        rows.append(text)
        wrapped.append(b'W' in flags)
    return rows, wrapped


def snapshot_commands(pane):
    return [
        ['display-message', '-p', '-t', pane,
         '|'.join('#{' + name + '}' for name in FIELDS)],
        ['capture-pane', '-p', '-C', '-F', '-N', '-T', '-S', '-', '-t', pane],
        ['capture-pane', '-p', '-C', '-F', '-N', '-T', '-a', '-q', '-t', pane],
        ['capture-pane', '-p', '-C', '-P', '-t', pane],
    ]


def snapshot_from_responses(responses):
    values = responses[0][0].decode('utf-8').split('|')
    if len(values) != len(FIELDS):
        raise RuntimeError('incomplete tmux pane metadata')
    metadata = dict(zip(FIELDS, values))
    cols, rows = validate_dimensions(metadata['pane_width'], metadata['pane_height'])
    if metadata['alternate_on'] == '1':
        validate_dimensions(cols, len(responses[2]))
    number = lambda key: int(metadata[key])
    tabs = [int(column) for column in metadata['pane_tabs'].split(',') if column]
    lines, wraps = capture_rows(responses[1], tabs)
    history_size = max(0, len(lines) - rows)
    result = dict(
        lines=lines[history_size:], wrapped=wraps[history_size:],
        history=lines[:history_size], history_wrapped=wraps[:history_size],
        cols=cols, rows=rows,
        cursor=(number('cursor_x'), number('cursor_y')),
        scroll_region=(number('scroll_region_upper'), number('scroll_region_lower')),
        autowrap=metadata['wrap_flag'] == '1',
        history_limit=number('history_limit'),
        origin=metadata['origin_flag'] == '1',
        insert=metadata['insert_flag'] == '1',
        scroll_on_clear=metadata['scroll-on-clear'] == '1',
        tabs=tabs,
    )
    if metadata['alternate_on'] == '1':
        main_lines, main_wraps = capture_rows(responses[2], tabs)
        result['alternate'] = dict(
            lines=main_lines, wrapped=main_wraps,
            cols=cols, rows=len(main_lines),
            cursor=(number('alternate_saved_x'), number('alternate_saved_y')),
            history_limit=number('history_limit'),
            scroll_on_clear=metadata['scroll-on-clear'] == '1',
        )
    pending = unescape(b'\n'.join(responses[3]))
    return result, pending, metadata


def command_args(commands):
    result = []
    for command in commands:
        if result:
            result.append(';')
        result.extend(command)
    return result


def logical_rows(rows):
    """Group physical rows without losing per-cell emission provenance."""
    groups = []
    text = []
    physical = []
    empty = []
    for row in rows:
        content = row.text
        text.append(content)
        physical.append(row)
        if not content:
            empty.append(row)
        if not row.wrapped:
            groups.append((''.join(text), physical, empty))
            text, physical, empty = [], [], []
    if physical:
        groups.append((''.join(text), physical, empty))
    return groups


def copy_seen_rows(previous, current):
    """Transfer cell markers between differently wrapped physical rows.

    Slices retain the flattened-cell ordering without allocating a tuple for
    every cell in the captured history. Blank-line markers are handled by the
    caller because a blank physical row need not occupy any cells.
    """
    previous = iter(previous)
    old_row = next(previous, None)
    old_i = 0
    for new_row in current:
        new_i = 0
        while new_i < new_row.width:
            while old_row is not None and old_i == old_row.width:
                old_row = next(previous, None)
                old_i = 0
            if old_row is None:
                return
            count = min(old_row.width - old_i, new_row.width - new_i)
            new_row.seen[new_i:new_i + count] = old_row.seen[old_i:old_i + count]
            old_i += count
            new_i += count


def reconcile_snapshot(screen, state):
    """Rebase at a command barrier, preserving already emitted cells.

    Captures are authoritative for cursor, margins, modes, and reflow. A
    logical-line comparison carries emission markers through a changed wrap
    width; comparing individual physical rows would duplicate old history.
    """
    old = screen._alt_state if screen._in_alt else screen._state()
    before = logical_rows(chain(old['_history'], old['_lines']))
    screen.load_snapshot(**state)
    primary = screen._alt_state if screen._in_alt else screen._state()
    after = logical_rows(chain(primary['_history'], primary['_lines']))
    # load_snapshot treats history as pre-existing only for initial bootstrap.
    # During a rebase, unmatched history is new output and must be emitted.
    for row in primary['_history']:
        row.seen[:] = b'\0' * row.width
        row.blank_seen = False
    before_text = [entry[0] for entry in before]
    after_text = [entry[0] for entry in after]
    prefix = 0
    while (prefix < min(len(before_text), len(after_text))
           and before_text[prefix] == after_text[prefix]):
        prefix += 1
    suffix = 0
    while (suffix < min(len(before_text), len(after_text)) - prefix
           and before_text[-suffix - 1] == after_text[-suffix - 1]):
        suffix += 1
    # Most resizes leave a long, possibly repetitive history unchanged. Strip
    # that prefix/suffix before asking for a diff to avoid quadratic matching.
    old_end, new_end = len(before_text) - suffix, len(after_text) - suffix
    matcher = SequenceMatcher(None, before_text[prefix:old_end],
                              after_text[prefix:new_end], autojunk=False)
    matches = [(0, 0, prefix), (old_end, new_end, suffix)]
    matches.extend((prefix + match.a, prefix + match.b, match.size)
                   for match in matcher.get_matching_blocks())
    for old_start, new_start, size in matches:
        for index in range(size):
            previous = before[old_start + index]
            current = after[new_start + index]
            copy_seen_rows(previous[1], current[1])
            for old_row, new_row in zip(previous[2], current[2]):
                new_row.blank_seen = old_row.blank_seen
    for row in primary['_history']:
        screen._emit(row)


def matches_snapshot(screen, state):
    """Check replay against tmux before committing a resize transaction."""
    history = screen._alt_state['_history'] if screen._in_alt else screen._history
    return (
        (screen.COLS, screen.ROWS) == (state['cols'], state['rows'])
        and (screen._c, screen._r) == state['cursor']
        and screen._in_alt == ('alternate' in state)
        and [row.text.rstrip() for row in screen._lines] == [line.rstrip() for line in state['lines']]
        and [row.wrapped for row in screen._lines] == state['wrapped']
        and [row.text.rstrip() for row in history] == [line.rstrip() for line in state['history']]
        and [row.wrapped for row in history] == state['history_wrapped']
        and (screen._scroll_top, screen._scroll_bot) == state['scroll_region']
        and screen._autowrap == state['autowrap']
        and screen._origin == state['origin']
        and screen._insert == state['insert']
        and screen._scroll_on_clear == state.get('scroll_on_clear', False)
        and screen._tabs == set(state['tabs'])
    )


class Protocol:
    """Decode control notifications and guarded command responses."""

    def __init__(self, callback):
        self.callback = callback
        self.buffer = bytearray()
        self.guard = None
        self.response = []

    def feed(self, data):
        self.buffer.extend(data)
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer = bytearray(rest)
            line = bytes(line)
            if self.guard is not None:
                if line in (b'%end ' + self.guard, b'%error ' + self.guard):
                    response = self.response
                    self.guard = None
                    self.response = []
                    self.callback('error' if line.startswith(b'%error') else 'response', response)
                else:
                    self.response.append(line)
            elif line.startswith(b'%begin '):
                self.guard = line[7:]
            else:
                self.callback('notification', line)


def registry_key(pane):
    return '@tmux-logging-' + pane


def filename_key(owner):
    return '@tmux-logging-file-' + owner


def start_time_key(owner):
    return '@tmux-logging-start-' + owner


def stop_time_key(owner):
    return '@tmux-logging-stop-' + owner


def recording_start_time(filename):
    """Only explicitly requested timestamped names carry interval metadata."""
    stamp = os.environ.get('TMUX_LOGGING_START_TIME', '')
    if (re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}', stamp)
            and filename.endswith('__' + stamp + '.log')):
        return stamp
    return None


def recording_end_time():
    """Sample the recording boundary before closing the control client."""
    try:
        result = subprocess.run(
            ['bash', str(Path(__file__).with_name('finalize_logging.sh')), '--timestamp'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=True)
        stamp = result.stdout.decode('ascii')
        if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}', stamp):
            return stamp
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        pass
    # Finalization can retry the clock without compromising the saved data.
    return ''


def finalize_recording_file(filename, start_time, tmux_socket, owner, pane, end_time):
    """Rename only after the writer is closed, keeping data on helper failure."""
    try:
        subprocess.run(
            ['bash', str(Path(__file__).with_name('finalize_logging.sh')), filename,
             start_time, tmux_socket, owner, pane, end_time],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def clear_recording_file(tmux_socket, owner):
    if owner is None:
        return
    try:
        # Removing the filename is the UI's completion barrier. Clear the
        # interval metadata first so it cannot outlive that barrier.
        subprocess.run(['tmux', '-S', tmux_socket,
                        'set-option', '-guq', stop_time_key(owner), ';',
                        'set-option', '-guq', start_time_key(owner), ';',
                        'set-option', '-guq', filename_key(owner)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        # A removed pane/server no longer has metadata to publish.
        pass


def tmux_quote(argument):
    """Quote one native tmux command argument, including literal newlines."""
    argument = (argument.replace('\\', '\\\\').replace('"', '\\"')
                .replace('$', '\\$').replace('\n', '\\n')
                .replace('\r', '\\r').replace('\t', '\\t'))
    return '"' + argument + '"'


def tmux_join(arguments):
    return ' '.join(tmux_quote(argument) for argument in arguments)


def ownership_condition(pane, owner):
    pid = owner.split(':', 1)[1]
    registry = '#{==:#{' + registry_key(pane) + '},' + owner + '}'
    process = '#{==:#{pane_pipe_pid},' + pid + '}'
    return '#{&&:#{pane_pipe},#{&&:' + registry + ',' + process + '}}'


def ownership_cleanup_command(pane, owner):
    """Build an atomic, ownership-checked stop and registry cleanup."""
    if not re.fullmatch(r'[0-9a-f]{32}:[1-9][0-9]{0,15}', owner):
        raise RuntimeError('invalid logging ownership record')
    pid = owner.split(':', 1)[1]
    key = registry_key(pane)
    registry_matches = '#{==:#{' + key + '},' + owner + '}'
    # The stop itself is guarded in one server queue, but after-pipe-pane
    # hooks may yield. Recheck the registry before removing its old value.
    stop = ['if-shell', '-F', '-t', pane, '#{==:#{pane_pipe_pid},' + pid + '}',
            tmux_join(['pipe-pane', '-t', pane])]
    clear = ['if-shell', '-F', '-t', pane, registry_matches,
             tmux_join(['set-option', '-gu', key])]
    body = ' ; '.join(tmux_join(command) for command in (stop, clear))
    return ['if-shell', '-F', '-t', pane, registry_matches, body]


def register_owned_pipe(tmux_socket, pane, owner):
    """A relay may register only the pipe whose PID is its own process."""
    pid = owner.split(':', 1)[1]
    command = ['if-shell', '-F', '-t', pane, '#{==:#{pane_pipe_pid},' + pid + '}',
               tmux_join(['set-option', '-g', registry_key(pane), owner])]
    subprocess.run(['tmux', '-S', tmux_socket] + command,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=3, check=True)
    # A hook may have replaced the pipe after set-option completed.
    owned = subprocess.run(
        ['tmux', '-S', tmux_socket, 'display-message', '-p', '-t', pane,
         ownership_condition(pane, owner)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=True).stdout.strip()
    if owned != b'1':
        raise RuntimeError('logging pipe was replaced before registration')


def cleanup_owned_pipe(tmux_socket, pane, owner):
    if owner is None:
        return
    try:
        subprocess.run(['tmux', '-S', tmux_socket] + ownership_cleanup_command(pane, owner),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        # The server may already have exited along with the pane.
        pass


def relay(address, tmux_socket=None, pane=None, token=None):
    owner = None if token is None else token + ':' + str(os.getpid())
    worker_gone = False
    try:
        if owner is not None:
            try:
                register_owned_pipe(tmux_socket, pane, owner)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                # A hook can replace this pipe before it registers. Tell the
                # waiting worker immediately rather than waiting for timeout.
                try:
                    with socket.socket(socket.AF_UNIX) as failed:
                        failed.connect(address)
                        failed.sendall(('ERROR ' + str(error) + '\n').encode())
                except OSError:
                    pass
                raise
        with socket.socket(socket.AF_UNIX) as peer:
            peer.connect(address)
            if owner is not None:
                peer.sendall(('HELLO ' + owner + '\n').encode())
            count = 0
            while True:
                # A failed worker closes its socket. End this relay promptly
                # even when the pane is idle, then clear only its own pipe.
                ready, _, _ = select.select([0, peer], [], [])
                if peer in ready and not peer.recv(1):
                    worker_gone = True
                    return
                if 0 not in ready:
                    continue
                data = os.read(0, 65536)
                if not data:
                    peer.sendall(('END %d\n' % count).encode())
                    return
                count += len(data)
                peer.sendall(('%d\n' % count).encode())
    except (BrokenPipeError, ConnectionResetError):
        worker_gone = True
        raise
    finally:
        if owner is not None:
            try:
                cleanup_owned_pipe(tmux_socket, pane, owner)
            finally:
                # Ordinary pane EOF precedes the worker's final screen flush.
                # Only worker EOF can stand in for its completion cleanup.
                if worker_gone:
                    clear_recording_file(tmux_socket, owner)


class Recording:
    def __init__(self, tmux_socket, pane, session, output, address):
        self.tmux_socket = tmux_socket
        self.pane = pane
        self.token = secrets.token_hex(16)
        self.owner = None
        self.relay_owner = None
        self.output = output
        self.screen = None
        self.stream = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.responses = []
        self.events = deque()
        self.available = 0
        self.consumed = 0
        self.final_count = None
        self.ready = False
        self.stopping = False
        self.window = None
        self.observed = 0
        self.rebase_responses = None
        self.rebase_waiting = False
        self.finished = False
        self.size_error = False
        self.protocol = Protocol(self.receive)
        relay_command = 'exec ' + shlex.join([
            sys.executable, '-B', str(Path(__file__).resolve()), 'relay', address,
            tmux_socket, pane, self.token])
        # pipe-pane expands time and formats before /bin/sh sees the quotes.
        # Every hash is inside a shlex-quoted word: split that literal after
        # the hash to prevent formats/jobs/styles, then escape strftime '%'.
        relay_command = relay_command.replace('#', "#''").replace('%', '%%')
        commands = [['attach-session', '-E', '-f', 'read-only,ignore-size', '-t', session]]
        commands += snapshot_commands(pane)
        start_commands = [
            ['pipe-pane', '-t', pane, relay_command],
            ['display-message', '-p', '-t', pane, 'logging-ready ' + self.token],
        ]
        # pipe-pane -o closes an existing pipe. Use a synchronous server-side
        # guard instead. The relay registers its own PID separately because
        # an after-pipe-pane hook can replace this pipe before the barrier.
        commands += [[
            'if-shell', '-F', '-t', pane, '#{pane_pipe}',
            tmux_join(['display-message', '-p', 'logging-busy ' + self.token]),
            ' ; '.join(tmux_join(command) for command in start_commands),
        ]]
        self.control = subprocess.Popen(
            ['tmux', '-u', '-S', tmux_socket, '-C'] + command_args(commands),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def receive(self, kind, value):
        if kind == 'error':
            raise RuntimeError(b'\n'.join(value).decode('utf-8', errors='replace'))
        if kind == 'response':
            if not self.ready:
                self.responses.append(value)
                if value == [('logging-busy ' + self.token).encode()]:
                    raise RuntimeError('pane already has an active output pipe')
                marker = ('logging-ready ' + self.token).encode()
                if value == [marker]:
                    state, pending, metadata = snapshot_from_responses(self.responses[1:5])
                    self.screen = Screen(state['cols'], state['rows'], self.output)
                    self.screen.load_snapshot(**state)
                    self.stream = StreamProcessor(self.screen)
                    self.stream.feed(self.decoder.decode(pending))
                    self.window = metadata['window_id']
                    self.responses.clear()
                    self.ready = True
            elif self.rebase_responses is not None:
                self.rebase_responses.append(value)
                if len(self.rebase_responses) == 4:
                    try:
                        state, pending, metadata = snapshot_from_responses(self.rebase_responses)
                    except ScreenSizeError as error:
                        self.events.append(('size-error', error))
                        self.size_error = True
                        self.rebase_waiting = False
                    else:
                        self.events.append(('snapshot', (self.observed, state, pending, metadata)))
                    self.rebase_responses = None
            return
        if not self.ready or getattr(self, 'size_error', False):
            return
        if value.startswith(b'%output '):
            _, pane, data = value.split(b' ', 2)
            if pane.decode() == self.pane:
                data = unescape(data)
                self.observed += len(data)
                self.events.append(('output', data))
        elif value.startswith(b'%layout-change '):
            parts = value.decode().split(' ')
            if len(parts) >= 4 and parts[1] == self.window:
                # Zoom uses visible-layout; hidden panes retain normal-layout.
                for layout in (parts[3], parts[2]):
                    dimensions = next(((w, h) for w, h, pane in LEAF.findall(layout)
                                       if '%' + pane == self.pane), None)
                    if dimensions is not None:
                        try:
                            dimensions = validate_dimensions(*dimensions)
                        except ScreenSizeError as error:
                            self.events.append(('size-error', error))
                            self.size_error = True
                            self.rebase_waiting = False
                            self.rebase_responses = None
                            break
                        self.events.append(('resize', dimensions))
                        if not self.rebase_waiting:
                            self.request_snapshot()
                        break
        elif value.startswith(b'%exit'):
            self.stopping = True

    def request_snapshot(self):
        self.rebase_waiting = True
        self.rebase_responses = []
        commands = ' ; '.join(shlex.join(command) for command in snapshot_commands(self.pane))
        self.control.stdin.write((commands + '\n').encode())
        self.control.stdin.flush()

    def rebase(self, snapshot_index, state, pending):
        staged = io.StringIO()
        candidate = copy.deepcopy(self.screen, {id(self.output): staged})
        candidate_stream = copy.deepcopy(self.stream, {id(self.screen): candidate})
        candidate_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        candidate_decoder.setstate(self.decoder.getstate())
        for kind, data in islice(self.events, snapshot_index):
            if kind == 'resize':
                candidate.resize(*data)
            elif kind == 'output':
                candidate_stream.feed(candidate_decoder.decode(data))
        if matches_snapshot(candidate, state):
            # Normal resize paths retain every output byte, including history
            # already evicted from tmux before this capture was requested.
            self.output.write(staged.getvalue())
            candidate.out = self.output
            self.screen = candidate
            self.stream = candidate_stream
            self.decoder = candidate_decoder
        else:
            # Several resizes in one command batch may report only the last
            # geometry. In that case the capture supplies the missing state.
            if self.screen._in_alt and candidate._in_alt and 'alternate' in state:
                # tmux's saved grid keeps its original dimensions while the
                # application resizes; capture-pane -a uses the current width
                # and may truncate that saved grid. Keep the main screen we
                # have already tracked instead of replacing it with a crop.
                saved = candidate._alt_state
                state['alternate'] = dict(
                    lines=[row.text for row in saved['_lines']],
                    wrapped=[row.wrapped for row in saved['_lines']],
                    history=[row.text for row in saved['_history']],
                    history_wrapped=[row.wrapped for row in saved['_history']],
                    cols=saved['COLS'], rows=saved['ROWS'],
                    cursor=(saved['_c'], saved['_r']),
                    scroll_region=(saved['_scroll_top'], saved['_scroll_bot']),
                    history_limit=saved['_history_limit'],
                    history_scrolled=saved['_history_scrolled'],
                    autowrap=saved['_autowrap'], origin=saved['_origin'],
                    insert=saved['_insert'], tabs=saved['_tabs'],
                    newline_mode=saved['_newline_mode'],
                    scroll_on_clear=saved['_scroll_on_clear'],
                )
            if candidate._full_clear_count > self.screen._full_clear_count:
                # A full clear can remove text from both the live screen and
                # captured history before this barrier. Keep its replayed
                # output and emission markers together: reconciliation then
                # corrects geometry without logging matching history twice.
                # Coalesced layouts can still make these earlier physical
                # wraps approximate; the capture cannot recover erased rows.
                self.output.write(staged.getvalue())
                candidate.out = self.output
                self.screen = candidate
            reconcile_snapshot(self.screen, state)
            self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            self.stream = StreamProcessor(self.screen)
            self.stream.feed(self.decoder.decode(pending))

    def drain(self):
        while self.events:
            kind, value = self.events[0]
            if kind == 'size-error':
                raise value
            if kind == 'resize':
                if getattr(self, 'size_error', False):
                    # Replay valid geometry and output preceding the rejected
                    # layout before the worker flushes and reports the error.
                    self.screen.resize(*value)
                    self.events.popleft()
                    continue
                # Keep all output since the first layout event uncommitted
                # until the authoritative capture reaches this same queue.
                snapshot_index = next((i for i, event in enumerate(self.events)
                                       if event[0] == 'snapshot'), None)
                if snapshot_index is None:
                    if self.final_count is None or self.rebase_waiting:
                        if not self.rebase_waiting:
                            self.request_snapshot()
                        break
                else:
                    offset, state, pending, metadata = self.events[snapshot_index][1]
                    if offset <= self.available:
                        self.rebase(snapshot_index, state, pending)
                        self.window = metadata['window_id']
                        self.consumed = offset
                        for _ in range(snapshot_index + 1):
                            self.events.popleft()
                        self.rebase_waiting = False
                        self.output.flush()
                        continue
                    if self.final_count is None:
                        break
                # A stop can precede an in-flight capture. Such a capture
                # includes post-stop bytes and must never replace this screen.
                self.screen.resize(*value)
                self.events.popleft()
                continue
            if kind == 'snapshot':
                self.events.popleft()
                self.rebase_waiting = False
                continue
            count = min(len(value), self.available - self.consumed)
            if count <= 0:
                break
            self.stream.feed(self.decoder.decode(value[:count]))
            self.consumed += count
            if count == len(value):
                self.events.popleft()
            else:
                self.events[0] = (kind, value[count:])
            self.output.flush()

    def request_stop(self):
        if not self.stopping:
            if self.owner is None:
                raise RuntimeError('logging stopped before pipe ownership was confirmed')
            self.stopping = True
            command = ownership_cleanup_command(self.pane, self.owner)
            self.control.stdin.write((tmux_join(command) + '\n').encode())
            self.control.stdin.flush()

    def finish(self):
        if getattr(self, 'finished', False):
            return
        self.finished = True
        if self.screen is not None:
            self.stream.feed(self.decoder.decode(b'', final=True))
            self.stream.finish()
            self.screen.flush_all()

    def close(self):
        cleanup_owned_pipe(self.tmux_socket, self.pane, self.owner)
        self.control.stdin.close()
        try:
            self.control.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.control.terminate()
            self.control.wait(timeout=3)
        self.control.stdout.close()
        self.control.stderr.close()


def worker(tmux_socket, pane, session, filename, status_fd):
    filename = os.path.abspath(filename)
    start_time = recording_start_time(filename)
    announced_ready = False
    end_time = None

    def mark_end():
        nonlocal end_time
        if announced_ready and start_time is not None and end_time is None:
            end_time = recording_end_time()

    def report(message):
        nonlocal status_fd
        if status_fd is not None:
            os.write(status_fd, (message + '\n').encode())
            os.close(status_fd)
            status_fd = None

    recording = None
    peer = None
    output = None
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    old_wakeup = signal.set_wakeup_fd(wake_write)
    stop_requested = False

    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        with tempfile.TemporaryDirectory(prefix='tmux-logging-') as directory, \
                socket.socket(socket.AF_UNIX) as listener:
            output = open(filename, 'a', encoding='utf-8')
            address = str(Path(directory) / 'pipe.sock')
            listener.bind(address)
            listener.listen(1)
            recording = Recording(tmux_socket, pane, session, output, address)
            deadline = time.monotonic() + 15
            count_buffer = bytearray()
            control_closed = False
            disconnected_at = None
            while True:
                if stop_requested:
                    mark_end()
                    if not recording.ready:
                        raise RuntimeError('logging startup interrupted')
                    recording.request_stop()
                fds = [wake_read]
                if not control_closed:
                    fds.append(recording.control.stdout)
                if peer is None:
                    fds.append(listener)
                elif recording.final_count is None:
                    fds.append(peer)
                ready, _, _ = select.select(fds, [], [], 1)
                if wake_read in ready:
                    os.read(wake_read, 4096)
                if listener in ready:
                    peer, _ = listener.accept()
                if recording.control.stdout in ready:
                    data = os.read(recording.control.stdout.fileno(), 65536)
                    if not data:
                        mark_end()
                        if not recording.ready:
                            error = recording.control.stderr.read().decode(errors='replace')
                            raise RuntimeError(error or 'tmux control client closed during startup')
                        control_closed = True
                        disconnected_at = time.monotonic()
                    else:
                        recording.protocol.feed(data)
                if peer is not None and peer in ready:
                    data = peer.recv(65536)
                    if not data and recording.final_count is None:
                        mark_end()
                        raise RuntimeError('tmux pipe reader closed without its final byte count')
                    count_buffer.extend(data)
                    while b'\n' in count_buffer:
                        line, _, rest = count_buffer.partition(b'\n')
                        count_buffer = bytearray(rest)
                        if line.startswith(b'ERROR '):
                            raise RuntimeError(line[6:].decode(errors='replace'))
                        elif line.startswith(b'HELLO '):
                            owner = line[6:].decode()
                            if (not re.fullmatch(recording.token + r':[1-9][0-9]{0,15}', owner)
                                    or recording.owner not in (None, owner)):
                                raise RuntimeError('logging pipe reader ownership does not match')
                            recording.relay_owner = recording.owner = owner
                        elif line.startswith(b'END '):
                            mark_end()
                            recording.final_count = int(line[4:])
                            recording.available = recording.final_count
                        else:
                            recording.available = int(line)
                if recording.ready:
                    recording.drain()
                    if (recording.relay_owner is not None
                            and recording.relay_owner != recording.owner):
                        raise RuntimeError('logging pipe reader ownership does not match')
                    if (recording.owner is not None and recording.relay_owner == recording.owner
                            and recording.final_count is None and not recording.stopping):
                        if status_fd is not None:
                            metadata = []
                            if start_time is not None:
                                metadata.append(tmux_join(['set-option', '-gq',
                                                           start_time_key(recording.owner),
                                                           start_time]))
                            metadata.append(tmux_join(['set-option', '-gq',
                                                       filename_key(recording.owner), filename]))
                            subprocess.run(
                                ['tmux', '-S', tmux_socket, 'if-shell', '-F', '1',
                                 ' ; '.join(metadata)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=3, check=True)
                            owned = subprocess.run(
                                ['tmux', '-S', tmux_socket, 'display-message', '-p', '-t', pane,
                                 ownership_condition(pane, recording.owner)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3,
                                check=True).stdout.strip()
                            if owned != b'1':
                                raise RuntimeError('logging pipe was replaced during startup')
                            report('READY')
                            announced_ready = True
                if status_fd is not None and time.monotonic() >= deadline:
                    raise RuntimeError('timed out while synchronizing with tmux')
                if control_closed and recording.final_count is not None:
                    # A pane/server exit closes both transports. Consume the
                    # already received control output after the final count.
                    recording.rebase_waiting = False
                    recording.drain()
                    break
                if disconnected_at is not None and time.monotonic() - disconnected_at >= 3:
                    raise RuntimeError('tmux control client disconnected before the pipe reader')
                if (recording.final_count is not None
                        and recording.consumed == recording.final_count
                        and not recording.rebase_waiting):
                    break
            recording.finish()
    except Exception as error:
        mark_end()
        was_ready = announced_ready
        report('ERROR ' + str(error))
        if recording is not None:
            try:
                recording.finish()
            except OSError:
                pass
        if was_ready:
            subprocess.run(['tmux', '-S', tmux_socket, 'display-message', '-t', pane,
                            'tmux-logging stopped: ' + str(error)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        raise
    finally:
        mark_end()
        try:
            if recording is not None:
                recording.close()
        finally:
            output_closed = False
            try:
                if output is not None:
                    output.close()
                    output_closed = True
            finally:
                try:
                    try:
                        if announced_ready and start_time is not None and output_closed:
                            finalize_recording_file(filename, start_time, tmux_socket,
                                                    recording.owner, pane, end_time or '')
                    finally:
                        if recording is not None:
                            clear_recording_file(tmux_socket, recording.owner)
                finally:
                    # Keep the worker socket open until the file is closed:
                    # relay EOF cleanup must not advertise completion sooner.
                    if peer is not None:
                        peer.close()
                    signal.set_wakeup_fd(old_wakeup)
                    os.close(wake_read)
                    os.close(wake_write)
                    if status_fd is not None:
                        os.close(status_fd)


def start(filename, target=None):
    command = ['tmux', 'display-message', '-p']
    if target is not None:
        command += ['-t', target]
    command += ['#{socket_path}\n#{pane_id}\n#{session_id}\n#{version}\n#{pane_width}\n#{pane_height}']
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE)
    tmux_socket, pane, session, version, cols, rows = result.stdout.decode().rstrip('\n').split('\n')
    validate_dimensions(cols, rows)
    match = re.match(r'(\d+)\.(\d+)', version)
    if match is None or tuple(map(int, match.groups())) < (3, 7):
        raise RuntimeError('synchronized screen logging requires tmux 3.7 or newer')
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen(
        [sys.executable, '-B', str(Path(__file__).resolve()), 'worker', tmux_socket,
         pane, session, filename, str(write_fd)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        pass_fds=(write_fd,), start_new_session=True)
    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], 20)
        status = os.read(read_fd, 4096).decode().strip() if ready else ''
        if status != 'READY':
            child.terminate()
            child.wait(timeout=5)
            raise RuntimeError((status[6:] if status.startswith('ERROR ') else status)
                               or 'logging worker did not start')
    finally:
        os.close(read_fd)


def main():
    mode, *args = sys.argv[1:]
    if mode == 'relay':
        relay(*args)
    elif mode == 'worker':
        worker(*args[:-1], int(args[-1]))
    elif mode == 'start':
        start(*args)
    else:
        raise RuntimeError('unknown mode: ' + mode)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('tmux-logging: ' + str(error), file=sys.stderr)
        sys.exit(1)
