"""Terminal edits preserve the final cells and match a real raw-output tmux."""

import io
import unittest

import test_logging_filter_chunks as chunks
import test_tmux_logging_sync as integration


logging_filter = chunks.logging_filter
ESC = '\x1b'
CSI = ESC + '['
SEED = '\r\n'.join('ABCDEF')


def emulate(data, cols=10, rows=6, newline_mode=False):
    output = io.StringIO()
    screen = logging_filter.Screen(cols, rows, output)
    screen._newline_mode = newline_mode
    processor = logging_filter.StreamProcessor(screen)
    processor.feed(data)
    processor.finish()
    return screen, output


def visible_rows(screen):
    return [row.rstrip(' ') for row in screen._buf]


class EditingTests(unittest.TestCase):
    def test_audit_examples(self):
        cases = (
            ('abcd' + CSI + '2G' + CSI + 'P', 10, 'acd\n'),
            ('abc' + CSI + '2G' + CSI + '1K', 10, '  c\n'),
            (CSI + '?7labcdef', 4, 'abcf\n'),
            ('abcd' + CSI + 'AX', 4, 'abcX\n'),
            ('abc\nX', 10, 'abc\n   X\n'),
            ('abc' + ESC + 'EX', 10, 'abc\nX\n'),
        )
        for data, cols, expected in cases:
            with self.subTest(data=data):
                screen, output = emulate(data, cols=cols)
                screen.flush_all()
                self.assertEqual(output.getvalue(), expected)

    def test_character_edits_default_zero_counts_and_clipping(self):
        cases = (
            ('@', 'ab cdef'), ('0@', 'ab cdef'), ('2@', 'ab  cdef'),
            # tmux only blanks cells actually moved by ICH; a count spanning
            # the remaining width has no cells to move and leaves it intact.
            ('7@', 'ab def   c'), ('999999@', 'abcdef'),
            ('P', 'abdef'), ('0P', 'abdef'), ('2P', 'abef'),
            ('999999P', 'ab'),
            ('X', 'ab def'), ('0X', 'ab def'), ('2X', 'ab  ef'),
            ('999999X', 'ab'),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                screen, output = emulate('abcdef' + CSI + '3G' + CSI + command)
                self.assertEqual(visible_rows(screen)[0], expected)
                self.assertEqual((screen._c, screen._r), (2, 0))
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(len(screen._lines), screen.ROWS)

    def test_erase_before_cursor_is_inclusive(self):
        screen, output = emulate('abc\r\ndef' + CSI + '2;2H' + CSI + '1J')
        self.assertEqual(visible_rows(screen), ['', '  f', '', '', '', ''])
        self.assertEqual((screen._c, screen._r), (1, 1))
        self.assertEqual(output.getvalue(), '')
        screen, _ = emulate('abcdef' + CSI + '3G' + CSI + '1K')
        self.assertEqual(visible_rows(screen)[0], '   def')

    def test_line_edits_preserve_margins_and_cursor(self):
        cases = (
            (3, 'L', ['A', 'B', '', 'C', 'E', 'F']),
            (3, '2L', ['A', 'B', '', '', 'E', 'F']),
            (3, '999999L', ['A', 'B', '', '', 'E', 'F']),
            (3, 'M', ['A', 'B', 'D', '', 'E', 'F']),
            (3, '2M', ['A', 'B', '', '', 'E', 'F']),
            (3, '999999M', ['A', 'B', '', '', 'E', 'F']),
            # tmux applies IL/DL to the rest of the screen outside margins.
            (1, 'L', ['', 'A', 'B', 'C', 'D', 'E']),
            (1, '4L', ['', '', 'C', 'D', 'A', 'B']),
            (1, '999999L', ['A', 'B', 'C', 'D', 'E', 'F']),
            (1, 'M', ['B', 'C', 'D', 'E', 'F', '']),
            (5, 'L', ['A', 'B', 'C', 'D', '', 'E']),
            (5, 'M', ['A', 'B', 'C', 'D', 'F', '']),
        )
        for row, command, expected in cases:
            with self.subTest(row=row, command=command):
                data = SEED + CSI + '2;4r' + CSI + f'{row};3H' + CSI + command
                screen, output = emulate(data)
                self.assertEqual(visible_rows(screen), expected)
                self.assertEqual((screen._c, screen._r), (2, row - 1))
                self.assertEqual(output.getvalue(), '')

    def test_relative_vertical_motion_outside_scrolling_margins(self):
        cases = ((1, 'A', 1), (2, '99A', 1), (4, '99A', 3),
                 (6, '99A', 3), (1, '99B', 4), (3, '99B', 4),
                 (5, 'B', 6), (6, '99B', 6))
        for row, command, expected_row in cases:
            with self.subTest(row=row, command=command):
                screen, _ = emulate(CSI + '3;4r' + CSI + f'{row};3H' + CSI + command)
                self.assertEqual((screen._c, screen._r), (2, expected_row - 1))

    def test_vertical_motion_cancels_pending_wrap(self):
        for motion in ('A', 'B', 'G', 'H'):
            with self.subTest(motion=motion):
                screen, _ = emulate('abcdefghij' + CSI + motion + 'X')
                self.assertEqual(screen._r, 1 if motion == 'B' else 0)
                self.assertEqual(screen._c, 10 if motion in ('A', 'B') else 1)
                self.assertEqual(visible_rows(screen)[screen._r],
                                 'abcdefghiX' if motion == 'A' else
                                 '         X' if motion == 'B' else 'Xbcdefghij')

    def test_linefeed_modes_and_nel(self):
        for newline_mode, expected in ((False, ['abc', '   X']),
                                       (True, ['abc', 'X'])):
            with self.subTest(newline_mode=newline_mode):
                screen, _ = emulate('abc\nX', newline_mode=newline_mode)
                self.assertEqual(visible_rows(screen)[:2], expected)
                screen, _ = emulate('abc' + ESC + 'EX', newline_mode=newline_mode)
                self.assertEqual(visible_rows(screen)[:2], ['abc', 'X'])
        # Raw LF keeps tmux's pending wrap; NEL explicitly resets the column.
        screen, _ = emulate('abcdefghij\nX')
        self.assertEqual(visible_rows(screen)[:3], ['abcdefghij', '', 'X'])
        screen, _ = emulate('abcdefghij' + ESC + 'EX')
        self.assertEqual(visible_rows(screen)[:2], ['abcdefghij', 'X'])

    def test_scroll_region_default_bottom_and_invalid_region(self):
        screen, _ = emulate(CSI + '2r')
        self.assertEqual((screen._scroll_top, screen._scroll_bot), (1, 5))
        for invalid in ('3;3r', '5;2r', '999;999r'):
            with self.subTest(invalid=invalid):
                screen, _ = emulate(CSI + '2;5r' + CSI + '3;4H' + CSI + invalid)
                self.assertEqual((screen._scroll_top, screen._scroll_bot), (1, 4))
                self.assertEqual((screen._c, screen._r), (3, 2))

    def test_edits_to_recorded_rows_keep_the_complete_new_revision(self):
        cases = (('P', 'acd'), ('@', 'a bcd'), ('X', 'a cd'),
                 ('1K', '  cd'), ('K', 'a'))
        for edit, expected in cases:
            with self.subTest(edit=edit):
                output = io.StringIO()
                screen = logging_filter.Screen(10, 3, output)
                screen.load_snapshot(['two', 'three', 'four'],
                                     history=['abcd'], cursor=(0, 2))
                screen.resize(10, 4)
                self.assertEqual(visible_rows(screen), ['abcd', 'two', 'three', 'four'])
                stream = logging_filter.StreamProcessor(screen)
                stream.feed(CSI + '1;2H' + CSI + edit)
                self.assertEqual(output.getvalue(), '')
                screen.flush_all()
                self.assertEqual(output.getvalue(), expected + '\ntwo\nthree\nfour\n')

    def test_edit_sequences_survive_every_input_boundary(self):
        cases = (
            ('abcdef' + CSI + '3G' + CSI + '2P' + CSI + '@X' + ESC + 'Enext',
             'abXef\nnext\n'),
            ('one\nthree' + CSI + '2;1H' + CSI + 'Ltwo' + CSI + '3;1H' + CSI + 'M',
             'one\ntwo\n'),
        )
        for text, expected in cases:
            chunks.ChunkTests.assert_chunkings(self, text.encode(), expected)


@unittest.skipUnless(integration.supported_tmux(), 'requires tmux 3.7 or newer')
class RealTmuxEditingTests(unittest.TestCase):
    setUp = integration.TmuxSynchronizationTests.setUp
    tmux = integration.TmuxSynchronizationTests.tmux
    wait_for = integration.TmuxSynchronizationTests.wait_for
    worker_command = integration.TmuxSynchronizationTests.worker_command
    create_pane = integration.TmuxSynchronizationTests.create_pane
    dimensions = integration.TmuxSynchronizationTests.dimensions
    capture = integration.TmuxSynchronizationTests.capture
    visible = integration.TmuxSynchronizationTests.visible
    emit = integration.TmuxSynchronizationTests.emit
    resize = integration.TmuxSynchronizationTests.resize
    cleanup_server = integration.TmuxSynchronizationTests.cleanup_server

    def assert_matches_tmux(self, cases, cols=10, rows=6, resize_to=None):
        self.create_pane(cols, rows)
        reset = (CSI + 'r' + CSI + '?6l' + CSI + '?7h' + CSI + '4l'
                 + CSI + 'H' + CSI + '2J' + CSI + '3J')
        for index, data in enumerate(cases):
            with self.subTest(data=data):
                if resize_to is not None:
                    self.resize(cols, rows)
                marker = f'editing-case-{index}'
                self.emit((reset + data + ESC + ']2;' + marker + '\x07').encode())
                self.wait_for(lambda: self.tmux('display-message', '-p', '-t', self.pane,
                              '#{pane_title}').decode().strip() == marker,
                              'terminal edit completion')
                screen, _ = emulate(data, cols, rows)
                if resize_to is not None:
                    self.resize(resize_to, rows)
                    screen.resize(resize_to, rows)
                self.assertEqual(visible_rows(screen), self.capture().splitlines())
                cursor = tuple(map(int, self.tmux('display-message', '-p', '-t', self.pane,
                                   '#{cursor_x} #{cursor_y}').split()))
                self.assertEqual((screen._c, screen._r), cursor)

    def test_character_edits_match_tmux(self):
        cases = ['abcdef' + CSI + '3G' + CSI + command
                 for command in ('@', '0@', '2@', '7@', '999999@', 'P', '0P', '2P',
                                 '999999P', 'X', '0X', '2X', '999999X')]
        cases += ['abcdefghij' + CSI + command + 'X' for command in ('@', 'P', 'X')]
        cases += ['abcdefghij' + CSI + '9G' + CSI + '4@',
                  'abcdefghij' + CSI + '10G' + CSI + '999999@',
                  'abcdef' + CSI + '3G' + CSI + '4hXY' + CSI + '4lZ']
        self.assert_matches_tmux(cases)

    def test_line_edits_match_tmux_inside_and_outside_margins(self):
        self.assert_matches_tmux([
            SEED + CSI + '2;4r' + CSI + f'{row};3H' + CSI + command
            for row in (1, 3, 5) for command in ('L', '0L', '4L', '999999L',
                                                'M', '0M', '999999M')])

    def test_erase_includes_cursor_and_preserves_other_rows(self):
        cases = ['abcdef' + CSI + '3G' + CSI + command
                 for command in ('K', '1K', '2K')]
        cases += [SEED + CSI + '3;1H' + CSI + command for command in ('J', '1J')]
        self.assert_matches_tmux(cases)

    def test_motion_and_pending_wrap_match_tmux(self):
        cases = [CSI + '3;4r' + CSI + f'{row};3H' + CSI + command + 'X'
                 for row in (1, 2, 3, 4, 5, 6) for command in ('99A', '99B')]
        cases += ['abcdefghij' + CSI + motion + 'X' for motion in ('A', 'B', 'G', 'H')]
        self.assert_matches_tmux(cases)

    def test_raw_linefeed_nel_and_scroll_region_match_tmux(self):
        self.assert_matches_tmux([
            'abc\nX', 'abc' + ESC + 'EX', 'abcdefghij\nX',
            'abcdefghij' + ESC + 'EX',
            SEED + CSI + '2r' + CSI + '6;1H\r\nLAST',
            CSI + '2;5r' + CSI + '3;4H' + CSI + '3;3rX',
            CSI + '2;4r' + CSI + '?6h' + CSI + '3;5rX',
        ])

    def test_edited_wrapped_rows_keep_blank_cells_when_widened(self):
        self.assert_matches_tmux([
            'abcdefghijklmno' + CSI + '1;3H' + CSI + command
            for command in ('P', '2P', '@', '2@', 'X', 'K')], resize_to=16)

    def test_edited_wrapped_rows_keep_blank_cells_when_narrowed(self):
        self.assert_matches_tmux([
            'abcdefghijklmno' + CSI + '1;3H' + CSI + command
            for command in ('P', '2P', '@', '2@', 'X', 'K')], resize_to=6)


if __name__ == '__main__':
    unittest.main()
