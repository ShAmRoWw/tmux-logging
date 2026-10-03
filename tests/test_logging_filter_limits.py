"""Oversized CSI input is bounded, discarded atomically, and recoverable."""

import io
import signal
import tracemalloc
import unittest
from unittest import mock

import test_logging_filter_chunks as chunks
import test_logging_filter_editing as editing
import test_tmux_logging_sync as integration


logging_filter = chunks.logging_filter
CSI = '\x1b['
MAX_VALUE = 2147483647
MAX_PARAMS = 23
MAX_PARAMETER_BYTES = 63


class CsiLimitsTests(unittest.TestCase):
    def screen(self, cols=80, rows=6):
        output = io.StringIO()
        screen = logging_filter.Screen(cols, rows, output)
        return screen, logging_filter.StreamProcessor(screen), output

    def logged(self, data):
        screen, stream, output = self.screen()
        stream.feed(data)
        stream.finish()
        screen.flush_all()
        return output.getvalue()

    def test_thousands_of_digits_never_reach_unbounded_int_conversion(self):
        for digits in ('9' * 4301, '0' * 4301):
            for command in ('H', 'S', '?1049;' + digits + 'h'):
                sequence = CSI + (command if command.endswith('h') else digits + command)
                with self.subTest(digits=digits[0], command=command[-1]):
                    self.assertEqual(self.logged('before' + sequence + 'after'),
                                     'beforeafter\n')

    def test_direct_dispatch_rejects_oversized_parameters(self):
        for raw in ('9' * 4301, '0' * 4301 + '1', str(MAX_VALUE + 1)):
            with self.subTest(parameter_length=len(raw)):
                screen, _, output = self.screen()
                logging_filter.process('abc' + CSI + raw + 'DX', screen)
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'abcX\n')

    def test_integer_and_parameter_byte_boundaries(self):
        cases = (
            (str(MAX_VALUE), 'Xbc\n'),
            (str(MAX_VALUE + 1), 'abcX\n'),
            ('0' * (MAX_PARAMETER_BYTES - 1) + '1', 'abX\n'),
            ('0' * MAX_PARAMETER_BYTES + '1', 'abcX\n'),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.logged('abc' + CSI + raw + 'DX'), expected)

    def test_parameter_count_boundary(self):
        for count, expected in ((MAX_PARAMS, 'Xbc\n'),
                                (MAX_PARAMS + 1, 'abcX\n')):
            with self.subTest(count=count):
                parameters = ';'.join('1' for _ in range(count))
                self.assertEqual(self.logged('abc' + CSI + parameters + 'HX'), expected)

    def test_invalid_mode_sequence_has_no_partial_effect(self):
        invalid = (
            '?1049;' + str(MAX_VALUE + 1),
            '?1049;' + ';'.join('0' for _ in range(MAX_PARAMS)),
            '4;20;' + str(MAX_VALUE + 1),
            '?1049;' + '0' * (MAX_PARAMETER_BYTES + 1),
        )
        for parameters in invalid:
            with self.subTest(parameters=parameters):
                screen, stream, output = self.screen()
                screen._newline_mode = False
                stream.feed('before' + CSI + parameters + 'hafter')
                self.assertFalse(screen._in_alt)
                self.assertFalse(screen._insert)
                self.assertFalse(screen._newline_mode)
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'beforeafter\n')

    def test_malformed_private_prefixes_and_private_ordinary_commands_are_ignored(self):
        for command in ('??1049h', '10?49h', '?7?l', '1?2H', '?2J', '?100S', '?7D'):
            with self.subTest(command=command):
                screen, stream, output = self.screen()
                stream.feed('before' + CSI + command + 'after')
                self.assertFalse(screen._in_alt)
                self.assertTrue(screen._autowrap)
                self.assertEqual(output.getvalue(), '')
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'beforeafter\n')

    def test_megabytes_of_unfinished_csi_have_bounded_peak_memory(self):
        screen, stream, output = self.screen()
        stream.feed('before' + CSI)
        block = '0' * 4096
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            before, _ = tracemalloc.get_traced_memory()
            for _ in range(512):
                stream.feed(block)
            _, peak = tracemalloc.get_traced_memory()
            self.assertLess(peak - before, 512 * 1024,
                            'CSI storage grew with the 2 MiB unfinished input')
        finally:
            if not already_tracing:
                tracemalloc.stop()
        # A suffix that looks like a supported erase must still belong to the
        # rejected sequence, and no discarded parameter text may become text.
        stream.feed(';2Jafter')
        self.assertEqual(output.getvalue(), '')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'beforeafter\n')

    def test_oversized_csi_recovers_at_escape_cancel_or_substitute(self):
        endings = (('\x1b[2DX', 'abXdef\n'),
                   ('\x18after', 'abcdefafter\n'),
                   ('\x1aafter', 'abcdefafter\n'))
        for ending, expected in endings:
            with self.subTest(ending=ending):
                # Cursor is at column four when the unsupported input starts.
                prefix = 'abcdef' + CSI + '5G' if ending.startswith('\x1b') else 'abcdef'
                self.assertEqual(self.logged(prefix + CSI + '0' * 10000 + ending), expected)

    def test_discarded_sequence_still_executes_c0_and_ignores_del(self):
        screen, stream, output = self.screen()
        stream.feed('abcdef' + CSI + '0' * 10000 + '\r\x7f' + '0' * 100 + 'mX')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'Xbcdef\n')

    def test_non_ascii_and_del_do_not_restart_csi_or_leak_into_text(self):
        cases = (
            ('abc' + CSI + '1中\x7f文2DX', 'Xbc\n'),
            ('abc' + CSI + '0' * 100 + '中\x7f文2Jafter', 'abcafter\n'),
            ('abc' + CSI + ' ' * 4 + '2Jafter', 'abcafter\n'),
        )
        for data, expected in cases:
            with self.subTest(data=data):
                chunks.ChunkTests.assert_chunkings(self, data.encode(), expected)

    def test_finish_discards_oversized_tail_and_allows_reuse(self):
        screen, stream, output = self.screen()
        stream.feed('before' + CSI + '0' * 10000)
        stream.finish()
        stream.feed('after')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'beforeafter\n')

    def test_overflow_and_malformed_sequences_are_chunk_invariant(self):
        commands = (str(MAX_VALUE + 1) + 'H', '?1049;' + str(MAX_VALUE + 1) + 'h',
                    '?7?l', '1?2H', '1;' * MAX_PARAMS + '1H')
        for command in commands:
            with self.subTest(command=command):
                chunks.ChunkTests.assert_chunkings(
                    self, ('before' + CSI + command + 'after').encode(), 'beforeafter\n')
        data = 'before' + CSI + '0' * 4301 + '2Jafter'
        for boundary in (1, 7, 8, 63, 64, 69, 70, 1024, 4096, len(data) - 1):
            with self.subTest(boundary=boundary):
                screen, stream, output = self.screen()
                stream.feed(data[:boundary])
                stream.feed(data[boundary:])
                stream.finish()
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'beforeafter\n')

    def test_large_valid_scroll_counts_execute_only_region_height_steps(self):
        for command, method in (('S', '_scroll_up'), ('T', '_scroll_down')):
            for region, maximum in (('', 6), (CSI + '2;4r', 3)):
                with self.subTest(command=command, region=region):
                    screen, stream, _ = self.screen()
                    stream.feed('\r\n'.join('ABCDEF') + region)
                    calls = []
                    original = getattr(screen, method)

                    def bounded_scroll():
                        calls.append(None)
                        self.assertLessEqual(len(calls), maximum,
                                             'scroll work followed the numeric count')
                        original()

                    with mock.patch.object(screen, method, bounded_scroll):
                        stream.feed(CSI + str(MAX_VALUE) + command)
                    self.assertEqual(len(calls), maximum)
                    self.assertEqual(len(screen._lines), screen.ROWS)

    def test_invalid_scroll_regions_do_not_break_later_editing(self):
        for parameters in ('5;2', '3;3', '1;0', ';0', str(MAX_VALUE) + ';' + str(MAX_VALUE),
                           '2;' + str(MAX_VALUE + 1), '9' * 4301 + ';1'):
            with self.subTest(parameters=parameters):
                screen, stream, _ = self.screen()
                stream.feed(CSI + '2;5r' + CSI + parameters + 'r'
                            + CSI + 'S' + CSI + 'T' + CSI + 'L' + CSI + 'Mtext')
                self.assertEqual((screen._scroll_top, screen._scroll_bot), (1, 4))
                self.assertEqual(len(screen._lines), screen.ROWS)
                self.assertTrue(any('text' in row for row in screen._buf))


