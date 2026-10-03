"""Nonvisible terminal strings stay hidden across reads and shutdown."""

import io
import signal
import subprocess
import sys
import tempfile
import tracemalloc
import unittest

import test_logging_filter_chunks as chunks
import test_tmux_logging_sync as sync


class ControlStringTests(unittest.TestCase):
    assert_chunkings = chunks.ChunkTests.assert_chunkings

    def test_dcs_payload_never_becomes_screen_text(self):
        for payload in ('ignored-payload', '1;2$qhidden', '+q746e',
                        'zsecret\r\n\t\x07\x1b[2Jhidden',
                        'zскрытый текст'):
            with self.subTest(payload=payload):
                data = ('before\x1bP' + payload + chunks.ST + 'after').encode()
                self.assert_chunkings(data, 'beforeafter\n', (0, 11))

    def test_dcs_escaped_escape_cannot_end_the_payload(self):
        self.assert_chunkings(
            'before\x1bPzhidden\x1b\x1b\\still-hidden' + chunks.ST + 'after',
            'beforeafter\n', (0, 11),
        )

    def test_dcs_header_can_be_cancelled(self):
        for header in ('\x1bP', '\x1bP12;', '\x1bP$', '\x1bP:', '\x1bP12<'):
            for cancel in ('\x18', '\x1a'):
                with self.subTest(header=header, cancel=cancel):
                    self.assert_chunkings('before' + header + cancel + 'after',
                                          'beforeafter\n', (0, 11))

    def test_tmux_keeps_can_and_sub_inside_dcs_payload(self):
        for cancel in ('\x18', '\x1a'):
            with self.subTest(cancel=cancel):
                self.assert_chunkings(
                    'before\x1bPzhidden' + cancel + 'still-hidden' + chunks.ST + 'after',
                    'beforeafter\n', (0, 11),
                )

    def test_escape_in_a_dcs_header_starts_a_fresh_sequence(self):
        self.assert_chunkings('before\x1bP12;\x1b[31mafter',
                              'beforeafter\n', (0, 11))
        self.assert_chunkings('before\x1bP:ignored\x1b[31mafter',
                              'beforeafter\n', (0, 11))

    def test_sos_pm_and_apc_ignore_bel_and_accept_cancellation(self):
        for introducer in ('X', '^', '_'):
            for terminator in (chunks.ST, '\x18', '\x1a', '\x1b[31m'):
                with self.subTest(introducer=introducer, terminator=terminator):
                    self.assert_chunkings(
                        'before\x1b' + introducer + 'hidden\x07hidden' + terminator + 'after',
                        'beforeafter\n', (0, 11),
                    )

    def test_adjacent_strings_and_existing_osc_keep_intervening_text(self):
        self.assert_chunkings(
            'a\x1bPzhidden' + chunks.ST + 'b\x1b_hidden' + chunks.ST
            + 'c\x1b]0;title\x07d\x1b^hidden' + chunks.ST
            + 'e\x1bXhidden' + chunks.ST + 'f\x1b[5 qg',
            'abcdefg\n', (0, 7),
        )

    def test_unterminated_strings_are_discarded_at_finish(self):
        for suffix in ('\x1bP', '\x1bP123;', '\x1bPzsecret',
                       '\x1bPzsecret\x1b', '\x1bXsecret',
                       '\x1b^secret', '\x1b_secret'):
            with self.subTest(suffix=suffix):
                self.assert_chunkings('visible' + suffix, 'visible\n', (0, 7))

    def test_long_unterminated_strings_do_not_accumulate_payload(self):
        payload = 'x' * 8192
        for prefix in ('\x1bPz', '\x1bP1;', '\x1bX', '\x1b^', '\x1b_'):
            with self.subTest(prefix=prefix):
                output = io.StringIO()
                screen = chunks.logging_filter.Screen(80, 4, output)
                processor = chunks.logging_filter.StreamProcessor(screen)
                processor.feed('visible' + prefix)
                tracemalloc.start()
                try:
                    for _ in range(128):
                        processor.feed(payload)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 256 * 1024)
                processor.finish()
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'visible\n')


class ControlStringCliTests(unittest.TestCase):
    start_filter = chunks.ChunkCliTests.start_filter
    cleanup_child = staticmethod(chunks.ChunkCliTests.cleanup_child)
    read_until = chunks.ChunkCliTests.read_until
    finish = chunks.ChunkCliTests.finish

    def test_partial_dcs_does_not_leak_at_eof_or_signal(self):
        for suffix in (b'\x1bPzhidden', b'\x1bPzhidden\x1b',
                       b'\x1bPzhidden \xe2\x82'):
            for number in (None, signal.SIGTERM, signal.SIGHUP):
                with self.subTest(suffix=suffix, signal=number):
                    child = self.start_filter()
                    child.stdin.write(b'visible' + suffix)
                    self.read_until(child.stderr, b'READ\n')
                    if number is not None:
                        child.send_signal(number)
                        child.wait(timeout=chunks.TIMEOUT)
                    self.assertEqual(self.finish(child), b'visible\n')

    def test_dcs_terminator_split_at_real_read_boundary(self):
        data = b'before\x1bPz' + b'x' * (4096 - len(b'before\x1bPz') - 1)
        data += b'\x1b\\after'
        with tempfile.TemporaryFile() as source:
            source.write(data)
            source.seek(0)
            result = subprocess.run(
                [sys.executable, '-B', str(chunks.FILTER), '80', '4'],
                stdin=source, capture_output=True, timeout=chunks.TIMEOUT,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, b'beforeafter\n')


@unittest.skipUnless(sync.supported_tmux(), 'requires tmux 3.7 or newer')
class TmuxControlStringTests(unittest.TestCase):
    setUp = sync.TmuxSynchronizationTests.setUp
    tmux = sync.TmuxSynchronizationTests.tmux
    wait_for = sync.TmuxSynchronizationTests.wait_for
    worker_command = sync.TmuxSynchronizationTests.worker_command
    create_pane = sync.TmuxSynchronizationTests.create_pane
    dimensions = sync.TmuxSynchronizationTests.dimensions
    capture = sync.TmuxSynchronizationTests.capture
    visible = sync.TmuxSynchronizationTests.visible
    emit = sync.TmuxSynchronizationTests.emit
    start_recording = sync.TmuxSynchronizationTests.start_recording
    stop_recording = sync.TmuxSynchronizationTests.stop_recording
    cleanup_server = sync.TmuxSynchronizationTests.cleanup_server

    def test_tmux_and_logger_agree_on_string_termination(self):
        self.create_pane(40, 6)
        self.start_recording()
        data = (b'before\x1bPzhidden\x18\x1a\x1b\x1b\\hidden\x1b\\'
                b'\x1bP123;\x18\x1b_hidden\x07hidden\x1b\\'
                b'\x1bXhidden\x1a\x1b^hidden\x1b[31mafter')
        self.emit(data, 'beforeafter')
        self.assertEqual(self.stop_recording(), 'beforeafter\n')


if __name__ == '__main__':
    unittest.main()
