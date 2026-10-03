"""Exercise pane synchronization against an isolated, real tmux server."""

import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tmux_logging.py"
TIMEOUT = 8
WORKER = r"""
import os
import sys
import tty

tty.setraw(0)
source = os.open(sys.argv[1], os.O_RDWR)
open(sys.argv[2], 'w').close()
while True:
    data = os.read(source, 65536)
    if not data:
        break
    while data:
        data = data[os.write(1, data):]
"""


def supported_tmux():
    if not shutil.which("tmux"):
        return False
    version = subprocess.run(["tmux", "-V"], capture_output=True, text=True,
                             timeout=TIMEOUT, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", version)
    return match is not None and tuple(map(int, match.groups())) >= (3, 7)


@unittest.skipUnless(supported_tmux(), "requires tmux 3.7 or newer")
class TmuxSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="logging-sync-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.socket = self.root / "tmux.sock"
        self.base = ["tmux", "-S", str(self.socket), "-f", "/dev/null"]
        self.log = self.root / "recording.log"
        self.writers = {}
        self.addCleanup(self.cleanup_server)

    def tmux(self, *args, check=True):
        return subprocess.run(self.base + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=TIMEOUT,
                              check=check).stdout

    def wait_for(self, callback, description):
        deadline = time.monotonic() + TIMEOUT
        last = None
        while time.monotonic() < deadline:
            last = callback()
            if last:
                return last
            time.sleep(0.01)
        self.fail(f"Timed out waiting for {description}; last result: {last!r}")

    def worker_command(self):
        index = len(self.writers)
        fifo = self.root / f"input-{index}"
        ready = self.root / f"ready-{index}"
        os.mkfifo(fifo)
        # Opening both ends avoids an unbounded open() if the pane fails.
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.writers[None] = fd
        command = shlex.join([sys.executable, "-B", "-c", WORKER,
                              str(fifo), str(ready)])
        return command, ready, fd

    def create_pane(self, cols=30, rows=8):
        command, ready, fd = self.worker_command()
        pane = self.tmux("new-session", "-d", "-P", "-F", "#{pane_id}",
                         "-s", "recording", "-x", str(cols), "-y", str(rows),
                         command).decode().strip()
        self.writers.pop(None)
        self.writers[pane] = fd
        self.pane = pane
        self.tmux("set-option", "-t", "recording", "status", "off")
        self.wait_for(ready.exists, "PTY worker startup")
        self.wait_for(lambda: self.dimensions(pane) == (cols, rows),
                      "initial pane dimensions")
        return pane

    def split(self, width):
        command, ready, fd = self.worker_command()
        pane = self.tmux("split-window", "-d", "-h", "-t", self.pane,
                         "-l", str(width), "-P", "-F", "#{pane_id}",
                         command).decode().strip()
        self.writers.pop(None)
        self.writers[pane] = fd
        self.wait_for(ready.exists, "second PTY worker startup")
        return pane

    def dimensions(self, pane=None):
        return tuple(map(int, self.tmux("display-message", "-p", "-t",
                         pane or self.pane, "#{pane_width} #{pane_height}").split()))

    def capture(self, pane=None):
        return self.tmux("capture-pane", "-p", "-t", pane or self.pane).decode()

    def history_and_screen(self):
        return self.tmux("capture-pane", "-p", "-S", "-", "-t", self.pane).decode().rstrip("\n")

    def visible(self, pane=None):
        return self.capture(pane).rstrip("\n")

    def emit(self, data, expected=None, pane=None):
        pane = pane or self.pane
        deadline = time.monotonic() + TIMEOUT
        while data:
            try:
                count = os.write(self.writers[pane], data)
                data = data[count:]
            except BlockingIOError:
                self.assertLess(time.monotonic(), deadline, "FIFO writer blocked")
                time.sleep(0.01)
        if expected is not None:
            self.wait_for(lambda: self.visible(pane) == expected,
                          f"pane content {expected!r}")

    def start_recording(self, shell_entry=False, script=None):
        script = script or SCRIPT
        server_pid = self.tmux("display-message", "-p", "#{pid}").decode().strip()
        env = dict(os.environ, TMUX=f"{self.socket},{server_pid},0",
                   TMUX_PANE=self.pane, PYTHONDONTWRITEBYTECODE="1")
        command = (["bash", str(script.with_name("start_logging.sh")), str(self.log)]
                   if shell_entry else
                   [sys.executable, "-B", str(script), "start", str(self.log), self.pane])
        result = subprocess.run(command, env=env,
                                capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane,
                                   "#{pane_pipe}").strip(), b"1")

    def resize(self, cols, rows):
        self.tmux("resize-window", "-t", self.pane, "-x", str(cols),
                  "-y", str(rows))
        self.wait_for(lambda: self.dimensions() == (cols, rows),
                      f"pane size {cols}x{rows}")

    def stop_recording(self):
        self.tmux("pipe-pane", "-t", self.pane)
        self.wait_for(lambda: not self.tmux("list-clients", "-F", "#{client_name}"),
                      "logging control-client shutdown")
        return self.log.read_text()

    def cleanup_server(self):
        for fd in self.writers.values():
            os.close(fd)
        self.tmux("kill-server", check=False)

    def test_existing_screen_and_absolute_cursor_overwrite(self):
        self.create_pane()
        self.emit(b"before\r\nkeep\r\nthird\x1b[5;4H", "before\nkeep\nthird")
        self.wait_for(lambda: self.tmux("display-message", "-p", "-t", self.pane,
                      "#{cursor_x},#{cursor_y}").strip() == b"3,4", "initial cursor")
        self.start_recording()
        self.emit(b"HELLO\x1b[5;4HCHANGED", "before\nkeep\nthird\n\n   CHANGED")
        self.assertEqual(self.stop_recording(), "before\nkeep\nthird\n\n   CHANGED\n")

    def test_width_growth_precedes_new_output(self):
        self.create_pane(10, 6)
        self.start_recording()
        self.resize(20, 6)
        self.emit(b"ABCDEFGHIJKLMNO\rREPLACED", "REPLACEDIJKLMNO")
        self.assertEqual(self.stop_recording(), "REPLACEDIJKLMNO\n")

    def test_width_shrink_precedes_new_output(self):
        self.create_pane(20, 6)
        self.start_recording()
        self.resize(10, 6)
        self.emit(b"ABCDEFGHIJKLMNO\rREPLACED", "ABCDEFGHIJ\nREPLACED")
        self.assertEqual(self.stop_recording(), "ABCDEFGHIJ\nREPLACED\n")

    def test_width_growth_reflows_initial_wrapped_text(self):
        self.create_pane(10, 6)
        self.emit(b"ABCDEFGHIJKLMNO", "ABCDEFGHIJ\nKLMNO")
        self.start_recording()
        self.resize(20, 6)
        self.emit(b"\rREPLACED", "REPLACEDIJKLMNO")
        self.assertEqual(self.stop_recording(), "REPLACEDIJKLMNO\n")

    def test_width_shrink_reflows_initial_text(self):
        self.create_pane(20, 6)
        self.emit(b"ABCDEFGHIJKLMNO", "ABCDEFGHIJKLMNO")
        self.start_recording()
        self.resize(10, 6)
        # tmux moves the first reflowed row into history when its trailing
        # screen rows are blank. It remains part of this recording.
        self.emit(b"\rREPLACED", "REPLACED")
        self.assertEqual(self.history_and_screen(), "ABCDEFGHIJ\nREPLACED")
        self.assertEqual(self.stop_recording(), "ABCDEFGHIJ\nREPLACED\n")

    def test_height_shrink_and_growth_do_not_duplicate_restored_history(self):
        self.create_pane(20, 6)
        text = "one\ntwo\nthree\nfour\nfive\nsix"
        self.emit(text.replace("\n", "\r\n").encode(), text)
        self.start_recording()
        self.resize(20, 3)
        self.wait_for(lambda: self.visible() == "four\nfive\nsix", "height shrink")
        self.resize(20, 6)
        self.wait_for(lambda: self.visible() == text, "history restored by height growth")
        self.assertEqual(self.stop_recording(), text + "\n")

    def test_split_zoom_and_unzoom_reflow_the_recorded_pane(self):
        self.create_pane(30, 6)
        self.start_recording()
        self.split(10)
        self.assertEqual(self.dimensions(), (19, 6))
        self.emit(b"ABCDEFGHIJKLMNOPQRSTUV", "ABCDEFGHIJKLMNOPQRS\nTUV")
        self.tmux("resize-pane", "-Z", "-t", self.pane)
        self.assertEqual(self.dimensions(), (30, 6))
        self.emit(b"\rUPDATED", "UPDATEDHIJKLMNOPQRSTUV")
        self.tmux("resize-pane", "-Z", "-t", self.pane)
        self.wait_for(lambda: self.visible() == "TUV", "unzoom reflow")
        self.assertEqual(self.history_and_screen(), "UPDATEDHIJKLMNOPQRS\nTUV")
        self.assertEqual(self.stop_recording(), "UPDATEDHIJKLMNOPQRS\nTUV\n")

    def test_start_inside_alternate_screen_and_later_return_to_main(self):
        self.create_pane(30, 6)
        self.emit(b"MAIN\r\nSAVED\x1b[?1049h\x1b[HEDITOR", "EDITOR")
        self.start_recording()
        self.emit(b"\rCHANGED\x1b[?1049l\r\nAFTER", "MAIN\nSAVED\nAFTER")
        self.assertEqual(self.stop_recording(), "MAIN\nSAVED\nAFTER\n")

    def test_stop_inside_preexisting_alternate_screen_keeps_saved_main(self):
        self.create_pane(30, 6)
        self.emit(b"MAIN\r\nSAVED\x1b[?1049h\x1b[HEDITOR", "EDITOR")
        self.start_recording()
        self.emit(b"\rCHANGED", "CHANGED")
        self.assertEqual(self.stop_recording(), "MAIN\nSAVED\n")
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane,
                                  "#{alternate_on}").strip(), b"1")

    def test_pending_osc_at_start_does_not_become_visible_text(self):
        self.create_pane(30, 6)
        self.emit(b"before\x1b]0;pending", "before")
        self.wait_for(lambda: b"pending" in self.tmux("capture-pane", "-p", "-P",
                      "-t", self.pane), "incomplete OSC in tmux parser")
        self.start_recording()
        self.emit(b"title\x07AFTER", "beforeAFTER")
        self.assertEqual(self.stop_recording(), "beforeAFTER\n")

    def test_burst_is_recorded_once_and_other_pane_output_is_excluded(self):
        self.create_pane(80, 6)
        other = self.split(30)
        self.start_recording()
        self.emit(b"OTHER-PANE-SECRET", "OTHER-PANE-SECRET", pane=other)
        lines = [f"row-{index:04d}" for index in range(400)]
        self.emit("\r\n".join(lines).encode(), "\n".join(lines[-6:]))
        self.assertEqual(self.stop_recording(), "\n".join(lines) + "\n")

    def test_batched_height_resizes_preserve_the_intermediate_deletions(self):
        self.create_pane(20, 6)
        self.emit(b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\x1b[2;1H",
                  "one\ntwo\nthree\nfour\nfive\nsix")
        self.start_recording()
        self.tmux("resize-window", "-t", self.pane, "-x", "20", "-y", "3", ";",
                  "resize-window", "-t", self.pane, "-x", "20", "-y", "6")
        self.wait_for(lambda: self.visible() == "one\ntwo\nthree", "batched resize result")
        self.emit(b"UPDATED", "one\nUPDATED\nthree")
        self.assertEqual(self.stop_recording(), "one\nUPDATED\nthree\n")

    def test_shell_entry_point_bootstraps_the_active_pane(self):
        self.create_pane(30, 6)
        self.emit(b"EXISTING\r\n", "EXISTING")
        self.start_recording(shell_entry=True)
        self.emit(b"NEW", "EXISTING\nNEW")
        self.assertEqual(self.stop_recording(), "EXISTING\nNEW\n")

    def test_python_relay_preserves_quoted_installation_path(self):
        self.create_pane(30, 6)
        directory = self.root / "plugin's #[default] %Y\n#literal second line"
        shutil.copytree(SCRIPT.parent, directory)
        self.start_recording(script=directory / SCRIPT.name)
        self.emit(b"RECORDED", "RECORDED")
        self.assertEqual(self.stop_recording(), "RECORDED\n")

    def test_initial_origin_mode_and_scrolling_margins(self):
        self.create_pane(30, 6)
        self.emit(b"\x1b[2;5r\x1b[?6h\x1b[2;3HSEED", "\n\n  SEED")
        self.start_recording()
        self.emit(b"\x1b[1;1HNEW", "\nNEW\n  SEED")
        self.assertEqual(self.stop_recording(), "\nNEW\n  SEED\n")

    def test_initial_insert_mode(self):
        self.create_pane(30, 6)
        self.emit(b"ABCDE\x1b[1;3H\x1b[4h", "ABCDE")
        self.start_recording()
        self.emit(b"Z", "ABZCDE")
        self.assertEqual(self.stop_recording(), "ABZCDE\n")

    def test_initial_disabled_autowrap(self):
        self.create_pane(10, 6)
        self.emit(b"\x1b[?7labcdefghijX", "abcdefghiX")
        self.start_recording()
        self.emit(b"YZ", "abcdefghiZ")
        self.assertEqual(self.stop_recording(), "abcdefghiZ\n")

    def test_history_before_start_is_not_recorded_after_height_growth(self):
        self.create_pane(30, 6)
        lines = [f"old-{index:02d}" for index in range(12)]
        self.emit("\r\n".join(lines).encode(), "\n".join(lines[-6:]))
        self.start_recording()
        self.resize(30, 10)
        self.wait_for(lambda: self.visible() == "\n".join(lines[-10:]), "old history restoration")
        self.assertEqual(self.stop_recording(), "\n".join(lines[-6:]) + "\n")

    def test_capture_preserves_literal_backslashes_and_tabs(self):
        self.create_pane(40, 6)
        self.emit(b"path\\123\\name\r\nx\ty", "path\\123\\name\nx\ty")
        self.start_recording()
        self.emit(b"Z", "path\\123\\name\nx\tyZ")
        self.assertEqual(self.stop_recording(), "path\\123\\name\nx       yZ\n")

    def test_resize_and_stop_in_one_command_batch(self):
        self.create_pane(20, 6)
        self.emit(b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\x1b[2;1H",
                  "one\ntwo\nthree\nfour\nfive\nsix")
        self.start_recording()
        self.tmux("resize-window", "-t", self.pane, "-y", "3", ";",
                  "resize-window", "-t", self.pane, "-y", "6", ";",
                  "pipe-pane", "-t", self.pane)
        self.wait_for(lambda: not self.tmux("list-clients", "-F", "#{client_name}"),
                      "resize and stop completion")
        self.assertEqual(self.log.read_text(), "one\ntwo\nthree\n")

    def test_resize_inside_alternate_keeps_the_known_main_screen(self):
        self.create_pane(30, 6)
        prior = [f"OLD-{index}" for index in range(6)]
        self.emit(("\r\n".join(prior) + "\r\nABCDEFGHIJKLMNOPQRST").encode(),
                  "\n".join(prior[1:] + ["ABCDEFGHIJKLMNOPQRST"]))
        self.start_recording()
        self.emit(b"\x1b[?1049h\x1b[HEDITOR", "EDITOR")
        self.resize(10, 6)
        self.emit(b"\x1b[HCHANGED", "CHANGED")
        self.assertEqual(self.stop_recording(),
                         "\n".join(prior[1:] + ["ABCDEFGHIJKLMNOPQRST"]) + "\n")

    def test_worker_signals_flush_and_release_the_pipe(self):
        self.create_pane(30, 6)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                self.emit(b"\x1b[H\x1b[2JSAVED", "SAVED")
                self.start_recording()
                client_pid = self.tmux("list-clients", "-F", "#{client_pid}").decode().strip()
                worker_pid = int(subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", client_pid], timeout=TIMEOUT).strip())
                os.kill(worker_pid, signum)
                self.wait_for(lambda: not self.tmux("list-clients", "-F", "#{client_name}"),
                              "signal shutdown")
                self.assertEqual(self.log.read_text(), "SAVED\n")
                self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane,
                                          "#{pane_pipe}").strip(), b"0")
                self.log.unlink()


if __name__ == "__main__":
    unittest.main()
