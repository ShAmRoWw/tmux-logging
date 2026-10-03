"""Chunk boundaries must not change the logged text or cursor position."""

import codecs
import importlib.util
import io
import os
from pathlib import Path
import random
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


FILTER = Path(__file__).resolve().parents[1] / "scripts" / "logging_filter.py"
SPEC = importlib.util.spec_from_file_location("logging_filter_chunks", FILTER)
logging_filter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logging_filter)

ST = "\x1b\\"
BEL = "\x07"
TIMEOUT = 5


def osc(payload, terminator=ST):
    return "\x1b]" + payload + terminator


def chunkings(data):
    """Exercise every two-piece split, bytewise input, and repeatable mixes."""
    for split in range(len(data) + 1):
        yield "split-" + str(split), (data[:split], data[split:])
    yield "one-at-a-time", tuple(data[i:i + 1] for i in range(len(data)))
    rng = random.Random(48271)
    for run in range(10):
        chunks = []
        offset = 0
        while offset < len(data):
            size = rng.randint(1, 11)
            chunks.append(data[offset:offset + size])
            offset += size
        yield "mixed-" + str(run), chunks


class ChunkTests(unittest.TestCase):
    def assert_chunkings(self, data, expected, cursor=None):
        for name, chunks in chunkings(data):
            with self.subTest(chunks=name):
                output = io.StringIO()
                screen = logging_filter.Screen(80, 4, output)
                processor = logging_filter.StreamProcessor(screen)
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                for chunk in chunks:
                    processor.feed(decoder.decode(chunk) if isinstance(chunk, bytes) else chunk)
                if isinstance(data, bytes):
                    processor.feed(decoder.decode(b"", final=True))
                processor.finish()
                if cursor is not None:
                    self.assertEqual((screen._r, screen._c), cursor)
                screen.flush_all()
                self.assertEqual(output.getvalue(), expected)

    def test_hyperlinks_keep_labels_for_each_terminator_combination(self):
        for opening in (ST, BEL):
            for closing in (ST, BEL):
                with self.subTest(opening=opening, closing=closing):
                    data = ("before" + osc("8;;https://example.test/", opening)
                            + "LINK" + osc("8;;", closing) + "after")
                    self.assert_chunkings(data, "beforeLINKafter\n", (0, 15))

    def test_titles_and_adjacent_osc_keep_visible_text(self):
        data = ("first" + osc("0;private title") + "between"
                + osc("2;another title", BEL) + osc("0;last title") + "last")
        self.assert_chunkings(data, "firstbetweenlast\n", (0, 16))

    def test_escape_characters_inside_osc_stay_hidden(self):
        for payload in ("0;hidden\x1b[31msecret", "0;hidden\x1b(0secret",
                        "0;hidden\x1b", "0;hidden\x1b\x1b"):
            for terminator in (ST, BEL):
                with self.subTest(payload=payload, terminator=terminator):
                    self.assert_chunkings(
                        "before" + osc(payload, terminator) + "after",
                        "beforeafter\n", (0, 11),
                    )

    def test_cursor_and_erase_commands_survive_every_split(self):
        self.assert_chunkings(
            "abc\x1b[2DZ\x1b[2;4HQ\x1b[1A!", "aZc !\n   Q\n", (0, 5),
        )
        self.assert_chunkings(
            "old\rnew\nabX\x1b[Dc\x1b[K", "new\nabc\n", (1, 3),
        )

    def test_charset_and_saved_cursor_survive_every_split(self):
        self.assert_chunkings("ab\x1b(0cd\x1b(Bef", "abcdef\n", (0, 6))
        self.assert_chunkings(
            "ab\x1b7X\x1b[2;4HZ\x1b8C", "abC\n   Z\n", (0, 3),
        )

    def test_unhandled_complete_csi_does_not_hide_following_text(self):
        for sequence in ("\x1b[5 q", "\x1b[38:2::255:0:0m"):
            with self.subTest(sequence=sequence):
                self.assert_chunkings("before" + sequence + "after", "beforeafter\n", (0, 11))

    def test_utf8_bytes_can_split_inside_text_and_osc(self):
        data = ("до" + osc("0;секретный заголовок")
                + osc("8;;https://example.test/путь", BEL) + "ссылка"
                + osc("8;;") + "после").encode()
        self.assert_chunkings(data, "доссылкапосле\n", (0, 13))

    def test_incomplete_osc_is_discarded_at_finish(self):
        for suffix in ("\x1b]", "\x1b]0;secret-title",
                       "\x1b]0;secret-title\x1b"):
            with self.subTest(suffix=suffix):
                self.assert_chunkings("visible" + suffix, "visible\n", (0, 7))

    def test_incomplete_escape_csi_and_charset_are_discarded_at_finish(self):
        for suffix in ("\x1b", "\x1b[", "\x1b[12;", "\x1b[5 ",
                       "\x1b[38:2::255:", "\x1b(", "\x1b)"):
            with self.subTest(suffix=suffix):
                self.assert_chunkings("visible" + suffix, "visible\n", (0, 7))

    def test_incomplete_utf8_is_hidden_only_inside_osc(self):
        self.assert_chunkings(b"visible\x1b]0;private \xe2\x82", "visible\n", (0, 7))
        self.assert_chunkings(b"visible \xe2\x82", "visible \ufffd\n", (0, 9))


