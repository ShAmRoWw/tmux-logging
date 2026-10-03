"""Filename metadata describes the actual file until its writer has closed."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

import test_logging_lifecycle as lifecycle
import test_logging_paths as paths
import test_tmux_logging_sync as integration


@unittest.skipUnless(integration.supported_tmux() and paths.BASH and paths.SED and paths.CAT,
                     'requires tmux 3.7+, bash, sed and cat')
class LoggingFilenameStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle.LoggingLifecycleTests('test_start_preserves_foreign_pipe')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def file_option(self):
        case = self.fixture
        owner = case.tmux('show-option', '-gqv', '@tmux-logging-' + case.pane).decode().strip()
        self.assertRegex(owner, r'^[a-zA-Z0-9_-]+:[0-9]+$')
        return '@tmux-logging-file-' + owner

    def assert_finished_file(self, option, target, expected):
        case = self.fixture
        case.wait_for(lambda: not case.tmux('show-option', '-gqv', option),
                      'filename completion barrier')
        # No second poll for contents: option removal must already imply that
        # all final output is readable, including a Python screen flush.
        self.assertEqual(target.read_bytes(), expected)
        case.wait_stopped()

    def test_all_backends_publish_literal_absolute_filename_until_headless_stop(self):
        case = self.fixture
        for backend in ('python', 'ansifilter', 'sed', 'sed-osx'):
            with self.subTest(backend=backend):
                case.clear_screen()
                case.set_backend(backend)
                target = case.root / (backend + " ' \" #{pane_id} %Y $(literal)\n.log\n;")
                target.write_bytes(b'existing\n')
                case.assert_success(case.run_logging(target))
                option = self.file_option()
                self.assertEqual(case.tmux('show-option', '-gqv', option),
                                 (str(target) + '\n').encode())
                case.emit(b'TAIL\n', expected='TAIL')
                # Calling toggle directly must stop without opening a prompt
                # or changing the exact filename selected at startup.
                case.assert_success(case.run_logging())
                self.assert_finished_file(option, target, b'existing\nTAIL\n')
                self.assertIn('Ended logging', case.notifications())

    def test_direct_python_start_normalizes_relative_filename_without_expansion(self):
        case = self.fixture
        relative = Path("relative '#{pane_id}' %Y\n.log;")
        target = case.root / relative
        result = subprocess.run(
            [sys.executable, '-B', str(paths.START.with_name('tmux_logging.py')),
             'start', str(relative), case.pane], cwd=case.root, env=case.env,
            capture_output=True, timeout=25)
        case.assert_success(result)
        option = self.file_option()
        self.assertEqual(case.tmux('show-option', '-gqv', option),
                         (str(target) + '\n').encode())
        case.emit(b'PENDING', expected='PENDING')
        case.tmux('pipe-pane', '-t', case.pane)
        self.assert_finished_file(option, target, b'PENDING\n')

    @unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'requires Linux procfs')
    def test_python_metadata_outlives_relay_end_until_worker_flush(self):
        case = self.fixture
        target = case.root / 'delayed-python.log'
        case.assert_success(case.run_logging(target))
        option = self.file_option()
        workers = case.processes_for_role('worker')
        self.assertEqual(len(workers), 1, workers)
        worker = workers[0]

        def resume():
            try:
                os.kill(worker, signal.SIGCONT)
            except ProcessLookupError:
                pass

        self.addCleanup(resume)
        os.kill(worker, signal.SIGSTOP)
        case.emit(b'WAITING FOR FINAL FLUSH', expected='WAITING FOR FINAL FLUSH')
        case.tmux('pipe-pane', '-t', case.pane)
        case.wait_for(lambda: not case.processes_for_role('relay'), 'ordinary relay EOF exit')
        self.assertEqual(case.tmux('show-option', '-gqv', option),
                         (str(target) + '\n').encode())
        self.assertEqual(target.read_bytes(), b'')
        resume()
        self.assert_finished_file(option, target, b'WAITING FOR FINAL FLUSH\n')

    def test_fallback_metadata_outlives_eof_until_buffered_filter_finishes(self):
        case = self.fixture
        drained = case.root / 'filter-at-eof'
        release = case.root / 'release-filter'
        case.write_executable('ansifilter',
            '#!' + sys.executable + '\n'
            'from pathlib import Path\nimport sys\nimport time\n'
            'data = sys.stdin.buffer.read()\n'
            f'Path({str(drained)!r}).touch()\n'
            f'while not Path({str(release)!r}).exists():\n'
            '    time.sleep(0.01)\n'
            'sys.stdout.buffer.write(data)\n'
            'sys.stdout.buffer.flush()\n')
        case.set_backend('ansifilter')
        target = case.root / 'delayed-fallback.log'
        case.assert_success(case.run_logging(target))
        option = self.file_option()
        case.emit(b'BUFFERED TAIL\n', expected='BUFFERED TAIL')
        case.tmux('pipe-pane', '-t', case.pane)
        case.wait_for(drained.exists, 'filter received EOF')
        self.assertEqual(case.tmux('show-option', '-gqv', option),
                         (str(target) + '\n').encode())
        self.assertEqual(target.read_bytes(), b'')
        release.touch()
        self.assert_finished_file(option, target, b'BUFFERED TAIL\n')

    def test_headless_toggle_keeps_configured_filename(self):
        case = self.fixture
        target = case.configure('headless-original.log')
        case.assert_success(case.run_logging())
        option = self.file_option()
        self.assertEqual(case.tmux('show-option', '-gqv', option),
                         (str(target) + '\n').encode())
        case.emit(b'UNCHANGED', expected='UNCHANGED')
        case.assert_success(case.run_logging())
        self.assert_finished_file(option, target, b'UNCHANGED\n')
        self.assertTrue(target.exists())
        self.assertIn('Started logging', case.notifications())
        self.assertIn('Ended logging', case.notifications())


if __name__ == '__main__':
    unittest.main()
