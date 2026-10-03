"""Capture screen/history safely at literal paths and preserve files on errors."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import test_tmux_logging_sync as sync_tests


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BASH = shutil.which("bash")
ENTRIES = (
    ("screen_capture.sh", "screen-capture"),
    ("save_complete_history.sh", "save-complete-history"),
)


@unittest.skipUnless(shutil.which("tmux") and BASH, "requires tmux and bash")
class CapturePathTests(unittest.TestCase):
    # Share only the isolated PTY fixture, without inheriting its test methods.
    wait_for = sync_tests.TmuxSynchronizationTests.wait_for
    worker_command = sync_tests.TmuxSynchronizationTests.worker_command
    create_pane = sync_tests.TmuxSynchronizationTests.create_pane
    dimensions = sync_tests.TmuxSynchronizationTests.dimensions
    capture = sync_tests.TmuxSynchronizationTests.capture
    visible = sync_tests.TmuxSynchronizationTests.visible
    emit = sync_tests.TmuxSynchronizationTests.emit
    tmux = sync_tests.TmuxSynchronizationTests.tmux
    cleanup_server = sync_tests.TmuxSynchronizationTests.cleanup_server

    def setUp(self):
        sync_tests.TmuxSynchronizationTests.setUp(self)
        self.create_pane(cols=24, rows=8)
        pid = self.tmux("display-message", "-p", "#{pid}").decode().strip()
        self.env = dict(os.environ, TMUX=f"{self.socket},{pid},0", TMUX_PANE=self.pane)

    def run_capture(self, script, option, destination):
        self.tmux("set-option", "-g", f"@{option}-path", str(destination.parent))
        self.tmux("set-option", "-g", f"@{option}-filename", destination.name)
        return subprocess.run([BASH, str(SCRIPTS / script)], env=self.env,
                              capture_output=True, timeout=sync_tests.TIMEOUT)

    def exercise_capture(self, script, option, history):
        history_lines = [f"history-{index:02d}" for index in range(18)]
        self.emit("\r\n".join(history_lines).encode())
        self.wait_for(lambda: "history-17" in self.visible(), "history fixture output")
        # Start at a known cursor position, retaining the older scrollback.
        content = "wrap-" + "x" * 35 + "\r\nlast nonempty  \r\n\r\ninside blank\r\n"
        self.emit(b"\x1b[2J\x1b[H" + content.encode())
        self.wait_for(lambda: "inside blank" in self.visible(), "capture fixture output")
        # Internal blanks and spaces must survive; only empty trailing rows
        # are removed. Wrapped text must follow capture-pane -J semantics.
        flags = ["-S", "-"] if history else []
        original = self.tmux("capture-pane", "-J", "-p", "-t", self.pane, *flags)
        expected = original.rstrip(b"\n") + b"\n"
        self.assertIn(b"last nonempty  \n\ninside blank\n", expected)
        if history:
            self.assertIn(b"history-00\n", expected)
            self.assertGreater(len(expected.splitlines()), 8)
        else:
            self.assertNotIn(b"history-00\n", expected)
        cases = (
            ("directory with spaces", "capture with spaces.log"),
            ("directory ' and \" quotes", "capture ' and \" quotes.log"),
            ("glob [xy]*? directory", "capture [ab]*?.log"),
            ("каталог 雪", "снимок 雪.log"),
            ("tab\tdirectory", "tab\tcapture.log"),
            ("line\nbreak directory", "line\nbreak capture.log"),
            ("ending space ", "ending space.log "),
            ("ending newline\n", "ending newline.log\n"),
        )
        for directory, filename in cases:
            with self.subTest(script=script, directory=directory, filename=filename):
                destination = self.root / directory / filename
                destination.parent.mkdir()
                destination.write_bytes(b"previous capture\n")
                result = self.run_capture(script, option, destination)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(destination.read_bytes(), expected)
                self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_screen_capture_at_literal_paths(self):
        self.exercise_capture("screen_capture.sh", "screen-capture", history=False)

    def test_complete_history_at_literal_paths(self):
        self.exercise_capture("save_complete_history.sh", "save-complete-history", history=True)

    def test_empty_capture_creates_parent_and_keeps_one_newline(self):
        for script, option in ENTRIES:
            with self.subTest(script=script):
                destination = self.root / f"new {option} directory" / "empty capture.log"
                self.assertFalse(destination.parent.exists())
                result = self.run_capture(script, option, destination)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(destination.read_bytes(), b"\n")
                self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_success_replaces_symlink_without_changing_target(self):
        self.emit(b"captured snapshot\r\n", "captured snapshot")
        for script, option in ENTRIES:
            with self.subTest(script=script):
                directory = self.root / f"{option} symlink directory"
                directory.mkdir()
                target = directory / "original target.log"
                target.write_bytes(b"previous target contents\n")
                target.chmod(0o640)
                destination = directory / "capture symlink.log"
                destination.symlink_to(target.name)
                self.assertTrue(destination.is_symlink())
                result = self.run_capture(script, option, destination)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertFalse(destination.is_symlink())
                self.assertTrue(destination.is_file())
                self.assertEqual(destination.read_bytes(), b"captured snapshot\n")
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
                self.assertEqual(target.read_bytes(), b"previous target contents\n")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                self.assertEqual(set(directory.iterdir()), {destination, target})

    def test_directory_target_is_rejected_without_changes(self):
        self.emit(b"capture output", "capture output")
        for script, option in ENTRIES:
            with self.subTest(script=script):
                parent = self.root / f"{option} directory"
                destination = parent / "destination.log"
                destination.mkdir(parents=True)
                marker = destination / "existing content"
                marker.write_bytes(b"must remain unchanged\n")
                result = self.run_capture(script, option, destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(destination.is_dir())
                self.assertEqual(marker.read_bytes(), b"must remain unchanged\n")
                self.assertEqual(list(destination.iterdir()), [marker])
                self.assertEqual(list(parent.iterdir()), [destination])


FAKE_TMUX = r"""
import os
from pathlib import Path
import sys