class CsiLimitsCliTests(unittest.TestCase):
    start_filter = chunks.ChunkCliTests.start_filter
    cleanup_child = staticmethod(chunks.ChunkCliTests.cleanup_child)
    read_until = chunks.ChunkCliTests.read_until
    finish = chunks.ChunkCliTests.finish

    def test_unfinished_oversized_csi_preserves_text_at_eof_term_and_hup(self):
        for number in (None, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                child = self.start_filter()
                data = b'visible\x1b[' + b'0' * 4301
                self.assertEqual(child.stdin.write(data), len(data))
                self.read_until(child.stderr, b'READ\n')
                if number is not None:
                    child.send_signal(number)
                    # stdin remains open, so termination must use signal drain.
                    child.wait(timeout=chunks.TIMEOUT)
                self.assertEqual(self.finish(child), b'visible\n')

    def test_completed_oversized_number_does_not_crash_the_filter(self):
        child = self.start_filter()
        data = b'before\x1b[' + b'9' * 4301 + b'Hafter'
        self.assertEqual(child.stdin.write(data), len(data))
        self.assertEqual(self.finish(child), b'beforeafter\n')


@unittest.skipUnless(integration.supported_tmux(), 'requires tmux 3.7 or newer')
class RealTmuxCsiLimitsTests(unittest.TestCase):
    setUp = editing.RealTmuxEditingTests.setUp
    tmux = editing.RealTmuxEditingTests.tmux
    wait_for = editing.RealTmuxEditingTests.wait_for
    worker_command = editing.RealTmuxEditingTests.worker_command
    create_pane = editing.RealTmuxEditingTests.create_pane
    dimensions = editing.RealTmuxEditingTests.dimensions
    capture = editing.RealTmuxEditingTests.capture
    visible = editing.RealTmuxEditingTests.visible
    emit = editing.RealTmuxEditingTests.emit
    cleanup_server = editing.RealTmuxEditingTests.cleanup_server
    assert_matches_tmux = editing.RealTmuxEditingTests.assert_matches_tmux

    def test_numeric_parameter_boundaries_match_tmux(self):
        cases = ['abc' + CSI + raw + 'DX' for raw in (
            str(MAX_VALUE), str(MAX_VALUE + 1),
            '0' * (MAX_PARAMETER_BYTES - 1) + '1',
            '0' * MAX_PARAMETER_BYTES + '1')]
        cases += ['abc' + CSI + ';'.join('1' for _ in range(count)) + 'HX'
                  for count in (MAX_PARAMS, MAX_PARAMS + 1)]
        cases += ['main' + CSI + '?1049;' + str(MAX_VALUE + 1) + 'hafter',
                  CSI + '?7?labcdefghijk']
        self.assert_matches_tmux(cases)


if __name__ == '__main__':
    unittest.main()
