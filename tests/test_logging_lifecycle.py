"""Logging follows stable panes and actual pipe lifetime on isolated tmux."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import unittest

import test_logging_paths as paths
import test_tmux_logging_sync as integration


TMUX = shutil.which('tmux')
START = paths.START
TOGGLE = paths.TOGGLE


@unittest.skipUnless(integration.supported_tmux() and paths.BASH and paths.CAT,
                     'requires tmux 3.7+, bash and cat')
class LoggingLifecycleTests(unittest.TestCase):
    tmux = paths.LoggingPathTests.tmux
    wait_for = paths.LoggingPathTests.wait_for
    worker_command = paths.LoggingPathTests.worker_command
    create_pane = paths.LoggingPathTests.create_pane
    dimensions = paths.LoggingPathTests.dimensions
    capture = paths.LoggingPathTests.capture
    visible = paths.LoggingPathTests.visible
    emit = paths.LoggingPathTests.emit
    cleanup_server = paths.LoggingPathTests.cleanup_server
    write_executable = paths.LoggingPathTests.write_executable
    split = integration.TmuxSynchronizationTests.split

    def setUp(self):
        paths.LoggingPathTests.setUp(self)
        self.messages = self.root / 'display-messages'
        # Observe user notifications without depending on private state names.
        # The server already inherits this bin directory for its child workers.
        self.write_executable('tmux',
            '#!/bin/sh\n'
            'logging_display=0\nlogging_query=0\n'
            'for logging_argument do\n'
            '  case "$logging_argument" in\n'
            '    display-message) logging_display=1 ;;\n'
            '    -p) logging_query=1 ;;\n'
            '  esac\n'
            'done\n'
            'if [ "$logging_display" = 1 ] && [ "$logging_query" = 0 ]; then\n'
            f'  printf "%s\\0" "$@" >> {shlex.quote(str(self.messages))}\n'
            'fi\n'
            f'exec {shlex.quote(TMUX)} "$@"\n')
        self.set_backend('python')
        self.configure('recording.log')

    def set_backend(self, backend):
        self.env['BASH_ENV'] = str(self.hook)
        paths.LoggingPathTests.select_fallback(self, backend)

    def configure(self, filename, directory=None):
        directory = self.root if directory is None else directory
        self.tmux('set-option', '-g', '@logging-path', str(directory))
        self.tmux('set-option', '-g', '@logging-filename', filename)
        return Path(directory) / filename

    def run_logging(self, filename=None, pane=None):
        env = dict(self.env, TMUX_PANE=pane or self.pane)
        command = ([paths.BASH, str(TOGGLE)] if filename is None else
                   [paths.BASH, str(START), str(filename)])
        return subprocess.run(command, env=env, capture_output=True, timeout=25)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def pane_pipe(self, pane=None):
        return self.tmux('display-message', '-p', '-t', pane or self.pane,
                         '#{pane_pipe}').strip() == b'1'

    def clients(self):
        return self.tmux('list-clients', '-F', '#{client_name}').splitlines()

    def notifications(self):
        if not self.messages.exists():
            return []
        return self.messages.read_text().split('\0')

    def wait_stopped(self, pane=None, other_clients=0):
        self.wait_for(lambda: not self.pane_pipe(pane), 'logging pipe shutdown')
        self.wait_for(lambda: len(self.clients()) == other_clients,
                      'logging control-client shutdown')

    def read_when(self, path, expected):
        self.wait_for(lambda: path.exists() and path.read_bytes() == expected,
                      f'log contents {expected!r}')

    def clear_screen(self):
        self.emit(b'\x1b[2J\x1b[H', '')

    def processes_for_role(self, role):
        """Find only this fixture's worker/relay, never another tmux session."""
        offset = {'worker': 1, 'relay': 2}[role]
        found = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                arguments = entry.joinpath('cmdline').read_bytes().split(b'\0')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            for index, argument in enumerate(arguments):
                if (argument == role.encode() and arguments[index + offset:index + offset + 2]
                        == [str(self.socket).encode(), self.pane.encode()]):
                    found.append(int(entry.name))
        return found

    @staticmethod
    def reap_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=integration.TIMEOUT)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def test_toggle_stops_same_recording_after_session_rename_and_pane_move(self):
        self.configure('#{session_name}-#{window_index}-#{pane_index}.log')
        original = self.root / 'recording-0-0.log'
        self.assert_success(self.run_logging())
        self.emit(b'before\r\n', 'before')
        self.tmux('rename-session', '-t', self.pane, 'renamed')

        command, ready, fd = self.worker_command()
        destination = self.tmux('new-window', '-d', '-P', '-F', '#{pane_id}',
                                '-t', 'renamed:7', command).decode().strip()
        self.writers.pop(None)
        self.writers[destination] = fd
        self.wait_for(ready.exists, 'destination PTY worker startup')
        self.tmux('move-pane', '-d', '-h', '-s', self.pane, '-t', destination)
        identity = self.tmux('display-message', '-p', '-t', self.pane,
                             '#{session_name}-#{window_index}-#{pane_index}').decode().strip()
        self.assertNotEqual(identity, 'recording-0-0')
        self.assertTrue(self.pane_pipe())
        self.emit(b'after\r\n', 'before\nafter')

        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(original, b'before\nafter\n')
        self.assertFalse((self.root / (identity + '.log')).exists())
        self.assertTrue(any(message.startswith('Ended logging')
                            for message in self.notifications()))

    def test_failed_file_open_does_not_claim_success_and_next_toggle_can_start(self):
        for backend in ('python', 'ansifilter', 'sed'):
            with self.subTest(backend=backend):
                self.set_backend(backend)
                self.clear_screen()
                target = self.configure(backend + '.log')
                target.mkdir()
                self.messages.unlink(missing_ok=True)
                failed = self.run_logging()
                self.assertNotEqual(failed.returncode, 0)
                self.wait_stopped()
                self.assertFalse(any(message.startswith('Started logging')
                                     for message in self.notifications()))
                target.rmdir()

                self.assert_success(self.run_logging())
                self.assertTrue(self.pane_pipe())
                self.emit(b'recovered\n', 'recovered')
                self.assert_success(self.run_logging())
                self.wait_stopped()
                self.read_when(target, b'recovered\n')

    def test_directory_creation_failure_has_no_success_or_active_pipe(self):
        obstacle = self.root / 'ordinary-file'
        obstacle.write_bytes(b'keep me')
        self.configure('recording.log', obstacle)
        self.messages.unlink(missing_ok=True)
        result = self.run_logging()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pane_pipe())
        self.assertFalse(self.clients())
        self.assertEqual(obstacle.read_bytes(), b'keep me')
        self.assertFalse(any(message.startswith('Started logging')
                             for message in self.notifications()))

    def test_start_preserves_foreign_pipe(self):
        foreign = self.root / 'foreign.log'
        self.tmux('pipe-pane', '-t', self.pane,
                  'exec ' + shlex.quote(paths.CAT) + ' >> ' + shlex.quote(str(foreign)))
        self.emit(b'before\r\n', 'before')
        expected = b'before\r\n'
        self.read_when(foreign, expected)
        for backend in ('python', 'ansifilter', 'sed'):
            with self.subTest(backend=backend):
                self.set_backend(backend)
                rejected = self.run_logging(self.root / (backend + '-rejected.log'))
                self.assertNotEqual(rejected.returncode, 0)
                self.assertTrue(self.pane_pipe())
                line = backend.encode() + b' still foreign\r\n'
                self.emit(line)
                expected += line
                self.read_when(foreign, expected)
                self.assertFalse(self.clients())

    def test_repeated_start_does_not_replace_existing_recording(self):
        first = self.configure('first.log')
        self.assert_success(self.run_logging())
        self.emit(b'before\r\n', 'before')
        rejected = self.run_logging(self.root / 'second.log')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertTrue(self.pane_pipe())
        self.emit(b'after\r\n', 'before\nafter')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(first, b'before\nafter\n')

    def test_concurrent_python_and_shell_starts_keep_one_owner_and_one_log(self):
        logs = [self.root / 'direct-python.log', self.root / 'shell-start.log']
        seeds = [b'existing Python\n', b'existing shell\n']
        for path, seed in zip(logs, seeds):
            path.write_bytes(seed)
        commands = (
            [sys.executable, '-B', str(START.with_name('tmux_logging.py')),
             'start', str(logs[0]), self.pane],
            [paths.BASH, str(START), str(logs[1])],
        )
        children = []
        for command in commands:
            child = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(self.reap_child, child)
            children.append(child)
        results = [child.communicate(timeout=25) for child in children]
        successes = [index for index, child in enumerate(children) if child.returncode == 0]
        self.assertEqual(len(successes), 1,
                         [(child.returncode, result[1])
                          for child, result in zip(children, results)])
        winner = successes[0]
        self.assertTrue(self.pane_pipe())
        self.wait_for(lambda: len(self.clients()) == 1, 'losing startup worker cleanup')
        self.emit(b'winner retained\r\n', 'winner retained')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(logs[winner], seeds[winner] + b'winner retained\n')
        self.assertEqual(logs[1 - winner].read_bytes(), seeds[1 - winner])

    def test_old_worker_cleanup_preserves_replacement_foreign_pipe(self):
        own = self.configure('own.log')
        self.assert_success(self.run_logging())
        self.emit(b'before\r\n', 'before')
        foreign = self.root / 'replacement.log'
        self.tmux('pipe-pane', '-t', self.pane,
                  'exec ' + shlex.quote(paths.CAT) + ' >> ' + shlex.quote(str(foreign)))
        self.wait_for(lambda: not self.clients(), 'replaced logging worker exit')
        self.assertTrue(self.pane_pipe())
        self.read_when(own, b'before\n')
        self.emit(b'foreign\r\n', 'before\nforeign')
        self.read_when(foreign, b'foreign\r\n')

    def test_external_stop_allows_next_toggle_to_start(self):
        first = self.configure('first.log')
        self.assert_success(self.run_logging())
        self.emit(b'first\r\n', 'first')
        self.tmux('pipe-pane', '-t', self.pane)
        self.wait_stopped()
        self.read_when(first, b'first\n')
        self.clear_screen()
        second = self.configure('second.log')
        self.assert_success(self.run_logging())
        self.assertTrue(self.pane_pipe())
        self.emit(b'second\r\n', 'second')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(second, b'second\n')

    def test_early_fallback_filter_exit_releases_pipe_and_next_toggle_can_start(self):
        self.set_backend('ansifilter')
        self.configure('failed-filter.log')
        self.write_executable('ansifilter', '#!/bin/sh\nexit 7\n')
        self.messages.unlink(missing_ok=True)
        result = self.run_logging()
        if result.returncode != 0:
            self.assertFalse(any(message.startswith('Started logging')
                                 for message in self.notifications()))
        # Startup and the child's immediate exit may race, but a dead filter
        # must release its pipe even when the PTY never produces another byte.
        self.wait_stopped()
        self.write_executable('ansifilter',
                              '#!/bin/sh\nexec ' + shlex.quote(paths.CAT) + '\n')
        recovered = self.configure('recovered-filter.log')
        self.assert_success(self.run_logging())
        self.assertTrue(self.pane_pipe())
        self.emit(b'recovered\n', 'recovered')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(recovered, b'recovered\n')

    @unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'requires Linux procfs')
    def test_idle_worker_termination_and_failure_release_pipe_and_allow_restart(self):
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=number):
                self.clear_screen()
                target = self.configure(f'worker-{number}.log')
                self.assert_success(self.run_logging())
                self.emit(b'before\r\n', 'before')
                worker_pids = self.processes_for_role('worker')
                self.assertEqual(len(worker_pids), 1, worker_pids)
                os.kill(worker_pids[0], number)
                # No further pane output is needed to detect a dead reader.
                self.wait_stopped()
                if number == signal.SIGTERM:
                    self.read_when(target, b'before\n')
                self.clear_screen()
                restart = self.configure(f'restarted-{number}.log')
                self.assert_success(self.run_logging())
                self.assertTrue(self.pane_pipe())
                self.emit(b'restarted\r\n', 'restarted')
                self.assert_success(self.run_logging())
                self.wait_stopped()
                self.read_when(restart, b'restarted\n')

    @unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'requires Linux procfs')
    def test_idle_relay_failure_releases_worker_and_allows_next_toggle(self):
        target = self.configure('relay-failure.log')
        self.assert_success(self.run_logging())
        self.emit(b'before\r\n', 'before')
        self.clear_screen()
        # Confirm all earlier bytes reached the worker before killing its
        # reader; this test targets idle cleanup, not delivery after SIGKILL.
        self.read_when(target, b'before\n')
        relay_pids = self.processes_for_role('relay')
        self.assertEqual(len(relay_pids), 1, relay_pids)
        os.kill(relay_pids[0], signal.SIGKILL)
        self.wait_stopped()
        self.read_when(target, b'before\n')
        self.clear_screen()
        restarted = self.configure('after-relay-failure.log')
        self.assert_success(self.run_logging())
        self.assertTrue(self.pane_pipe())
        self.emit(b'restarted\r\n', 'restarted')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(restarted, b'restarted\n')

    @unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'requires Linux procfs')
    def test_delayed_old_worker_cleanup_preserves_new_recording_owner(self):
        previous = self.configure('previous.log')
        self.assert_success(self.run_logging())
        self.emit(b'before\r\n', 'before')
        worker_pids = self.processes_for_role('worker')
        self.assertEqual(len(worker_pids), 1, worker_pids)
        old_pid = worker_pids[0]

        def resume_old_worker():
            if old_pid in self.processes_for_role('worker'):
                os.kill(old_pid, signal.SIGCONT)

        self.addCleanup(resume_old_worker)
        os.kill(old_pid, signal.SIGSTOP)
        self.tmux('pipe-pane', '-t', self.pane)
        replacement = self.root / 'replacement-own.log'
        self.assert_success(self.run_logging(replacement))
        self.assertTrue(self.pane_pipe())
        self.emit(b'after\r\n', 'before\nafter')
        resume_old_worker()
        self.wait_for(lambda: len(self.clients()) == 1, 'old recording cleanup')
        self.assertTrue(self.pane_pipe())
        self.read_when(previous, b'before\n')
        self.emit(b'survives\r\n', 'before\nafter\nsurvives')
        self.assert_success(self.run_logging())
        self.wait_stopped()
        self.read_when(replacement, b'before\nafter\nsurvives\n')

    def test_toggling_one_pane_leaves_other_recording_running(self):
        second_pane = self.split(35)
        first_log = self.configure('first-pane.log')
        self.assert_success(self.run_logging(pane=self.pane))
        second_log = self.configure('second-pane.log')
        self.assert_success(self.run_logging(pane=second_pane))
        self.emit(b'first\r\n', 'first', pane=self.pane)
        self.emit(b'second\r\n', 'second', pane=second_pane)

        self.assert_success(self.run_logging(pane=self.pane))
        self.wait_stopped(self.pane, other_clients=1)
        self.assertTrue(self.pane_pipe(second_pane))
        self.read_when(first_log, b'first\n')
        self.emit(b'continues\r\n', 'second\ncontinues', pane=second_pane)
        self.assert_success(self.run_logging(pane=second_pane))
        self.wait_stopped(second_pane)
        self.read_when(second_log, b'second\ncontinues\n')


if __name__ == '__main__':
    unittest.main()
