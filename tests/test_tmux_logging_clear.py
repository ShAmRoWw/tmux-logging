"""Preserve main-screen text at full clears without recording transient edits."""

import codecs
from collections import deque
import io
from pathlib import Path
import sys
import unittest

import test_tmux_logging_sync as synchronization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from logging_filter import Screen, StreamProcessor
from tmux_logging import Recording


@unittest.skipUnless(synchronization.supported_tmux(), 'requires tmux 3.7 or newer')
class TmuxClearTests(unittest.TestCase):
    # Reuse the isolated-server fixture without inheriting its test methods.
    setUp = synchronization.TmuxSynchronizationTests.setUp
    tmux = synchronization.TmuxSynchronizationTests.tmux
    wait_for = synchronization.TmuxSynchronizationTests.wait_for
    worker_command = synchronization.TmuxSynchronizationTests.worker_command
    create_pane = synchronization.TmuxSynchronizationTests.create_pane
    dimensions = synchronization.TmuxSynchronizationTests.dimensions
    capture = synchronization.TmuxSynchronizationTests.capture
    history_and_screen = synchronization.TmuxSynchronizationTests.history_and_screen
    visible = synchronization.TmuxSynchronizationTests.visible
    emit = synchronization.TmuxSynchronizationTests.emit
    start_recording = synchronization.TmuxSynchronizationTests.start_recording
    resize = synchronization.TmuxSynchronizationTests.resize
    stop_recording = synchronization.TmuxSynchronizationTests.stop_recording
    cleanup_server = synchronization.TmuxSynchronizationTests.cleanup_server

    def check_full_clear(self, scroll_on_clear, sequence):
        self.create_pane(30, 6)
        self.tmux('set-option', '-p', '-t', self.pane,
                  'scroll-on-clear', scroll_on_clear)
        self.emit(b'EXISTING', 'EXISTING')
        self.start_recording()
        self.emit(b'\r\nLIVE', 'EXISTING\nLIVE')
        self.emit(sequence + b'AFTER', 'AFTER')
        self.wait_for(lambda: self.log.read_text() == 'EXISTING\nLIVE\n',
                      'cleared text committed before recording stops')
        # Re-capturing history after a resize must retain its emission markers.
        self.resize(30, 8)
        self.assertEqual(self.stop_recording(), 'EXISTING\nLIVE\nAFTER\n')

    def test_ed2_with_scroll_on_clear_enabled(self):
        self.check_full_clear('on', b'\x1b[H\x1b[2J')

    def test_ed2_with_scroll_on_clear_disabled(self):
        self.check_full_clear('off', b'\x1b[H\x1b[2J')

    def test_home_ed0_with_scroll_on_clear_enabled(self):
        self.check_full_clear('on', b'\x1b[H\x1b[J')

    def test_home_ed0_with_scroll_on_clear_disabled(self):
        self.check_full_clear('off', b'\x1b[H\x1b[J')

    def test_ed3_clears_history_without_erasing_visible_text(self):
        self.create_pane(30, 4)
        lines = [f'row-{index}' for index in range(8)]
        visible = '\n'.join(lines[-4:])
        self.emit('\r\n'.join(lines).encode(), visible)
        self.start_recording()
        self.emit(b'\x1b[3J-EDIT', visible + '-EDIT')
        self.assertEqual(self.history_and_screen(), visible + '-EDIT')
        self.resize(30, 6)
        self.assertEqual(self.stop_recording(), visible + '-EDIT\n')

    def test_clear_ed2_ed3_survives_reflow_without_duplicate_text(self):
        self.create_pane(30, 6)
        self.emit(b'ABCDEFGHIJKLMNO', 'ABCDEFGHIJKLMNO')
        self.start_recording()
        self.emit(b'\x1b[H\x1b[2J\x1b[3JAFTER', 'AFTER')
        self.wait_for(lambda: self.log.read_text() == 'ABCDEFGHIJKLMNO\n',
                      'full clear committed before scrollback erasure')
        self.resize(10, 6)
        self.resize(30, 6)
        self.assertEqual(self.stop_recording(), 'ABCDEFGHIJKLMNO\nAFTER\n')

    def test_repeated_empty_clear_does_not_duplicate_previous_text(self):
        self.create_pane(30, 6)
        self.start_recording()
        self.emit(b'FIRST\x1b[H\x1b[2J\x1b[2J', '')
        self.wait_for(lambda: self.log.read_text() == 'FIRST\n', 'first clear')
        # Identical content drawn again is a fresh screen, not a duplicate.
        self.emit(b'FIRST\x1b[H\x1b[2JFINAL', 'FINAL')
        self.wait_for(lambda: self.log.read_text() == 'FIRST\nFIRST\n',
                      'new occurrence of identical screen text')
        self.assertEqual(self.stop_recording(), 'FIRST\nFIRST\nFINAL\n')

    def test_alternate_screen_clears_do_not_record_application_text(self):
        self.create_pane(30, 6)
        self.emit(b'MAIN', 'MAIN')
        self.start_recording()
        self.emit(b'\x1b[?1049hPRIVATE\x1b[H\x1b[2JPRIVATE-AGAIN',
                  'PRIVATE-AGAIN')
        self.emit(b'\x1b[H\x1b[J\x1b[?1049l', 'MAIN')
        self.emit(b'\x1b[H\x1b[2JAFTER', 'AFTER')
        self.wait_for(lambda: self.log.read_text() == 'MAIN\n',
                      'main screen committed after alternate screen exit')
        self.assertEqual(self.stop_recording(), 'MAIN\nAFTER\n')

    def test_partial_redraw_has_no_intermediate_revisions(self):
        self.create_pane(30, 6)
        self.emit(b'PROMPT\r\nCOMPLETION-MENU', 'PROMPT\nCOMPLETION-MENU')
        self.start_recording()
        self.emit(b'\x1b[2;1H\x1b[JRESULT', 'PROMPT\nRESULT')
        self.assertEqual(self.stop_recording(), 'PROMPT\nRESULT\n')


