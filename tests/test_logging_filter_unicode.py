"""Display cells and Unicode edits must agree with a real tmux pane."""

import io
import shutil
import unittest

from test_logging_filter_chunks import logging_filter
import test_tmux_logging_sync as sync_tests


class UnicodeRowTests(unittest.TestCase):
    def test_row_tracks_columns_instead_of_code_points(self):
        row = logging_filter.Screen._Row('A中e\u0301🙂Z', seen=True)
        self.assertEqual(row.cells, ['A', '中', None, 'e\u0301', '🙂', None, 'Z'])
        self.assertEqual(row.width, 7)
        self.assertEqual(row.text, 'A中e\u0301🙂Z')
        self.assertEqual(row.seen, b'\1' * 7)

    def test_combined_emoji_keep_one_terminal_cell(self):
        for text in ('❤️', '👩\u200d💻', '👍🏽', '🇺🇳'):
            with self.subTest(text=text):
                row = logging_filter.Screen._Row(text + 'Z')
                self.assertEqual(row.cells, [text, None, 'Z'])
                self.assertEqual(row.width, 3)

    def test_regional_indicators_form_pairs(self):
        row = logging_filter.Screen._Row('🇺🇳🇩')
        self.assertEqual(row.cells, ['🇺🇳', None, '🇩'])
        self.assertEqual(row.width, 3)

    def test_cell_slices_keep_provenance_and_partial_tmux_cells(self):
        row = logging_filter.Screen._Row('A中e\u0301Z', seen=True)
        row.seen[3:] = b'\0\0'
        piece = row.piece(1, 4, True)
        self.assertEqual(piece.cells, ['中', None, 'e\u0301'])
        self.assertEqual(piece.seen, b'\1\1\0')
        self.assertEqual(piece.pending(), ('e\u0301', True))
        # tmux grid operations preserve raw halves, even if a command cuts
        # through a wide character. A padding cell contributes no text.
        self.assertEqual(row.piece(2, 4, False).cells, [None, 'e\u0301'])
        self.assertEqual(row.piece(2, 4, False).text, 'e\u0301')
        self.assertEqual(row.piece(0, 2, False).text, 'A中')

    def test_leading_combining_marks_have_no_base_to_modify(self):
        row = logging_filter.Screen._Row('\u0301\u200d\ufe0fA')
        self.assertEqual(row.text, 'A')
        self.assertEqual(row.width, 1)

    def test_long_combining_sequence_uses_tmux_cell_byte_limit(self):
        row = logging_filter.Screen._Row('e' + '\u0301' * 30 + 'Z')
        self.assertEqual(row.cells, ['e' + '\u0301' * 15, 'Z'])
        self.assertEqual(row.width, 2)


