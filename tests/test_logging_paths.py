"""Keep log paths literal through tmux and each logging backend."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest

import test_tmux_logging_sync as sync_tests


START = Path(__file__).resolve().parents[1] / "scripts" / "start_logging.sh"
TOGGLE = START.with_name("toggle_logging.sh")
BASH = shutil.which("bash")
SED = shutil.which("sed")
CAT = shutil.which("cat")


@unittest.skipUnless(shutil.which("tmux") and BASH and SED and CAT,
                     "requires tmux, bash, sed and cat")
class LoggingPathTests(unittest.TestCase):
    # Reuse the real PTY fixture without inheriting its synchronization tests.
    wait_for = sync_tests.TmuxSynchronizationTests.wait_for
    worker_command = sync_tests.TmuxSynchronizationTests.worker_command
    create_pane = sync_tests.TmuxSynchronizationTests.create_pane
    dimensions = sync_tests.TmuxSynchronizationTests.dimensions
    capture = sync_tests.TmuxSynchronizationTests.capture
    visible = sync_tests.TmuxSynchronizationTests.visible
    emit = sync_tests.TmuxSynchronizationTests.emit
    cleanup_server = sync_tests.TmuxSynchronizationTests.cleanup_server

    def setUp(self):
        sync_tests.TmuxSynchronizationTests.setUp(self)
        self.marker = self.root / "unexpected-command"
        self.branch = self.root / "filter-arguments"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.hook = self.root / "force-fallback.bash"
        # pipe-pane inherits the tmux server process's original environment,
        # so these overrides must exist before starting the isolated server.
        self.server_env = dict(os.environ)
        self.server_env.update(
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            LOGGING_TEST_MARKER=str(self.marker),
            LOGGING_TEST_BRANCH=str(self.branch),
        )
        self.write_executable("ansifilter", "#!/bin/sh\n"
                              'printf "ansifilter\\n" > "$LOGGING_TEST_BRANCH"\n'
                              f"exec {shlex.quote(CAT)}\n")
        self.write_executable("sed", "#!/bin/sh\n"
                              'printf "%s\\n" "$@" > "$LOGGING_TEST_BRANCH"\n'
                              f"exec {shlex.quote(SED)} \"$@\"\n")
        self.create_pane(cols=80, rows=8)
        self.env = dict(self.server_env)
        self.env.update(
            TMUX=f"{self.socket},{self.tmux('display-message', '-p', '#{pid}').decode().strip()},0",
            TMUX_PANE=self.pane,
            BASH_ENV=str(self.hook),
        )

    def tmux(self, *args, check=True):
        return subprocess.run(self.base + list(args), env=self.server_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=sync_tests.TIMEOUT, check=check).stdout

    def write_executable(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o700)

    def select_fallback(self, branch):
        if branch == "python":
            self.env.pop("BASH_ENV")
            return
        # The hook is read only by fixture-launched bash processes. The real
        # Python PTY worker and the system's installed tools remain untouched.
        ansifilter_status = "0" if branch == "ansifilter" else "1"
        operating_system = "Darwin" if branch == "sed-osx" else "Linux"
        self.hook.write_text(
            "type() {\n"
            '    case "$1" in\n'
            "        python3) return 1 ;;\n"
            f"        ansifilter) return {ansifilter_status} ;;\n"
            '        *) builtin type "$@" ;;\n'
            "    esac\n"
            "}\n"
            f"uname() {{ printf '%s\\n' {operating_system}; }}\n"
        )

    def run_entry(self, *args, toggle=False):
        result = subprocess.run([BASH, str(TOGGLE if toggle else START), *args],
                                env=self.env, capture_output=True,
                                timeout=sync_tests.TIMEOUT)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode(errors="replace"))

    def assert_literal_recording(self, path, branch, toggle=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing\n")
        self.branch.unlink(missing_ok=True)
        self.emit(b"\x1b[2J\x1b[H", "")
        try:
            self.run_entry(*([] if toggle else [str(path)]), toggle=toggle)
            if branch != "python":
                selected = self.wait_for(
                    lambda: self.branch.exists() and self.branch.read_text().splitlines(),
                    f"{branch} filter startup")[0]
                self.assertEqual(selected, {"ansifilter": "ansifilter",
                                            "sed": "-r", "sed-osx": "-E"}[branch])
            self.emit(b"appended\n", "appended")
        finally:
            self.tmux("pipe-pane", "-t", self.pane, check=False)
        # sed may buffer its output until the pipe reaches EOF.
        self.wait_for(lambda: path.read_bytes() == b"existing\nappended\n",
                      f"append to literal filename {path.name!r}")
        self.assertFalse(self.marker.exists(), "filename executed a command")
        self.assertEqual(path.read_bytes(), b"existing\nappended\n")
        if branch == "python":
            self.wait_for(lambda: not self.tmux("list-clients", "-F", "#{client_name}"),
                          "logging control-client shutdown")

    def exercise_paths(self, branch):
        self.select_fallback(branch)
        filenames = [
            "spaces and apostrophe ' and double quote \".log",
            'substitution-$(touch "$LOGGING_TEST_MARKER").log',
            'backticks-`touch "$LOGGING_TEST_MARKER"`.log',
            '; touch "$LOGGING_TEST_MARKER"; #',
            'single-quote\'; touch "$LOGGING_TEST_MARKER"; #',
            'first\ntouch "$LOGGING_TEST_MARKER"\n#',
            "trailing-newline.log\n",
            "literal-#{pane_id}-%Y-%%-##.log",
            "literal-#[bold]-##[fg=red].log",
            'format-#(touch "$LOGGING_TEST_MARKER").log',
            "журнал-雪.log",
        ]
        for filename in filenames:
            with self.subTest(branch=branch, filename=filename):
                self.assert_literal_recording(self.root / filename, branch)
        with self.subTest(branch=branch, special_directory=True):
            self.assert_literal_recording(
                self.root / "directory ' \" #{pane_id} %Y" / "recording.log",
                branch)

    def test_ansifilter_paths_are_literal_and_append(self):
        self.exercise_paths("ansifilter")

    def test_linux_sed_paths_are_literal_and_append(self):
        self.exercise_paths("sed")

    def test_macos_sed_branch_paths_are_literal_and_append(self):
        # Exercise the macOS branch and -E on the host sed. Native macOS ANSI
        # filtering is a separate compatibility issue, not asserted here.
        self.exercise_paths("sed-osx")

    @unittest.skipUnless(sync_tests.supported_tmux(), "requires tmux 3.7 or newer")
    def test_python_paths_are_literal_and_append(self):
        self.exercise_paths("python")

    def test_session_metadata_stays_literal_after_filename_expansion(self):
        self.select_fallback("ansifilter")
        session = ('session \' " $(touch "$LOGGING_TEST_MARKER") '
                   '`touch "$LOGGING_TEST_MARKER"` ; #{pane_id} '
                   '#(touch "$LOGGING_TEST_MARKER") %Y Ж')
        self.tmux("rename-session", "-t", self.pane, session.replace("#", "##"))
        # rename-session itself interprets tmux formats in the requested name.
        session = self.tmux("display-message", "-p", "-t", self.pane,
                            "#{session_name}").decode()[:-1]
        self.tmux("set-option", "-g", "@logging-path", str(self.root))
        self.tmux("set-option", "-g", "@logging-filename",
                  "from-#{session_name}.log")
        self.assert_literal_recording(self.root / f"from-{session}.log",
                                      "ansifilter", toggle=True)


if __name__ == "__main__":
    unittest.main()
