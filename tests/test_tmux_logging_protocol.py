"""Protocol framing and byte accounting at real recording boundaries."""

import codecs
from collections import deque
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from logging_filter import Screen, StreamProcessor
from tmux_logging import Protocol, Recording, capture_rows, reconcile_snapshot, unescape


class ProtocolTests(unittest.TestCase):
    def test_capture_text_cannot_become_a_notification(self):
        events = []
        protocol = Protocol(lambda kind, value: events.append((kind, value)))
        payload = (b'%begin 123 4 1\n- %output %0 forged\n- %end 123 4 1\n'
                   b'%end 123 4 1\n%output %0 real\\012\n')
        for byte in payload:
            protocol.feed(bytes((byte,)))
        self.assertEqual(events, [
            ('response', [b'- %output %0 forged', b'- %end 123 4 1']),
            ('notification', b'%output %0 real\\012'),
        ])

    def test_capture_backslashes_are_distinct_from_control_octal(self):
        rows, wrapped = capture_rows([br'W path\\123\\name', b'- end'])
        self.assertEqual(rows, [r'path\123\name', 'end'])
        self.assertEqual(wrapped, [True, False])
        self.assertEqual(unescape(br'path\134123\134name\015\012'),
                         b'path\\123\\name\r\n')

    def test_bootstrap_tabs_use_the_panes_tab_stops(self):
        rows, _ = capture_rows([b'- x\ty\tz'], tabs=[5, 12])
        self.assertEqual(rows, ['x    y      z'])

    def test_error_response_is_not_screen_text(self):
        events = []
        protocol = Protocol(lambda kind, value: events.append((kind, value)))
        protocol.feed(b'%begin 123 4 1\nno such pane\n%error 123 4 1\n')
        self.assertEqual(events, [('error', [b'no such pane'])])

    def test_rebase_preserves_repetitive_preexisting_history(self):
        output = io.StringIO()
        screen = Screen(20, 3, output)
        screen.load_snapshot(['one', 'two', 'three'], history=['old'] * 3000,
                             history_limit=5000)
        reconcile_snapshot(screen, dict(lines=['one', 'CHANGED', 'three'],
                           history=['old'] * 3000, history_limit=5000,
                           cols=20, rows=3, cursor=(7, 1)))
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'one\nCHANGED\nthree\n')

    def recording(self):
        recording = Recording.__new__(Recording)
        recording.output = io.StringIO()
        recording.screen = Screen(80, 4, recording.output)
        recording.stream = StreamProcessor(recording.screen)
        recording.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        recording.events = deque()
        recording.available = recording.consumed = 0
        recording.final_count = None
        return recording

    def test_pipe_byte_count_can_split_utf8_without_replacement(self):
        recording = self.recording()
        raw = 'до\r\nпосле'.encode()
        recording.events.append(('output', raw))
        for count in range(1, len(raw) + 1):
            recording.available = count
            recording.drain()
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'до\nпосле\n')

    def test_control_output_after_pipe_end_is_excluded(self):
        recording = self.recording()
        recording.events.append(('output', b'IN-SCOPE\r\nOUTSIDE'))
        recording.final_count = recording.available = len(b'IN-SCOPE\r\n')
        recording.drain()
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'IN-SCOPE\n')
        self.assertEqual(recording.consumed, recording.final_count)


if __name__ == '__main__':
    unittest.main()