class UnicodeScreenTests(unittest.TestCase):
    def screen(self, text, cols=10, rows=4):
        out = io.StringIO()
        screen = logging_filter.Screen(cols, rows, out)
        logging_filter.StreamProcessor(screen).feed(text)
        return screen, out

    def test_audit_wide_character_overwrite(self):
        screen, out = self.screen('中Z\x1b[3G!')
        screen.flush_all()
        self.assertEqual(out.getvalue(), '中!\n')
        self.assertEqual(screen._c, 3)

    def test_audit_combining_character_overwrite(self):
        screen, out = self.screen('e\u0301Z\x1b[2G!')
        screen.flush_all()
        self.assertEqual(out.getvalue(), 'e\u0301!\n')
        self.assertEqual(screen._c, 2)

    def test_overwriting_wide_continuation_clears_whole_glyph(self):
        screen, out = self.screen('A中Z\x1b[3G!')
        screen.flush_all()
        self.assertEqual(out.getvalue(), 'A !Z\n')

    def test_combining_mark_does_not_trigger_pending_wrap(self):
        screen, _ = self.screen('abcd\u0301', cols=4)
        self.assertEqual(screen._buf, ['abcd\u0301', '', '', ''])
        self.assertEqual((screen._c, screen._r), (4, 0))
        logging_filter.StreamProcessor(screen).feed('X')
        self.assertEqual(screen._buf[:2], ['abcd\u0301', 'X'])

    def test_snapshot_and_stream_use_the_same_unicode_columns(self):
        text = '中e\u0301❤️👍🏽🇺🇳'
        streamed, _ = self.screen(text, cols=20)
        restored = logging_filter.Screen(20, 4, io.StringIO())
        restored.load_snapshot([text], cursor=(9, 0))
        self.assertEqual(streamed._lines[0], restored._lines[0])
        self.assertEqual(streamed._c, 9)

    def test_seen_wide_cells_do_not_duplicate_after_height_restore(self):
        screen, out = self.screen('中e\u0301\none\ntwo', rows=2)
        screen.resize(10, 3)
        screen.flush_all()
        self.assertEqual(out.getvalue(), '中e\u0301\none\ntwo\n')


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class TmuxUnicodeTests(unittest.TestCase):
    setUp = sync_tests.TmuxSynchronizationTests.setUp
    tmux = sync_tests.TmuxSynchronizationTests.tmux
    wait_for = sync_tests.TmuxSynchronizationTests.wait_for
    worker_command = sync_tests.TmuxSynchronizationTests.worker_command
    create_pane = sync_tests.TmuxSynchronizationTests.create_pane
    dimensions = sync_tests.TmuxSynchronizationTests.dimensions
    capture = sync_tests.TmuxSynchronizationTests.capture
    visible = sync_tests.TmuxSynchronizationTests.visible
    emit = sync_tests.TmuxSynchronizationTests.emit
    cleanup_server = sync_tests.TmuxSynchronizationTests.cleanup_server
    start_recording = sync_tests.TmuxSynchronizationTests.start_recording
    stop_recording = sync_tests.TmuxSynchronizationTests.stop_recording

    def initialize(self, cols=10, rows=5):
        self.create_pane(cols, rows)
        self.screen = logging_filter.Screen(cols, rows, io.StringIO())
        self.screen._newline_mode = False
        self.stream = logging_filter.StreamProcessor(self.screen)
        self.serial = 0

    def write(self, text):
        self.serial += 1
        title = f'unicode-sync-{self.serial}'
        payload = text + '\x1b]0;' + title + '\x07'
        self.emit(payload.encode())
        self.wait_for(lambda: self.tmux('display-message', '-p', '#{pane_title}')
                      .decode().strip() == title, 'Unicode output consumed')
        self.stream.feed(payload)
        self.assert_screen()

    def assert_screen(self):
        self.assertEqual([row.rstrip() for row in self.screen._buf],
                         self.capture().splitlines())
        cursor = self.tmux('display-message', '-p', '#{cursor_x},#{cursor_y}')
        self.assertEqual(f'{self.screen._c},{self.screen._r}', cursor.decode().strip())

    def resize(self, cols, rows):
        self.tmux('resize-window', '-x', str(cols), '-y', str(rows))
        self.screen.resize(cols, rows)
        self.assert_screen()

    def test_wide_and_combining_overwrites(self):
        self.initialize()
        for text in ('中Z\x1b[3G!', '\rA中Z\x1b[3G!',
                     '\r\n' + 'e\u0301Z\x1b[2G!', '\r\n👩\u200d💻Z\x1b[3G!'):
            self.write(text)

    def test_unicode_wrap_and_pending_combining(self):
        self.initialize()
        self.write('123456789中!')
        self.write('\r\n123456789e\u0301')
        self.write('Z')

    def test_combining_requires_cursor_after_the_whole_wide_cell(self):
        self.initialize()
        self.write('中Z\x1b[2G\u0301')
        self.write('\x1b[3G\u0301')
        self.write('\r\nA中Z\x1b[3G\u0301')

    def test_combining_attaches_to_implicit_blank_after_cursor_jump(self):
        self.initialize()
        self.write('A\x1b[5G\u0301')
        self.write('\r\n\x1b[5G\u0301')

    def test_variation_selector_at_last_column_preserves_cursor(self):
        self.initialize()
        self.write('123456789❤')
        self.write('\ufe0f')
        self.write('X')

    def test_no_wrap_wide_character_at_right_margin(self):
        self.initialize()
        self.write('\x1b[?7labcdefghij')
        self.write('中')
        self.write('X')

    def test_no_wrap_variation_selector_at_right_margin(self):
        self.initialize()
        self.write('\x1b[?7l123456789')
        self.write('❤')
        self.write('\ufe0f')
        self.write('X')

    def test_emoji_sequences_and_variation_selector(self):
        self.initialize(cols=30)
        self.write('❤️|👍🏽|👩\u200d💻|🇺🇳🇩|☝Z')
        self.write('\r\n\u0301\ufe0fA\u200dB')

    def test_editing_inside_wide_cells(self):
        self.initialize()
        for text in ('A中🙂Z\x1b[3G\x1b[P',
                     '\r\nA中🙂Z\x1b[3G\x1b[@',
                     '\r\nA中🙂Z\x1b[3G\x1b[X',
                     '\r\nA中🙂Z\x1b[3G\x1b[1K'):
            self.write(text)

    def test_reflow_and_height_changes_preserve_unicode_cells(self):
        self.initialize(cols=10, rows=5)
        self.write('A中B🙂Ce\u0301D❤️E🇺🇳F')
        self.resize(16, 5)
        self.resize(7, 5)
        self.resize(12, 3)
        self.resize(20, 6)
        self.write('\r中!')

    def test_reflow_preserves_partially_edited_wide_cells(self):
        self.initialize(cols=10, rows=5)
        self.write('A中🙂Z\x1b[3G\x1b[P')
        self.write('\r\nA中🙂Z\x1b[3G\x1b[@')
        self.write('\r\nA中🙂Z\x1b[3G\x1b[X')
        self.resize(14, 5)
        self.resize(6, 5)
        self.resize(20, 6)

    @unittest.skipUnless(sync_tests.supported_tmux(), 'requires tmux 3.7 or newer')
    def test_recording_snapshot_uses_unicode_cursor_and_tab_columns(self):
        self.create_pane(cols=30, rows=8)
        self.emit('中e\u0301\tZ'.encode(), expected='中e\u0301\tZ')
        self.start_recording()
        self.emit(b'\x1b[9G!', expected='中e\u0301\t!')
        self.assertEqual(self.stop_recording(), '中e\u0301     !\n')


if __name__ == '__main__':
    unittest.main()
