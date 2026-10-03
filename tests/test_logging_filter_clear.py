"""Full clears preserve pending output without recording ordinary redraws."""

import io
import signal
import unittest

import test_logging_filter_chunks as helpers


logging_filter = helpers.logging_filter
HOME = '\x1b[H'
CLEAR = '\x1b[2J'
HOME_CLEAR = HOME + '\x1b[J'


class ClearScreenTests(unittest.TestCase):
    assert_chunkings = helpers.ChunkTests.assert_chunkings

    def screen(self, cols=80, rows=5):
        output = io.StringIO()
        screen = logging_filter.Screen(cols, rows, output)
        return screen, logging_filter.StreamProcessor(screen), output

    def test_full_clear_records_pending_rows_before_erasing(self):
        for sequence, cursor in ((CLEAR, (4, 2)), (HOME_CLEAR, (0, 0)),
                                 ('\x1b[5;80H\x1b[1J', (79, 4))):
            with self.subTest(sequence=sequence):
                screen, stream, output = self.screen()
                stream.feed('first\n\nlast')
                self.assertEqual(output.getvalue(), '')
                stream.feed(sequence)
                self.assertEqual(output.getvalue(), 'first\n\nlast\n')
                self.assertTrue(all(not row.rstrip() for row in screen._buf))
                self.assertEqual((screen._c, screen._r), cursor)
                stream.feed(HOME + 'next')
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'first\n\nlast\nnext\n')

    def test_clear_and_utf8_survive_every_input_boundary(self):
        text = ('до\nпосле' + CLEAR + HOME + 'новое'
                + HOME_CLEAR + 'итог')
        self.assert_chunkings(text.encode(), 'до\nпосле\nновое\nитог\n')

    def test_empty_and_repeated_clear_add_no_blank_or_duplicate_rows(self):
        for sequence in (CLEAR + HOME, HOME_CLEAR,
                         '\x1b[5;80H\x1b[1J' + HOME):
            with self.subTest(sequence=sequence):
                screen, stream, output = self.screen()
                stream.feed(sequence * 3)
                self.assertEqual(output.getvalue(), '')
                stream.feed('one' + sequence * 3 + 'two' + sequence * 3)
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'one\ntwo\n')

    def test_clear_does_not_repeat_rows_restored_from_recorded_history(self):
        for sequence in (CLEAR, HOME_CLEAR):
            with self.subTest(sequence=sequence):
                screen, stream, output = self.screen(cols=20)
                stream.feed('one\ntwo\nthree\nfour\nfive')
                screen.resize(20, 3)
                self.assertEqual(output.getvalue(), 'one\ntwo\n')
                screen.resize(20, 5)
                self.assertEqual(screen._buf, ['one', 'two', 'three', 'four', 'five'])
                stream.feed(sequence + HOME + 'next')
                screen.flush_all()
                self.assertEqual(output.getvalue(), 'one\ntwo\nthree\nfour\nfive\nnext\n')

    def test_clear_keeps_only_unrecorded_suffix_after_reflow(self):
        screen, stream, output = self.screen(cols=10, rows=2)
        stream.feed('ABCDEFGHIJKLMNOPQRSTUVWXY')
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\n')
        screen.resize(20, 2)
        stream.feed(CLEAR + HOME + 'next')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'ABCDEFGHIJ\nKLMNOPQRST\nUVWXY\nnext\n')

    def test_partial_ed_and_line_edits_keep_only_final_text(self):
        cases = (
            ('progress 10%\rprogress 90%\x1b[K', 'progress 90%\n'),
            ('abX\bc\x1b[K', 'abc\n'),
            ('old\r\x1b[2Knew', 'new\n'),
            ('prompt> \nCOMPLETION A\nCOMPLETION B\x1b[1;9H\x1b[Jdone',
             'prompt> done\n'),
            ('first\nsecond\x1b[2;4H\x1b[J', 'first\nsec\n'),
            ('first\nsecond\x1b[1;3H\x1b[1J' + HOME + 'ready',
             'ready\nsecond\n'),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                screen, stream, output = self.screen()
                stream.feed(text)
                self.assertEqual(output.getvalue(), '')
                screen.flush_all()
                self.assertEqual(output.getvalue(), expected)

    def test_origin_relative_home_is_not_a_full_display_clear(self):
        screen, stream, output = self.screen()
        stream.feed('keep\nsecond\nthird\x1b[2;4r\x1b[?6h' + HOME_CLEAR)
        self.assertEqual(output.getvalue(), '')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'keep\n')

    def test_alternate_clear_never_records_app_or_saved_main_early(self):
        for mode in (47, 1047, 1049):
            for sequence in (CLEAR, HOME_CLEAR, '\x1b[5;80H\x1b[1J'):
                with self.subTest(mode=mode, sequence=sequence):
                    screen, stream, output = self.screen()
                    stream.feed(f'MAIN\x1b[?{mode}hPRIVATE' + sequence + 'SECRET')
                    self.assertEqual(output.getvalue(), '')
                    stream.feed(f'\x1b[?{mode}l after')
                    screen.flush_all()
                    self.assertEqual(output.getvalue(), 'MAIN after\n')

    def test_ed3_clears_history_without_erasing_visible_rows_or_moving_cursor(self):
        screen, stream, output = self.screen(rows=3)
        stream.feed('one\ntwo\nthree\nfour\nfive')
        self.assertEqual(output.getvalue(), 'one\ntwo\n')
        self.assertTrue(screen._history)
        before = (screen._buf, screen._c, screen._r)
        stream.feed('\x1b[3J')
        self.assertEqual((screen._buf, screen._c, screen._r), before)
        self.assertEqual(screen._history, [])
        self.assertEqual(screen._history_scrolled, 0)
        self.assertEqual(output.getvalue(), 'one\ntwo\n')
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'one\ntwo\nthree\nfour\nfive\n')

    def test_ed3_in_alternate_preserves_saved_main_history(self):
        screen, stream, output = self.screen(rows=3)
        stream.feed('one\ntwo\nthree\nfour\x1b[?1049hPRIVATE\x1b[3J')
        self.assertEqual(screen._buf[0], 'PRIVATE')
        self.assertEqual(output.getvalue(), 'one\n')
        stream.feed('\x1b[?1049l')
        self.assertEqual([row.text for row in screen._history], ['one'])
        screen.flush_all()
        self.assertEqual(output.getvalue(), 'one\ntwo\nthree\nfour\n')


