"""Reject unsupported geometry before allocation or loss of pending output."""

import codecs
from collections import deque
import copy
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import logging_filter
import tmux_logging
import test_tmux_logging_sync as sync_tests


class ScreenDimensionsTests(unittest.TestCase):
    def test_dimension_boundaries_and_bounded_leading_zeros(self):
        for cols, rows, expected in ((1, 1, (1, 1)), (10000, 100, (10000, 100)),
                                     (100, 10000, (100, 10000)),
                                     ('0000000000000080', '0024', (80, 24))):
            with self.subTest(cols=cols, rows=rows):
                self.assertEqual(logging_filter.validate_dimensions(cols, rows), expected)

    def test_invalid_axes_and_product_are_rejected_without_rows(self):
        invalid = [(0, 24), (-1, 24), (80, 0), (10001, 1), (1, 10001),
                   (1001, 1000), (10000, 101), ('x', 24), (' 80', 24),
                   ('+80', 24), ('８０', 24), (True, 24), (80.0, 24),
                   ('9' * 100000, 24), ('0' * 100000 + '1', 24)]
        for cols, rows in invalid:
            with self.subTest(cols=str(cols)[:20], rows=rows):
                with mock.patch.object(logging_filter.Screen, '_Row') as row:
                    with self.assertRaises(logging_filter.ScreenSizeError):
                        logging_filter.Screen(cols, rows, io.StringIO())
                    row.assert_not_called()

    def test_resize_rejects_before_mutating_pending_screen(self):
        output = io.StringIO()
        screen = logging_filter.Screen(20, 4, output)
        logging_filter.StreamProcessor(screen).feed('KEEP\r\nPENDING')
        before = copy.deepcopy(screen._state())
        with self.assertRaises(logging_filter.ScreenSizeError):
            screen.resize(10000, 101)
        self.assertEqual(screen._state(), before)
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'KEEP\nPENDING\n')

    def test_snapshot_validates_nested_alternate_before_mutating(self):
        output = io.StringIO()
        screen = logging_filter.Screen(20, 4, output)
        logging_filter.StreamProcessor(screen).feed('KEEP')
        before = copy.deepcopy(screen._state())
        alternate = dict(lines=['hidden'], cols=30, rows=5,
                         alternate=dict(lines=[], cols=10001, rows=1))
        with mock.patch.object(logging_filter.Screen, '_Row') as row:
            with self.assertRaises(logging_filter.ScreenSizeError):
                screen.load_snapshot(['new'], cols=40, rows=6, alternate=alternate)
            row.assert_not_called()
        self.assertEqual(screen._state(), before)

    def test_recursive_alternate_metadata_is_rejected(self):
        screen = logging_filter.Screen(20, 4, io.StringIO())
        alternate = dict(lines=[])
        alternate['alternate'] = alternate
        with self.assertRaises(logging_filter.ScreenSizeError):
            screen.load_snapshot([], alternate=alternate)

    def test_cli_invalid_geometry_exits_without_waiting_for_input(self):
        for args in (['0', '24'], ['-1', '24'], ['10001', '1'],
                     ['10000', '101'], ['9' * 100000, '1'],
                     ['no-number', '24'], ['80', '24', 'extra']):
            with self.subTest(args=[value[:20] for value in args]):
                process = subprocess.Popen(
                    [sys.executable, '-B', str(SCRIPTS / 'logging_filter.py')] + args,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.assertEqual(process.wait(timeout=3), 2)
                    self.assertEqual(process.stdout.read(), b'')
                    error = process.stderr.read()
                    self.assertIn(b'tmux-logging:', error)
                    self.assertNotIn(b'Traceback', error)
                    self.assertLess(len(error), 200)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    process.stdin.close()
                    process.stdout.close()
                    process.stderr.close()


class TmuxDimensionsTests(unittest.TestCase):
    def responses(self, cols='80', rows='4', alternate=False):
        metadata = dict.fromkeys(tmux_logging.FIELDS, '0')
        metadata.update(pane_width=cols, pane_height=rows, history_limit='2000',
                        pane_tabs='8,16', window_id='@0', scroll_region_lower='3',
                        alternate_on=str(int(alternate)))
        return [[('|'.join(metadata[key] for key in tmux_logging.FIELDS)).encode()],
                [b'- visible'] * 4, [b'- saved'] * 4 if alternate else [], []]

    def recording(self):
        recording = tmux_logging.Recording.__new__(tmux_logging.Recording)
        recording.output = io.StringIO()
        recording.screen = logging_filter.Screen(80, 4, recording.output)
        recording.stream = logging_filter.StreamProcessor(recording.screen)
        recording.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        recording.events = deque()
        recording.available = recording.consumed = recording.observed = 0
        recording.final_count = None
        recording.ready = True
        recording.pane, recording.window = '%0', '@0'
        recording.rebase_responses = None
        recording.rebase_waiting = recording.size_error = False
        recording.control = SimpleNamespace(stdin=io.BytesIO())
        return recording

    def test_invalid_bootstrap_is_rejected_before_capture_decoding(self):
        for cols, rows in (('9' * 100000, '4'), ('10000', '101'), ('80', '0')):
            with self.subTest(cols=cols[:20], rows=rows):
                with mock.patch.object(tmux_logging, 'capture_rows') as capture:
                    with self.assertRaises(logging_filter.ScreenSizeError):
                        tmux_logging.snapshot_from_responses(self.responses(cols, rows))
                    capture.assert_not_called()

    def test_invalid_saved_screen_is_rejected_before_capture_decoding(self):
        responses = self.responses(alternate=True)
        responses[2] = mock.MagicMock()
        responses[2].__len__.return_value = 10001
        with mock.patch.object(tmux_logging, 'capture_rows') as capture:
            with self.assertRaises(logging_filter.ScreenSizeError):
                tmux_logging.snapshot_from_responses(responses)
            capture.assert_not_called()

    def test_start_rejects_size_before_fork_or_pipe_installation(self):
        result = SimpleNamespace(stdout=b'/tmp/socket\n%0\n$0\n3.7c\n10000\n101\n')
        with mock.patch.object(tmux_logging.subprocess, 'run', return_value=result), \
                mock.patch.object(tmux_logging.subprocess, 'Popen') as child, \
                mock.patch.object(tmux_logging.os, 'pipe') as pipe:
            with self.assertRaises(logging_filter.ScreenSizeError):
                tmux_logging.start('/unused/log')
            child.assert_not_called()
            pipe.assert_not_called()

    def test_live_invalid_layout_preserves_preceding_byte_gated_output(self):
        recording = self.recording()
        protocol = tmux_logging.Protocol(recording.receive)
        protocol.feed(b'%output %0 BEFORE\n'
                      b'%layout-change @0 10000x101,0,0,0 10000x101,0,0,0\n'
                      b'%output %0 AFTER\n')
        recording.available = 3
        recording.drain()
        self.assertEqual(recording.consumed, 3)
        recording.available = len(b'BEFORE')
        with self.assertRaises(logging_filter.ScreenSizeError):
            recording.drain()
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'BEFORE\n')
        self.assertEqual(recording.control.stdin.getvalue(), b'')

    def test_size_error_after_valid_resize_does_not_wait_for_snapshot(self):
        recording = self.recording()
        recording.receive('notification', b'%layout-change @0 10x4,0,0,0 10x4,0,0,0')
        recording.receive('notification', b'%output %0 preserved')
        recording.receive('notification', b'%layout-change @0 10000x101,0,0,0 10000x101,0,0,0')
        recording.available = len(b'preserved')
        with self.assertRaises(logging_filter.ScreenSizeError):
            recording.drain()
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'preserved\n')

    def test_live_invalid_snapshot_preserves_preceding_output(self):
        recording = self.recording()
        recording.events.append(('resize', (10, 4)))
        recording.events.append(('output', b'preserved'))
        recording.rebase_responses = []
        recording.rebase_waiting = True
        for response in self.responses('10000', '101'):
            recording.receive('response', response)
        recording.available = len(b'preserved')
        with self.assertRaises(logging_filter.ScreenSizeError):
            recording.drain()
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'preserved\n')

    def test_relay_exits_when_worker_closes_socket_even_if_pane_is_idle(self):
        with tempfile.TemporaryDirectory(prefix='logging-relay-dimensions-') as directory:
            address = str(Path(directory) / 'relay.sock')
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(address)
                listener.listen(1)
                listener.settimeout(3)
                process = subprocess.Popen(
                    [sys.executable, '-B', str(SCRIPTS / 'tmux_logging.py'), 'relay', address],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    peer, _ = listener.accept()
                    peer.close()
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(process.stderr.read(), b'')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    process.stdin.close()
                    process.stdout.close()
                    process.stderr.close()


@unittest.skipUnless(sync_tests.supported_tmux(), 'requires tmux 3.7 or newer')
class RealTmuxDimensionsTests(unittest.TestCase):
    setUp = sync_tests.TmuxSynchronizationTests.setUp
    tmux = sync_tests.TmuxSynchronizationTests.tmux
    wait_for = sync_tests.TmuxSynchronizationTests.wait_for
    worker_command = sync_tests.TmuxSynchronizationTests.worker_command
    create_pane = sync_tests.TmuxSynchronizationTests.create_pane
    dimensions = sync_tests.TmuxSynchronizationTests.dimensions
    capture = sync_tests.TmuxSynchronizationTests.capture
    visible = sync_tests.TmuxSynchronizationTests.visible
    emit = sync_tests.TmuxSynchronizationTests.emit
    resize = sync_tests.TmuxSynchronizationTests.resize
    cleanup_server = sync_tests.TmuxSynchronizationTests.cleanup_server
    start_recording = sync_tests.TmuxSynchronizationTests.start_recording

    def test_oversized_start_leaves_file_and_pane_pipe_untouched(self):
        self.create_pane(cols=10000, rows=101)
        server_pid = self.tmux('display-message', '-p', '#{pid}').decode().strip()
        env = dict(os.environ, TMUX=f'{self.socket},{server_pid},0', TMUX_PANE=self.pane)
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPTS / 'tmux_logging.py'), 'start',
             str(self.log), self.pane], env=env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'screen size exceeds', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.tmux('display-message', '-p', '#{pane_pipe}').strip(), b'0')

    def test_oversized_live_resize_preserves_output_and_releases_idle_relay(self):
        self.create_pane(cols=80, rows=4)
        self.start_recording()
        relay_pid = int(self.tmux('display-message', '-p', '#{pane_pipe_pid}'))
        self.emit(b'PENDING BEFORE LIMIT', expected='PENDING BEFORE LIMIT')
        self.resize(10000, 101)
        self.wait_for(lambda: not self.tmux('list-clients', '-F', '#{client_name}'),
                      'worker shutdown after size limit')

        def relay_stopped():
            try:
                os.kill(relay_pid, 0)
            except ProcessLookupError:
                return True
            return False

        self.wait_for(relay_stopped, 'owned idle relay process exit')
        self.assertEqual(self.log.read_text(), 'PENDING BEFORE LIMIT\n')
        # Ownership cleanup releases an idle pipe explicitly; tmux's own
        # EV_WRITE notification would only notice reader exit on later output.
        self.assertEqual(self.tmux('display-message', '-p', '#{pane_pipe}').strip(), b'0')
        self.emit(b'AFTER LIMIT')
        self.wait_for(lambda: self.tmux('display-message', '-p', '#{pane_pipe}').strip() == b'0',
                      'tmux observes its closed output pipe')
        self.assertEqual(self.log.read_text(), 'PENDING BEFORE LIMIT\n')


if __name__ == '__main__':
    unittest.main()
