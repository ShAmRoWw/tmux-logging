"""Shutdown must preserve the main screen and never log alternate content."""

import copy
import io
import signal
import unittest

import test_logging_filter_chunks as helpers


logging_filter = helpers.logging_filter
MODES = (47, 1047, 1049)
PRIVATE = "PRIVATE EDITOR CONTENT\n" * 8 + "PRIVATE LAST LINE"


def mode_sequence(mode, enable=True):
    return f"\x1b[?{mode}{'h' if enable else 'l'}"


class AlternateScreenTests(unittest.TestCase):
    def render(self, text):
        output = io.StringIO()
        screen = logging_filter.Screen(80, 3, output)
        stream = logging_filter.StreamProcessor(screen)
        stream.feed(text)
        stream.finish()
        return screen, output

    def test_flush_in_alternate_preserves_main_screen(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                screen, output = self.render("MAIN" + mode_sequence(mode) + PRIVATE)
                screen.flush_all()
                self.assertEqual(output.getvalue(), "MAIN\n")

    def test_scrolled_and_visible_main_lines_keep_order_and_blanks(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                screen, output = self.render(
                    "scrolled\nvisible\n\nlast" + mode_sequence(mode) + PRIVATE,
                )
                self.assertEqual(output.getvalue(), "scrolled\n")
                screen.flush_all()
                self.assertEqual(output.getvalue(), "scrolled\nvisible\n\nlast\n")

    def test_empty_main_does_not_log_alternate_even_when_it_scrolls(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                screen, output = self.render(mode_sequence(mode) + PRIVATE)
                screen.flush_all()
                self.assertEqual(output.getvalue(), "")

    def test_normal_return_keeps_subsequent_main_output(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                screen, output = self.render(
                    "before" + mode_sequence(mode) + PRIVATE
                    + mode_sequence(mode, False) + " after\nnext",
                )
                screen.flush_all()
                self.assertEqual(output.getvalue(), "before after\nnext\n")

    def test_repeated_enter_does_not_replace_saved_main(self):
        screen, output = self.render(
            "MAIN" + mode_sequence(1049) + "PRIVATE FIRST"
            + mode_sequence(47) + PRIVATE + mode_sequence(1047),
        )
        screen.flush_all()
        self.assertEqual(output.getvalue(), "MAIN\n")

    def test_multiple_sessions_preserve_updated_main(self):
        screen, output = self.render(
            "first" + mode_sequence(47) + PRIVATE + mode_sequence(47, False)
            + "\nsecond" + mode_sequence(1047) + PRIVATE
            + mode_sequence(1047, False) + "\nthird"
            + mode_sequence(1049) + PRIVATE,
        )
        screen.flush_all()
        self.assertEqual(output.getvalue(), "first\nsecond\nthird\n")

    def test_flush_does_not_change_active_screen_or_saved_main(self):
        screen, output = self.render(
            "MAIN\x1b7" + mode_sequence(1049)
            + "PRIVATE\x1b[2;3r\x1b[2;4H\x1b7",
        )
        before = copy.deepcopy({key: value for key, value in vars(screen).items()
                                if key != "out"})
        screen.flush_all()
        self.assertEqual({key: value for key, value in vars(screen).items()
                          if key != "out"}, before)
        self.assertEqual(output.getvalue(), "MAIN\n")


class AlternateCliTests(unittest.TestCase):
    # Reuse only subprocess helpers, without inheriting their test methods.
    start_filter = helpers.ChunkCliTests.start_filter
    cleanup_child = staticmethod(helpers.ChunkCliTests.cleanup_child)
    read_until = helpers.ChunkCliTests.read_until
    finish = helpers.ChunkCliTests.finish

    def test_eof_and_signals_in_alternate_preserve_main(self):
        for mode in MODES:
            for number in (None, signal.SIGTERM, signal.SIGHUP):
                for main in ("", "MAIN", "scrolled\nvisible\n\nlast"):
                    with self.subTest(mode=mode, signal=number, main=main):
                        child = self.start_filter()
                        data = (main + mode_sequence(mode) + PRIVATE).encode()
                        self.assertEqual(child.stdin.write(data), len(data))
                        self.read_until(child.stderr, b"READ\n")
                        if number is not None:
                            child.send_signal(number)
                            # Keep stdin open until exit; do not supply EOF.
                            child.wait(timeout=helpers.TIMEOUT)
                        self.assertEqual(self.finish(child),
                                         (main + "\n").encode() if main else b"")


if __name__ == "__main__":
    unittest.main()