class ClearCliTests(unittest.TestCase):
    start_filter = helpers.ChunkCliTests.start_filter
    cleanup_child = staticmethod(helpers.ChunkCliTests.cleanup_child)
    read_until = helpers.ChunkCliTests.read_until
    finish = helpers.ChunkCliTests.finish

    def test_clear_writes_live_output_before_eof(self):
        for sequence in (CLEAR, HOME_CLEAR):
            with self.subTest(sequence=sequence):
                child = self.start_filter()
                data = ('до\nпосле' + sequence).encode()
                self.assertEqual(child.stdin.write(data), len(data))
                before = self.read_until(child.stdout, 'после\n'.encode())
                self.assertEqual(before, 'до\nпосле\n'.encode())
                child.stdin.write((HOME + 'новое').encode())
                self.assertEqual(before + self.finish(child), 'до\nпосле\nновое\n'.encode())

    def test_eof_and_signals_after_clear_preserve_each_main_row_once(self):
        for number in (None, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                child = self.start_filter()
                data = ('before' + CLEAR + HOME_CLEAR + 'after').encode()
                self.assertEqual(child.stdin.write(data), len(data))
                self.read_until(child.stderr, b'READ\n')
                if number is not None:
                    child.send_signal(number)
                    child.wait(timeout=helpers.TIMEOUT)
                self.assertEqual(self.finish(child), b'before\nafter\n')


if __name__ == '__main__':
    unittest.main()