# Announce installed signal handlers and actual input reads. Tests can then
# coordinate writes and signals without relying on sleeps or pipe packet sizes.
BOOTSTRAP = r"""
import importlib.util
import os
import signal
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("logging_filter", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv = [path, "80", "3"]

original_signal = signal.signal
def install_handler(number, handler):
    previous = original_signal(number, handler)
    if number == signal.SIGHUP:
        os.write(2, b"READY\n")
    return previous
signal.signal = install_handler

original_read = os.read
def read(fd, size):
    data = original_read(fd, size)
    if fd == 0 and data:
        os.write(2, b"READ\n")
    return data
os.read = read

module.main()
"""


class ChunkCliTests(unittest.TestCase):
    def start_filter(self):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", BOOTSTRAP, str(FILTER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.addCleanup(self.cleanup_child, child)
        self.read_until(child.stderr, b"READY\n")
        return child

    @staticmethod
    def cleanup_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=TIMEOUT)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    def read_until(self, stream, marker):
        result = b""
        deadline = time.monotonic() + TIMEOUT
        while marker not in result:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, f"Timed out waiting for {marker!r}: {result!r}")
            readable, _, _ = select.select([stream], [], [], remaining)
            self.assertTrue(readable, f"Timed out waiting for {marker!r}: {result!r}")
            chunk = os.read(stream.fileno(), 65536)
            self.assertTrue(chunk, f"Unexpected EOF waiting for {marker!r}: {result!r}")
            result += chunk
        return result

    def finish(self, child):
        output, errors = child.communicate(timeout=TIMEOUT)
        self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))
        return output

    def test_st_split_at_real_4096_byte_read_boundary(self):
        opening = "\x1b]0;secret-title"
        prefix = "a" * (4096 - len(opening) - 1)
        data = (prefix + opening + ST + "shown").encode()
        # A regular file fixes the read boundary independently of pipe timing.
        with tempfile.TemporaryFile() as source:
            source.write(data)
            source.seek(0)
            result = subprocess.run(
                [sys.executable, "-B", str(FILTER), "10000", "2"],
                stdin=source, capture_output=True, timeout=TIMEOUT,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, (prefix + "shown\n").encode())

    def test_split_osc_resumes_live_logging_before_eof(self):
        child = self.start_filter()
        child.stdin.write(b"one\ntwo\nthree\nbefore\x1b]0;secret-title\x1b")
        prefix = self.read_until(child.stdout, b"one\n")
        self.assertEqual(prefix, b"one\n")
        child.stdin.write(b"\\after\nlast")
        prefix += self.read_until(child.stdout, b"two\n")
        self.assertEqual(prefix, b"one\ntwo\n")
        self.assertEqual(prefix + self.finish(child), b"one\ntwo\nthree\nbeforeafter\nlast\n")

    def test_partial_osc_does_not_leak_at_eof_or_signal(self):
        for suffix in (b"\x1b]0;secret-title", b"\x1b]0;secret-title\x1b",
                       b"\x1b]0;private \xe2\x82"):
            for number in (None, signal.SIGTERM, signal.SIGHUP):
                with self.subTest(suffix=suffix, signal=number):
                    child = self.start_filter()
                    child.stdin.write(b"visible" + suffix)
                    self.read_until(child.stderr, b"READ\n")
                    if number is not None:
                        child.send_signal(number)
                        # Keep stdin open: successful exit cannot depend on EOF.
                        child.wait(timeout=TIMEOUT)
                    self.assertEqual(self.finish(child), b"visible\n")

    def test_unhandled_complete_csi_keeps_logging_across_live_reads(self):
        for sequence in (b"\x1b[5 q", b"\x1b[38:2::255:0:0m"):
            with self.subTest(sequence=sequence):
                child = self.start_filter()
                lines = b"line\n" * 1000
                data = b"start\n" + sequence + lines + b"confirmed\nx\ny\nz"
                self.assertGreater(len(data), 4096)
                self.assertEqual(child.stdin.write(data), len(data))
                prefix = self.read_until(child.stdout, b"confirmed\n")
                self.assertEqual(prefix, b"start\n" + lines + b"confirmed\n")
                self.assertEqual(prefix + self.finish(child), prefix + b"x\ny\nz\n")


if __name__ == "__main__":
    unittest.main()