args = sys.argv[1:]
destination = Path(os.environ["CAPTURE_DESTINATION"])
if args == ["-V"]:
    print("tmux 3.7c")
elif args[0] == "show-option":
    option = args[-1]
    if option in ("@screen-capture-path", "@save-complete-history-path"):
        print(destination.parent)
    elif option in ("@screen-capture-filename", "@save-complete-history-filename"):
        print(destination.name)
elif args[0] == "display-message":
    if "-p" in args:
        print(args[-1].replace("#{history_limit}", "2000"), flush=True)
        if os.environ["CAPTURE_FAILURE"] == "format":
            print("injected format failure", file=sys.stderr)
            sys.exit(31)
    else:
        with open(os.environ["CAPTURE_MESSAGES"], "a") as messages:
            messages.write(args[-1] + "\n")
elif args[0] == "capture-pane":
    sys.stdout.buffer.write(b"alpha  \n\nbeta \n\n\n")
    sys.stdout.buffer.flush()
    if os.environ["CAPTURE_FAILURE"] == "capture":
        print("injected capture failure", file=sys.stderr)
        sys.exit(31)
"""


@unittest.skipUnless(BASH, "requires bash")
class CaptureFailureTests(unittest.TestCase):
    def write_executable(self, directory, name, source):
        path = directory / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)

    def exercise_failure(self, stage):
        for script, _ in ENTRIES:
            for existing in (False, True):
                with self.subTest(stage=stage, script=script, existing=existing):
                    with tempfile.TemporaryDirectory(prefix="capture-failure-") as temporary:
                        root = Path(temporary)
                        directory = root / "captures with spaces"
                        directory.mkdir()
                        destination = directory / "capture ' [x].log"
                        previous = b"previous successful capture\n"
                        if existing:
                            destination.write_bytes(previous)
                        before = set(directory.iterdir())
                        binary_directory = root / "bin"
                        binary_directory.mkdir()
                        self.write_executable(binary_directory, "tmux", FAKE_TMUX)
                        if stage not in ("capture", "format"):
                            self.write_executable(
                                binary_directory, stage,
                                "import sys\n"
                                + ("sys.stdin.buffer.read()\n"
                                   "sys.stdout.buffer.write(b'partial processing output\\n')\n"
                                   "sys.stdout.buffer.flush()\n" if stage == "awk" else "")
                                + f"print('injected {stage} failure', file=sys.stderr)\n"
                                  "sys.exit(32)\n")
                        messages = root / "messages"
                        env = dict(os.environ,
                                   PATH=str(binary_directory) + os.pathsep + os.environ.get("PATH", ""),
                                   CAPTURE_DESTINATION=str(destination),
                                   CAPTURE_MESSAGES=str(messages), CAPTURE_FAILURE=stage)
                        result = subprocess.run([BASH, str(SCRIPTS / script)], env=env,
                                                capture_output=True, timeout=sync_tests.TIMEOUT)
                        self.assertNotEqual(result.returncode, 0,
                                            "capture failure was reported as success")
                        self.assertIn(f"injected {stage} failure".encode(), result.stderr,
                                      "fixture did not reach the intended failure")
                        if existing:
                            self.assertEqual(destination.read_bytes(), previous)
                        else:
                            self.assertFalse(destination.exists())
                        self.assertEqual(set(directory.iterdir()), before,
                                         "failed capture left a temporary output file")
                        output = result.stdout + result.stderr
                        if messages.exists():
                            output += messages.read_bytes()
                        self.assertNotIn(b"saved to", output.lower())

    def test_format_failure_preserves_destination(self):
        self.exercise_failure("format")

    def test_directory_creation_failure_preserves_destination(self):
        self.exercise_failure("mkdir")

    def test_capture_failure_preserves_destination(self):
        self.exercise_failure("capture")

    def test_processing_failure_preserves_destination(self):
        self.exercise_failure("awk")

    def test_replacement_failure_preserves_destination(self):
        self.exercise_failure("mv")

    def test_temporary_file_failure_preserves_destination(self):
        self.exercise_failure("mktemp")


if __name__ == "__main__":
    unittest.main()