class ClearDuringRebaseTests(unittest.TestCase):
    def test_coalesced_resize_preserves_clear_when_margins_need_rebase(self):
        recording = Recording.__new__(Recording)
        recording.output = io.StringIO()
        recording.screen = Screen(20, 6, recording.output)
        recording.screen.load_snapshot(['EXISTING'], cursor=(8, 0),
                                       scroll_region=(1, 4))
        recording.stream = StreamProcessor(recording.screen)
        recording.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        # tmux may coalesce a 6 -> 3 -> 6 resize into the final geometry.
        # The omitted intermediate resize resets scrolling margins, making
        # the authoritative capture differ even after ED2 and ED3.
        recording.events = deque([
            ('resize', (20, 6)),
            ('output', b'\r\nBURST\x1b[H\x1b[2J\x1b[3JAFTER'),
        ])
        state = dict(lines=['AFTER', '', '', '', '', ''],
                     wrapped=[False] * 6, history=[], history_wrapped=[],
                     cols=20, rows=6, cursor=(5, 0), scroll_region=(0, 5),
                     autowrap=True, origin=False, insert=False, tabs=[8, 16],
                     scroll_on_clear=False)
        recording.rebase(2, state, b'')
        self.assertEqual(recording.output.getvalue(), 'EXISTING\nBURST\n')
        recording.finish()
        self.assertEqual(recording.output.getvalue(), 'EXISTING\nBURST\nAFTER\n')

    def test_rebase_preserves_clear_and_scroll_output_without_history_duplicates(self):
        recording = Recording.__new__(Recording)
        recording.output = io.StringIO()
        recording.screen = Screen(20, 6, recording.output)
        recording.screen.load_snapshot(['EXISTING'], cursor=(8, 0),
                                       scroll_region=(0, 4), scroll_on_clear=True)
        recording.stream = StreamProcessor(recording.screen)
        recording.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        history = ['EXISTING'] + [f'ROW-{index}' for index in range(1, 7)]
        # Both the old and reset margins scroll before ED2. Clearing into
        # history produces the same rows, although the margin mismatch forces
        # reconciliation. Rows emitted by scrolling and clearing stay marked.
        recording.events = deque([
            ('resize', (20, 6)),
            ('output', ('\r\n' + '\r\n'.join(history[1:])
                        + '\x1b[H\x1b[2JAFTER').encode()),
        ])
        state = dict(lines=['AFTER', '', '', '', '', ''], wrapped=[False] * 6,
                     history=history, history_wrapped=[False] * len(history),
                     cols=20, rows=6, cursor=(5, 0), scroll_region=(0, 5),
                     autowrap=True, origin=False, insert=False, tabs=[8, 16],
                     scroll_on_clear=True)
        recording.rebase(2, state, b'')
        expected = '\n'.join(history) + '\n'
        self.assertEqual(recording.output.getvalue(), expected)
        self.assertEqual([row.text for row in recording.screen._history], history)
        recording.finish()
        self.assertEqual(recording.output.getvalue(), expected + 'AFTER\n')


if __name__ == '__main__':
    unittest.main()
