"""Regression coverage for text between complete OSC escape sequences."""

import importlib.util
import io
from pathlib import Path
import unittest


FILTER = Path(__file__).resolve().parents[1] / "scripts" / "logging_filter.py"
SPEC = importlib.util.spec_from_file_location("logging_filter", FILTER)
logging_filter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logging_filter)

ST = "\x1b\\"
BEL = "\x07"


def osc(payload, terminator=ST):
    return "\x1b]" + payload + terminator


def hyperlink(label, terminator=ST):
    return osc("8;;https://example.test/", terminator) + label + osc("8;;", terminator)


class OscTests(unittest.TestCase):
    def assert_log(self, data, expected):
        output = io.StringIO()
        screen = logging_filter.Screen(80, 24, output)
        # A single chunk deliberately places all terminators in one parse call.
        logging_filter.process(data, screen)
        screen.flush_all()
        self.assertEqual(output.getvalue(), expected)

    def test_st_hyperlink_preserves_label_and_surrounding_text(self):
        self.assert_log("before" + hyperlink("LINK") + "after", "beforeLINKafter\n")

    def test_adjacent_st_hyperlinks_preserve_both_labels(self):
        self.assert_log(hyperlink("FIRST") + hyperlink("SECOND"), "FIRSTSECOND\n")

    def test_st_titles_preserve_text_between_them(self):
        self.assert_log(
            "before" + osc("0;first title") + "visible" + osc("2;second title") + "after",
            "beforevisibleafter\n",
        )

    def test_bel_terminated_osc_preserves_existing_behavior(self):
        self.assert_log(
            osc("0;title", BEL) + "before" + hyperlink("LINK", BEL) + "after",
            "beforeLINKafter\n",
        )

    def test_mixed_terminators_preserve_hyperlink_labels(self):
        for opening, closing in ((ST, BEL), (BEL, ST)):
            with self.subTest(opening=opening, closing=closing):
                self.assert_log(
                    "before" + osc("8;;https://example.test/", opening)
                    + "LINK" + osc("8;;", closing) + "after",
                    "beforeLINKafter\n",
                )

    def test_controls_between_st_sequences_are_processed(self):
        cases = (
            ("first\nsecond", "first\nsecond\n"),
            ("abX\x1b[Dc", "abc\n"),
            ("\x1b[31mred\x1b[0m", "red\n"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assert_log(osc("0;first title") + text + osc("2;second title"), expected)


if __name__ == "__main__":
    unittest.main()
