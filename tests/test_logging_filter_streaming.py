"""Regression coverage for live pipe input and graceful filter shutdown."""

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


FILTER = Path(__file__).resolve().parents[1] / "scripts" / "logging_filter.py"
TIMEOUT = 5

# The child announces when both shutdown handlers are installed. Optional
# hooks deliver signals at deterministic points that timing sleeps cannot test.
BOOTSTRAP = r"""
import importlib.util
import os
import signal
import sys

path, mode, *extra = sys.argv[1:]
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

if mode in ("mid_chunk", "continuous"):
    original_write = module.Screen.write_char
    count = 0
    def write_char(screen, char):
        global count
        original_write(screen, char)
        count += 1
        if count == 10:
            os.kill(os.getpid(), signal.SIGTERM)
    module.Screen.write_char = write_char

if mode == "continuous":
    original_read = os.read
    def read(fd, size):
        chunk = original_read(fd, size)
        if fd == 0 and chunk:
            # A deterministic producer: the input pipe never becomes empty.
            os.write(int(extra[0]), chunk)
        return chunk
    os.read = read

if mode == "during_flush":
    class SignalingOutput:
        sent = False
        def write(self, text):
            result = sys.__stdout__.write(text)
            if not self.sent:
                self.sent = True
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGHUP)
            return result
        def flush(self):
            sys.__stdout__.flush()
    sys.stdout = SignalingOutput()

module.main()
"""


class StreamingTests(unittest.TestCase):
    def start_filter(self, mode="normal", stdin=subprocess.PIPE, pass_fds=()):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", BOOTSTRAP, str(FILTER), mode,
             *map(str, pass_fds)],
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, pass_fds=pass_fds,
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
            if stream is not None:
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

    def finish(self, child, prefix=b"", data=None):
        output, errors = child.communicate(input=data, timeout=TIMEOUT)
        self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))
        return prefix + output

    def finish_signaled(self, child, number, prefix=b""):
        child.send_signal(number)
        # Keep the writer open until exit: shutdown must not depend on EOF.
        child.wait(timeout=TIMEOUT)
        return self.finish(child, prefix)

    def test_small_input_is_logged_before_eof_and_flushed_once(self):
        child = self.start_filter()
        child.stdin.write(b"one\ntwo\nthree\nfour")
        prefix = self.read_until(child.stdout, b"one\n")
        self.assertEqual(prefix, b"one\n")
        self.assertEqual(self.finish(child, prefix), b"one\ntwo\nthree\nfour\n")

    def test_eof_flushes_visible_lines_once(self):
        child = self.start_filter()
        self.assertEqual(
            self.finish(child, data=b"first\n\nlast"), b"first\n\nlast\n",
        )

    def test_shutdown_with_open_pipe_preserves_output(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            for data in (b"", b"one\ntwo\nthree\nfour"):
                with self.subTest(signal=number, data=data):
                    child = self.start_filter()
                    prefix = b""
                    if data:
                        child.stdin.write(data)
                        prefix = self.read_until(child.stdout, b"one\n")
                    output = self.finish_signaled(child, number, prefix)
                    self.assertEqual(output, data + b"\n" if data else b"")

    def test_utf8_character_split_across_live_reads(self):
        child = self.start_filter()
        child.stdin.write(b"one\ntwo\nthree\n\xd0")
        prefix = self.read_until(child.stdout, b"one\n")
        self.assertEqual(
            self.finish(child, prefix, b"\xbd"), "one\ntwo\nthree\nн\n".encode(),
        )

    def test_incomplete_utf8_is_replaced_at_eof_or_shutdown(self):
        for number in (None, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                child = self.start_filter()
                child.stdin.write(b"one\ntwo\nthree\nlast \xe2\x82")
                prefix = self.read_until(child.stdout, b"one\n")
                output = (self.finish(child, prefix) if number is None else
                          self.finish_signaled(child, number, prefix))
                self.assertEqual(output, "one\ntwo\nthree\nlast �\n".encode())

    def test_signal_mid_chunk_finishes_chunk_and_drains_queued_input(self):
        self.check_queued_shutdown("mid_chunk")

    def test_shutdown_finishes_even_while_input_keeps_arriving(self):
        self.check_queued_shutdown("continuous")

    def check_queued_shutdown(self, mode):
        data = b"".join(f"line-{i:04d}\n".encode() for i in range(600))
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb", buffering=0) as source, \
                os.fdopen(writer, "wb", buffering=0) as sink:
            # Preload more than a read block before the reader starts, keeping
            # the writer open throughout shutdown. Never block on small pipes.
            os.set_blocking(writer, False)
            if sink.write(data) != len(data):
                self.skipTest("This regression requires a pipe holding 6000 bytes")
            os.set_blocking(writer, True)
            inherited = (writer,) if mode == "continuous" else ()
            child = self.start_filter(mode, stdin=source, pass_fds=inherited)
            output = self.finish(child)
            if mode == "continuous":
                self.assertTrue(output.startswith(data))
            else:
                self.assertEqual(output, data)

    def test_incomplete_escape_is_finalized_equally_at_eof_and_signal(self):
        outputs = []
        for number in (None, signal.SIGTERM, signal.SIGHUP):
            child = self.start_filter()
            child.stdin.write(b"one\ntwo\nthree\nlast\x1b[")
            prefix = self.read_until(child.stdout, b"one\n")
            outputs.append(self.finish(child, prefix) if number is None else
                           self.finish_signaled(child, number, prefix))
        self.assertEqual(outputs[1:], [outputs[0], outputs[0]])

    def test_repeated_signals_during_final_flush_do_not_duplicate_lines(self):
        child = self.start_filter("during_flush")
        self.assertEqual(
            self.finish(child, data=b"first\nsecond\nthird"),
            b"first\nsecond\nthird\n",
        )

    def test_regular_file_input(self):
        data = "first\nsecond\nthird\nпоследняя".encode()
        with tempfile.TemporaryFile() as source:
            source.write(data)
            source.seek(0)
            child = self.start_filter(stdin=source)
            self.assertEqual(self.finish(child), data + b"\n")


if __name__ == "__main__":
    unittest.main()
