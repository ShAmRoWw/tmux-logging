"""The Linux and macOS sed branches remove controls without eating text."""

import shlex
import subprocess
import unittest

import test_logging_paths as paths
import test_tmux_logging_sync as integration


ESC = b'\x1b'
CSI = ESC + b'['
BEL = b'\x07'
ST = ESC + b'\\'


@unittest.skipUnless(integration.supported_tmux() and paths.BASH and paths.SED and paths.CAT,
                     'requires tmux 3.7+, bash, sed and cat')
class SedFallbackTests(unittest.TestCase):
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
    select_fallback = paths.LoggingPathTests.select_fallback
    run_entry = paths.LoggingPathTests.run_entry

    def setUp(self):
        paths.LoggingPathTests.setUp(self)
        # NUL separators preserve literal CR/ESC/BEL and whitespace arguments.
        self.write_executable('sed', '#!/bin/sh\n'
                              'printf "%s\\0" "$@" > "$LOGGING_TEST_BRANCH"\n'
                              f'exec {shlex.quote(paths.SED)} "$@"\n')
        self.run_number = 0

    def record(self, branch, pieces, expected):
        self.select_fallback(branch)
        self.emit(CSI + b'2J' + CSI + b'H', '')
        self.run_number += 1
        target = self.root / f'{branch}-{self.run_number}.log'
        self.branch.unlink(missing_ok=True)
        self.run_entry(str(target))
        self.wait_for(lambda: self.branch.exists() and self.branch.stat().st_size,
                      'sed invocation')
        arguments = self.branch.read_bytes().split(b'\0')[:-1]
        self.assertEqual(arguments[0], b'-r' if branch == 'sed' else b'-E')
        try:
            for piece in pieces:
                self.emit(piece)
            # The title is ordered after all bytes written to the raw PTY, so
            # closing the pipe cannot race the pane application's FIFO reads.
            marker = f'sed-complete-{self.run_number}'
            self.emit(ESC + b']2;' + marker.encode() + BEL)
            self.wait_for(lambda: self.tmux('display-message', '-p', '-t', self.pane,
                          '#{pane_title}').decode().strip() == marker,
                          'raw PTY output completion')
        finally:
            self.tmux('pipe-pane', '-t', self.pane, check=False)
        self.wait_for(lambda: target.exists() and target.read_bytes() == expected,
                      f'{branch} filtered output {expected!r}')
        self.assertEqual(target.read_bytes(), expected)
        return arguments

    def assert_both_branches(self, data, expected):
        pieces = [data] if isinstance(data, bytes) else data
        for branch in ('sed', 'sed-osx'):
            with self.subTest(branch=branch):
                self.record(branch, pieces, expected)

    def test_carriage_returns_are_removed_at_start_middle_and_line_end(self):
        self.assert_both_branches(
            b'\rfirst\r\nprogress 10%\rprogress 90%\r\nlast\r\r\n',
            b'first\nprogress 10%progress 90%\nlast\n')

    def test_csi_colors_defaults_many_parameters_and_cursor_controls_are_removed(self):
        sequences = (
            CSI + b'm', CSI + b'K', CSI + b'0m', CSI + b'1;32;40m',
            CSI + b'38;2;255;128;64m', CSI + b'38:2::255:128:64m',
            CSI + b'1K', CSI + b'2K', CSI + b'2J', CSI + b'5D',
            CSI + b'2;3H', CSI + b'?25l', CSI + b'?25h', CSI + b'5 q',
        )
        data = b''.join(sequence + str(index).encode() + b'|'
                        for index, sequence in enumerate(sequences)) + b'\n'
        expected = b''.join(str(index).encode() + b'|'
                            for index in range(len(sequences))) + b'\n'
        self.assert_both_branches(data, expected)

    def test_osc_titles_use_first_bel_or_st_and_keep_adjacent_text(self):
        data = (b'before' + ESC + b']0;secret [31m title' + BEL + b'between'
                + ESC + b']2;second secret / title' + ST + b'after\n'
                + ESC + b']2;one' + BEL + ESC + b']2;two' + ST + b'visible\n')
        self.assert_both_branches(data, b'beforebetweenafter\nvisible\n')

    def test_nonterminating_and_repeated_escapes_stay_inside_osc_payload(self):
        source = (b'before' + ESC + b']0;secret' + ESC + b'xhidden' + BEL
                  + b'middle' + ESC + b']0;secret' + ESC + ESC + b'\\'
                  + b'after' + ESC + b']0;secret' + ESC + ESC + BEL + b'end\n')
        self.assert_both_branches(source, b'beforemiddleafterend\n')

    def test_osc_hyperlinks_keep_unicode_labels_for_both_terminators(self):
        pieces = []
        labels = []
        for opening in (BEL, ST):
            for closing in (BEL, ST):
                label = 'ссылка 雪 e\u0301'.encode()
                pieces.append(ESC + b']8;id=example;https://example.test/path?q=1'
                              + opening + label + ESC + b']8;;' + closing + b'\n')
                labels.append(label + b'\n')
        self.assert_both_branches(b''.join(pieces), b''.join(labels))

    def test_plain_lookalike_sequences_punctuation_and_whitespace_are_preserved(self):
        data = (b'[31m [0K ]0;literal title m K | @ ~ ; : \\x1b[31m\n'
                + 'до / после 雪\t  '.encode() + b'\n\t  \n\n')
        self.assert_both_branches(data, data)

    def test_controls_split_between_writes_on_one_line_are_removed(self):
        pieces = [b'before\x1b', b'[38;2;', b'255;128;0m', b'colored\x1b]',
                  b'8;;https://example.test/', b'\x1b', b'\\', 'ссылка'.encode(),
                  b'\x1b]', b'8;;\x07', b'after\r', b'\n']
        self.assert_both_branches(pieces, 'beforecoloredссылкаafter\n'.encode())

    def test_captured_filter_arguments_work_directly_under_c_locale(self):
        # Exercise the exact installed expression independently of tmux's own
        # display parser, which never changes bytes delivered to pipe-pane.
        source = (b'\r[31m|' + CSI + b'31mred' + CSI + b'0m|'
                  + ESC + b']0;private' + ST + '雪'.encode() + b'\t \r\n')
        expected = b'[31m|red|' + '雪'.encode() + b'\t \n'
        outputs = []
        for branch in ('sed', 'sed-osx'):
            with self.subTest(branch=branch):
                arguments = self.record(branch, [b'probe\n'], b'probe\n')
                result = subprocess.run([paths.SED.encode(), *arguments],
                                        input=source, capture_output=True,
                                        env=dict(self.env, LC_ALL='C'),
                                        timeout=integration.TIMEOUT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == '__main__':
    unittest.main()
