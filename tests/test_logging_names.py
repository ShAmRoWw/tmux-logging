"""Exercise filename questions through a real interactive tmux client."""

import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import termios
import threading
import unittest

import test_logging_paths as paths
import test_tmux_logging_sync as integration


PLUGIN = paths.START.parents[1] / 'logging.tmux'
START_PROMPT = b'Log filename'
STOP_PROMPT = b'Save log as'


@unittest.skipUnless(integration.supported_tmux() and paths.BASH,
                     'requires tmux 3.7+ and bash')
class LoggingNameTests(unittest.TestCase):
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

    def setUp(self):
        paths.LoggingPathTests.setUp(self)
        self.env.pop('BASH_ENV')
        self.default = self.root / 'default.log'
        self.tmux('set-option', '-g', '@logging-path', str(self.root))
        self.tmux('set-option', '-g', '@logging-filename', self.default.name)
        self.tmux('set-option', '-g', 'status-keys', 'emacs')
        result = subprocess.run([str(PLUGIN)], env=self.env,
                                capture_output=True, timeout=integration.TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.attach_client()

    def attach_client(self):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 8, 80, 0, 0))
        self.display = bytearray()
        self.reader_done = threading.Event()

        def drain_display():
            while not self.reader_done.is_set():
                if not select.select([self.master], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.display.extend(data)

        self.reader = threading.Thread(target=drain_display, daemon=True)
        self.reader.start()
        try:
            self.client_process = subprocess.Popen(
                self.base + ['attach-session', '-t', 'recording'],
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(self.server_env, TERM='xterm-256color'), start_new_session=True)
        finally:
            os.close(slave)
        self.addCleanup(self.close_client)
        self.client = self.wait_for(
            lambda: self.tmux('list-clients', '-F', '#{client_name}').strip(),
            'interactive client attachment').decode()

    def close_client(self):
        if self.client_process.poll() is None:
            self.client_process.terminate()
        self.client_process.wait(timeout=integration.TIMEOUT)
        self.reader_done.set()
        self.reader.join(timeout=integration.TIMEOUT)
        os.close(self.master)

    def keys(self, *keys):
        self.tmux('send-keys', '-K', '-c', self.client, *keys)

    def toggle_prompt(self, stopping=False):
        offset = len(self.display)
        self.keys('C-b', 'P')
        prompt = STOP_PROMPT if stopping else START_PROMPT
        self.wait_for(lambda: prompt in self.display[offset:],
                      f'visible filename question {prompt!r}')
        return bytes(self.display[offset:])

    def answer(self, name=None):
        if name is not None:
            self.keys('C-u')
            # Type through the client's terminal. tmux's command-line parser
            # would consume a final semicolon even in a send-keys -l argument.
            data = name.encode() + b'\r'
            self.assertEqual(os.write(self.master, data), len(data))
        else:
            self.keys('Enter')
        self.wait_for(lambda: not self.tmux('show-messages', '-J'),
                      'submitted filename processing')

    def pipe_active(self):
        return self.tmux('display-message', '-p', '-t', self.pane,
                         '#{pane_pipe}').strip() == b'1'

    def wait_started(self):
        self.wait_for(self.pipe_active, 'logging pipe installation')

    def wait_stopped(self):
        self.wait_for(lambda: not self.pipe_active(), 'logging pipe shutdown')
        self.wait_for(lambda: self.tmux('list-clients', '-F', '#{client_name}').splitlines()
                      == [self.client.encode()], 'logging worker shutdown')

    def wait_content(self, path, expected):
        self.wait_for(lambda: path.exists() and path.read_bytes() == expected,
                      f'contents of {path.name!r}')

    def stop_and_keep(self, path, expected):
        shown = self.toggle_prompt(stopping=True)
        self.wait_stopped()
        self.assertIn(path.name.encode(), shown)
        self.answer()
        self.wait_content(path, expected)

    def test_binding_prompts_before_start_and_enter_accepts_default(self):
        shown = self.toggle_prompt()
        self.assertIn(self.default.name.encode(), shown)
        self.assertFalse(self.pipe_active())
        self.assertFalse(self.default.exists())
        self.answer()
        self.wait_started()
        self.emit(b'kept\r\n', 'kept')
        self.stop_and_keep(self.default, b'kept\n')

    def test_custom_name_is_used_at_start_and_kept_after_stop(self):
        destination = self.root / 'custom name.log'
        self.toggle_prompt()
        self.answer(destination.name)
        self.wait_started()
        self.emit(b'custom\r\n', 'custom')
        self.stop_and_keep(destination, b'custom\n')
        self.assertFalse(self.default.exists())

    def test_blank_answers_use_the_default_and_keep_the_current_name(self):
        self.toggle_prompt()
        self.answer('')
        self.wait_started()
        self.emit(b'blank answer\r\n', 'blank answer')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        self.answer('')
        self.wait_content(self.default, b'blank answer\n')

    def test_stop_flushes_before_renaming_and_preserves_all_recorded_text(self):
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'first\r\nlast without newline', 'first\nlast without newline')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        self.wait_content(self.default, b'first\nlast without newline\n')
        renamed = self.root / 'finished.log'
        self.answer(renamed.name)
        self.wait_content(renamed, b'first\nlast without newline\n')
        self.assertFalse(self.default.exists())

    def test_stop_keeps_actual_filename_after_session_and_default_name_change(self):
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'original name\r\n', 'original name')
        self.tmux('rename-session', '-t', self.pane, 'renamed session')
        self.tmux('set-option', '-g', '@logging-filename', 'different-default.log')
        self.stop_and_keep(self.default, b'original name\n')
        self.assertFalse((self.root / 'different-default.log').exists())

    def test_special_names_remain_literal_at_start_and_after_rename(self):
        name = ('literal \' " $(touch "$LOGGING_TEST_MARKER") '
                '`touch "$LOGGING_TEST_MARKER"` #{pane_id} %Y Ж.log')
        destination = self.root / name
        self.toggle_prompt()
        self.answer(name)
        self.wait_started()
        self.emit(b'literal\r\n', 'literal')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        self.wait_content(destination, b'literal\n')
        renamed = self.root / ('renamed \' " #{session_name} %Y 雪.log')
        self.answer(renamed.name)
        self.wait_content(renamed, b'literal\n')
        self.assertFalse(destination.exists())
        self.assertFalse(self.marker.exists(), 'a filename executed a command')

    def test_stop_prefill_round_trips_commas_formats_styles_and_semicolons(self):
        initial_files = {path.name for path in self.root.iterdir() if path.is_file()}
        names = ('comma,name.log', 'style #[fg=red] ## hash.log',
                 'literal %1 %% #{pane_id}.log', 'name;')
        for name in names:
            with self.subTest(name=name):
                self.emit(b'\x1b[2J\x1b[H', '')
                destination = self.root / name
                self.toggle_prompt()
                self.answer(name)
                self.wait_started()
                self.emit(b'literal name\r\n', 'literal name')
                self.toggle_prompt(stopping=True)
                self.wait_stopped()
                self.answer()
                self.wait_for(lambda: not self.tmux('show-messages', '-J'),
                              'filename question completion')
                self.wait_content(destination, b'literal name\n')
        self.assertEqual({path.name for path in self.root.iterdir() if path.is_file()},
                         initial_files | set(names))

    def test_directory_in_answer_is_rejected_at_start_and_stop(self):
        self.toggle_prompt()
        offset = len(self.display)
        self.answer('subdirectory/name.log')
        self.wait_for(lambda: b'Invalid log filename' in self.display[offset:],
                      'invalid start filename message')
        self.wait_for(lambda: not self.tmux('show-messages', '-J'),
                      'invalid start question completion')
        self.assertFalse(self.pipe_active())
        self.assertFalse(self.default.exists())
        self.assertFalse((self.root / 'subdirectory').exists())
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'keep original\r\n', 'keep original')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        offset = len(self.display)
        self.answer('subdirectory/name.log')
        self.wait_for(lambda: b'Invalid log filename' in self.display[offset:],
                      'invalid stop filename message')
        self.wait_for(lambda: not self.tmux('show-messages', '-J'),
                      'invalid stop question completion')
        self.wait_content(self.default, b'keep original\n')
        self.assertFalse((self.root / 'subdirectory').exists())

    def test_rename_collision_keeps_original_and_existing_destination(self):
        occupied = self.root / 'occupied.log'
        occupied.write_bytes(b'prior contents\n')
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'recording\r\n', 'recording')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        offset = len(self.display)
        self.answer(occupied.name)
        self.wait_for(lambda: b'Cannot rename log: filename already exists' in self.display[offset:],
                      'rename collision message')
        self.wait_content(self.default, b'recording\n')
        self.assertEqual(occupied.read_bytes(), b'prior contents\n')
        self.assertFalse(self.pipe_active())

    def test_escape_cancels_start_without_creating_a_log(self):
        self.toggle_prompt()
        self.keys('Escape')
        self.assertFalse(self.pipe_active())
        self.assertFalse(self.default.exists())
        # A second invocation must present the start prompt again.
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'after cancel\r\n', 'after cancel')
        self.stop_and_keep(self.default, b'after cancel\n')

    def test_escape_after_stop_keeps_the_original_file_and_recording_stopped(self):
        self.toggle_prompt()
        self.answer()
        self.wait_started()
        self.emit(b'kept after cancel\r\n', 'kept after cancel')
        self.toggle_prompt(stopping=True)
        self.wait_stopped()
        self.keys('Escape')
        self.wait_content(self.default, b'kept after cancel\n')
        self.assertFalse(self.pipe_active())

    def test_detaching_during_start_question_cleans_up_the_prompt_helper(self):
        self.toggle_prompt()
        self.assertTrue(self.tmux('show-messages', '-J'), 'filename helper is not running')
        self.tmux('detach-client', '-t', self.client)
        self.wait_for(lambda: not self.tmux('list-clients', '-F', '#{client_name}'),
                      'interactive client detachment')
        self.wait_for(lambda: not self.tmux('show-messages', '-J'),
                      'filename helper exit after detachment')
        self.assertNotIn(b'@tmux-logging-input-', self.tmux('show-options', '-g'))
        self.assertFalse(self.pipe_active())
        self.assertFalse(self.default.exists())


if __name__ == '__main__':
    unittest.main()
