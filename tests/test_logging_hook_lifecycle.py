"""After-hooks must not let an old logger claim or clear a replacement."""

import shlex
import unittest

import test_logging_lifecycle as lifecycle
import test_logging_paths as paths
import test_tmux_logging_sync as integration


@unittest.skipUnless(integration.supported_tmux() and paths.BASH and paths.CAT,
                     'requires tmux 3.7+, bash and cat')
class LoggingHookLifecycleTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated server without inheriting its unrelated tests.
        self.fixture = lifecycle.LoggingLifecycleTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def replacement_hook(self, filename, replacement_token=None):
        case = self.fixture
        commands = [
            ['set-hook', '-gu', 'after-pipe-pane'],
            ['pipe-pane', '-t', case.pane,
             'exec ' + shlex.quote(paths.CAT) + ' >> ' + shlex.quote(str(filename))],
        ]
        if replacement_token is not None:
            commands.append([
                'set-option', '-gF', '-t', case.pane,
                '@tmux-logging-' + case.pane, replacement_token + ':#{pane_pipe_pid}',
            ])
        case.tmux('set-hook', '-g', 'after-pipe-pane',
                  ' ; '.join(shlex.join(command) for command in commands))

    def finish_replacement(self):
        case = self.fixture
        case.tmux('pipe-pane', '-t', case.pane)
        case.wait_stopped()
        case.tmux('set-option', '-guq', '@tmux-logging-' + case.pane)

    def test_start_hook_replacement_is_not_claimed_or_closed(self):
        case = self.fixture
        for backend in ('python', 'ansifilter'):
            with self.subTest(backend=backend):
                case.set_backend(backend)
                case.clear_screen()
                case.configure('interrupted-start-' + backend + '.log')
                foreign = case.root / ('startup-replacement-' + backend + '.log')
                self.replacement_hook(foreign)

                result = case.run_logging()
                self.assertNotEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                case.wait_for(lambda: not case.clients(), 'interrupted logger shutdown')
                self.assertTrue(case.pane_pipe(), 'startup cleanup closed the replacement')
                owner = case.tmux('show-option', '-gqv', '@tmux-logging-' + case.pane).strip()
                self.assertEqual(owner, b'', 'failed startup claimed the replacement')
                case.emit(b'foreign remains\r\n', 'foreign remains')
                case.read_when(foreign, b'foreign remains\r\n')
                self.finish_replacement()

    def test_stop_hook_replacement_keeps_its_new_owner(self):
        case = self.fixture
        for backend in ('python', 'ansifilter'):
            with self.subTest(backend=backend):
                case.set_backend(backend)
                case.clear_screen()
                case.configure('stopping-' + backend + '.log')
                case.assert_success(case.run_logging())
                case.emit(b'own recording\r\n', 'own recording')
                foreign = case.root / ('stop-replacement-' + backend + '.log')
                replacement_token = 'a' * 32
                self.replacement_hook(foreign, replacement_token)

                case.assert_success(case.run_logging())
                case.wait_for(lambda: not case.clients(), 'old logger shutdown')
                self.assertTrue(case.pane_pipe(), 'old cleanup closed the replacement')
                current_pid = case.tmux('display-message', '-p', '-t', case.pane,
                                        '#{pane_pipe_pid}').strip()
                owner = case.tmux('show-option', '-gqv', '@tmux-logging-' + case.pane).strip()
                self.assertEqual(owner, replacement_token.encode() + b':' + current_pid,
                                 'old cleanup erased the replacement owner')
                case.emit(b'new recording\r\n', 'own recording\nnew recording')
                case.read_when(foreign, b'new recording\r\n')
                self.finish_replacement()


if __name__ == '__main__':
    unittest.main()
