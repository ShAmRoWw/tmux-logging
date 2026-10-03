"""Reflow must track tmux geometry without replaying already logged cells."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_logging_filter_chunks import logging_filter


class ResizeTests(unittest.TestCase):
    def screen(self, cols=10, rows=5):
        output = io.StringIO()
        screen = logging_filter.Screen(cols, rows, output)
        return screen, output

    def feed(self, screen, text):
        logging_filter.StreamProcessor(screen).feed(text)

    def test_widen_reflows_wrapped_lines_before_overwrite(self):
        screen, output = self.screen()
        self.feed(screen, 'ABCDEFGHIJKLMNO')
        screen.resize(20, 5)
        self.assertEqual((screen._buf, screen._c, screen._r),
                         (['ABCDEFGHIJKLMNO', '', '', '', ''], 15, 0))
        self.feed(screen, '\rREPLACED')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'REPLACEDIJKLMNO\n')

    def test_shrink_keeps_empty_bottom_rows_and_moves_cursor_out_of_history(self):
        screen, output = self.screen(20)
        self.feed(screen, 'ABCDEFGHIJKLMNO\rREPLACED')
        screen.resize(10, 5)
        self.assertEqual(screen._buf, ['KLMNO', '', '', '', ''])
        self.assertEqual((screen._c, screen._r), (0, 0))
        self.assertEqual(output.getvalue(), 'REPLACEDIJ\n')
        self.feed(screen, 'Z')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'REPLACEDIJ\nZLMNO\n')

    def test_height_growth_restores_history_without_duplicate_lines(self):
        screen, output = self.screen()
        self.feed(screen, 'one\ntwo\nthree\nfour\nfive')
        for _ in range(3):
            screen.resize(10, 3)
            self.assertEqual(screen._buf, ['three', 'four', 'five'])
            screen.resize(10, 5)
            self.assertEqual(screen._buf, ['one', 'two', 'three', 'four', 'five'])
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'one\ntwo\nthree\nfour\nfive\n')

    def test_height_shrink_discards_rows_below_cursor(self):
        screen, output = self.screen()
        self.feed(screen, 'one\ntwo\nthree\nfour\nfive\x1b[2;3H')
        screen.resize(10, 3)
        screen.resize(10, 5)
        self.assertEqual(screen._buf, ['one', 'two', 'three', '', ''])
        self.assertEqual((screen._c, screen._r), (2, 1))
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'one\ntwo\nthree\n')

    def test_pending_wrap_survives_widen_and_narrow(self):
        screen, _ = self.screen()
        self.feed(screen, '1234567890')
        screen.resize(20, 5)
        self.assertEqual((screen._c, screen._r), (10, 0))
        screen.resize(10, 5)
        self.assertEqual((screen._c, screen._r), (10, 0))
        self.feed(screen, 'X')
        self.assertEqual(screen._buf[:2], ['1234567890', 'X'])
        self.assertEqual((screen._c, screen._r), (1, 1))

    def test_reflow_retains_unrecorded_suffix_of_recorded_prefix(self):
        screen, output = self.screen(10, 2)
        self.feed(screen, 'ABCDEFGHIJKLMNOPQRSTUVWXY')
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\n')
        screen.resize(20, 2)
        self.assertEqual(screen._buf, ['ABCDEFGHIJKLMNOPQRST', 'UVWXY'])
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\nKLMNOPQRST\nUVWXY\n')

    def test_narrow_and_widen_does_not_duplicate_fragments(self):
        screen, output = self.screen(20)
        self.feed(screen, 'ABCDEFGHIJKLMNO')
        for _ in range(3):
            screen.resize(10, 5)
            screen.resize(20, 5)
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\nKLMNO\n')

    def test_edited_restored_row_is_a_new_revision(self):
        screen, output = self.screen(10, 2)
        self.feed(screen, 'old\ntwo\nthree')
        screen.resize(10, 3)
        self.feed(screen, '\x1b[1;1Hnew')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'old\nnew\ntwo\nthree\n')

    def test_height_change_resets_margins_width_change_preserves_them(self):
        screen, _ = self.screen()
        self.feed(screen, '\x1b[2;4r')
        screen.resize(20, 5)
        self.assertEqual((screen._scroll_top, screen._scroll_bot), (1, 3))
        screen.resize(20, 6)
        self.assertEqual((screen._scroll_top, screen._scroll_bot), (0, 5))

    def test_alternate_resize_reflows_saved_main_only_on_exit(self):
        screen, output = self.screen()
        self.feed(screen, 'ABCDEFGHIJKLMNO\x1b[?1049hPRIVATE')
        screen.resize(20, 5)
        self.assertEqual([row.text for row in screen._alt_state['_lines']],
                         ['ABCDEFGHIJ', 'KLMNO', '', '', ''])
        self.feed(screen, '\x1b[?1049l\rREPLACED')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'REPLACEDIJKLMNO\n')

    def test_shutdown_during_alternate_resize_keeps_main_and_excludes_private(self):
        screen, output = self.screen()
        self.feed(screen, 'ABCDEFGHIJKLMNO\x1b[?1049hPRIVATE')
        screen.resize(3, 2)
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\nKLMNO\n')

    def test_normal_scrolling_keeps_only_bounded_history(self):
        screen, output = self.screen(20, 2)
        screen._history_limit = 20
        for index in range(100):
            self.feed(screen, f'line-{index}\n')
        self.assertLessEqual(len(screen._history), 20)
        screen.resize(20, 10)
        screen.flush_all()
        self.assertEqual(output.getvalue(), ''.join(f'line-{index}\n' for index in range(100)))


# Helpers are reused explicitly so discovery runs every test once.
class BootstrapTests(unittest.TestCase):
    screen = ResizeTests.screen
    feed = ResizeTests.feed

    def test_startup_restores_existing_cells_and_cursor(self):
        screen, output = self.screen(20, 3)
        screen.load_snapshot(['first', 'prompt> OLDsuffix', ''], cursor=(8, 1))
        self.feed(screen, 'NEW')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'first\nprompt> NEWsuffix\n')

    def test_startup_preserves_pending_wrap_and_hard_line_boundaries(self):
        screen, _ = self.screen()
        screen.load_snapshot(['1234567890', 'NEXT', '', '', ''], cursor=(10, 0))
        screen.resize(20, 5)
        self.assertEqual(screen._buf[:2], ['1234567890', 'NEXT'])
        self.assertEqual((screen._c, screen._r), (10, 0))

    def test_startup_wrapped_rows_reflow_and_history_is_not_dumped(self):
        screen, output = self.screen(10, 2)
        screen.load_snapshot(['KLMNOPQRST', 'UVWXY'], cursor=(5, 1),
                             wrapped=[True, False], history=['ABCDEFGHIJ'],
                             history_wrapped=[True])
        self.assertEqual(output.getvalue(), '')
        screen.resize(20, 2)
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'KLMNOPQRST\nUVWXY\n')

    def test_startup_saved_main_keeps_its_own_geometry(self):
        screen, output = self.screen(20, 5)
        screen.load_snapshot(['PRIVATE'], cursor=(7, 0), alternate={
            'lines': ['ABCDEFGHIJ', 'KLMNO', '', '', ''],
            'wrapped': [True, False, False, False, False],
            'cursor': (5, 1), 'cols': 10, 'rows': 5,
        })
        self.feed(screen, '\x1b[?1049l\rREPLACED')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'REPLACEDIJKLMNO\n')

    def test_bootstrap_keeps_history_temporarily_expanded_by_tmux_reflow(self):
        screen, output = self.screen(10, 2)
        screen.load_snapshot(['current', 'last'], history=['older', 'newer'],
                             history_limit=1, history_scrolled=2, cursor=(4, 1))
        screen.resize(10, 4)
        self.assertEqual(screen._buf, ['older', 'newer', 'current', 'last'])
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'current\nlast\n')

    def test_initial_modes_are_used_and_can_be_changed_by_stream(self):
        screen, _ = self.screen(10, 3)
        screen.load_snapshot(['abcdefghij', '', ''], cursor=(9, 0), autowrap=False)
        self.feed(screen, 'X')
        self.assertEqual(screen._buf, ['abcdefghiX', '', ''])
        self.feed(screen, '\x1b[?7hY')
        self.assertEqual(screen._buf[:2], ['abcdefghiY', ''])
        screen.load_snapshot(['abcd', '', ''], cursor=(1, 0), insert=True)
        self.feed(screen, 'Z\x1b[4lQ')
        # tmux's insert operation retains cleared cells through the right edge.
        self.assertEqual(screen._buf[0], 'aZQcd     ')
        screen.load_snapshot(['', '', ''], scroll_region=(1, 2), origin=True,
                             tabs=[3, 7])
        self.feed(screen, '\x1b[1;1H\tX')
        self.assertEqual(screen._buf, ['', '   X', ''])


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class TmuxResizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='logging-resize-test-')
        self.addCleanup(self.temp.cleanup)
        self.socket = str(Path(self.temp.name) / 'socket')
        fifo = Path(self.temp.name) / 'input'
        os.mkfifo(fifo)
        self.writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, self.writer)
        self.command('new-session', '-d', '-x', '10', '-y', '5', '-s', 'test',
                     f'exec cat {fifo}')
        self.addCleanup(self.stop_server)
        self.command('set-option', '-g', 'status', 'off')
        self.command('set-option', '-g', 'window-size', 'manual')
        self.command('resize-window', '-x', '10', '-y', '5')
        self.screen = logging_filter.Screen(10, 5, io.StringIO())
        self.screen._newline_mode = False
        self.stream = logging_filter.StreamProcessor(self.screen)
        self.serial = 0

    def command(self, *args):
        return subprocess.check_output(['tmux', '-S', self.socket, *args],
                                       stderr=subprocess.STDOUT).decode()

    def stop_server(self):
        subprocess.run(['tmux', '-S', self.socket, 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, text):
        self.serial += 1
        title = f'resize-sync-{self.serial}'
        payload = text + '\x1b]0;' + title + '\x07'
        os.write(self.writer, payload.encode())
        deadline = time.monotonic() + 5
        while self.command('display-message', '-p', '#{pane_title}').strip() != title:
            self.assertLess(time.monotonic(), deadline, 'tmux did not consume output')
            time.sleep(.01)
        self.stream.feed(payload)

    def resize(self, cols, rows):
        self.command('resize-window', '-x', str(cols), '-y', str(rows))
        self.screen.resize(cols, rows)
        self.assert_screen()

    def assert_screen(self):
        actual = self.command('capture-pane', '-p').splitlines()
        self.assertEqual([row.rstrip() for row in self.screen._buf], actual)
        cursor = self.command('display-message', '-p', '#{cursor_x},#{cursor_y}').strip()
        self.assertEqual(f'{self.screen._c},{self.screen._r}', cursor)

    def test_reflow_matches_real_tmux_overwrite_and_pending_wrap(self):
        self.write('ABCDEFGHIJKLMNO')
        self.resize(20, 5)
        self.write('\rREPLACED')
        self.assert_screen()
        self.resize(10, 5)
        self.write('12345\r\n1234567890')
        self.resize(20, 5)
        self.resize(10, 5)
        self.write('X')
        self.assert_screen()

    def test_height_cycles_and_combined_resize_match_real_tmux(self):
        self.write('one\r\ntwo\r\nthree\r\nfour\r\nfive')
        self.resize(10, 3)
        self.resize(10, 5)
        self.write('\x1b[2;3H')
        self.resize(10, 3)
        self.resize(10, 5)
        self.write('\x1b[3;1HABCDEFGHIJKLMNOPQRSTUVWXY')
        self.resize(20, 4)
        self.resize(7, 6)
        self.resize(15, 3)
        self.resize(10, 5)

    def test_deleted_continuation_does_not_join_later_unrelated_text(self):
        self.resize(4, 4)
        self.write('ABCDEFGHIJ\x1b[H')
        self.resize(4, 1)
        self.resize(4, 4)
        self.write('\x1b[2;1HX')
        self.resize(8, 4)
        self.assertEqual(self.screen._buf, ['ABCD', 'X', '', ''])


if __name__ == '__main__':
    unittest.main()
