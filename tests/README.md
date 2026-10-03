Run the regression tests from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

The tests use the Python standard library and isolated child processes. They
cover live output before EOF, SIGTERM/SIGHUP with an open input pipe, queued
input, repeated signals, UTF-8 split across reads, and regular-file input.
OSC tests cover hyperlink labels, adjacent sequences, BEL/ST terminators, and
visible text and screen updates between sequences.
Chunk-boundary tests compare every two-part split, one-character/byte chunks,
and repeatable mixed chunks. They also cover the actual 4096-byte read boundary,
incomplete control sequences on EOF/signals, and live output after unsupported
CSI sequences.
Alternate-screen tests cover saved main-screen output, empty main screens,
repeated transitions, and stopping via EOF/SIGTERM/SIGHUP inside an application.

Resize tests cover startup snapshots, cursor/mode restoration, wrapped-line
reflow, history restoration without duplicates, and saved-main dimensions.
Protocol tests check response framing, capture/control escaping, split UTF-8
byte accounting, and exclusion of output beyond the pipe's final byte count.
Integration tests exercise the shell entry point and synchronization with an
isolated tmux server, including resize/split/zoom and several resize commands
in one batch. These tests are skipped when their required tmux version is not
available; the synchronized transport tests require tmux 3.7+.

Path regressions exercise forced ansifilter and both sed branches through an
isolated tmux server. They check exact filenames, append behavior, and absence
of shell/tmux command execution for quotes, whitespace and format-like input.
The ansifilter executable is a test stand-in; the macOS branch is selected on
Linux and checked with GNU sed, not a native macOS run.

Sed fallback regressions cover CR/CRLF, complete CSI commands, OSC title and
hyperlink sequences with BEL/ST, visible Unicode, ordinary brackets and
whitespace, and split writes through a real pane pipe. Both branches are forced
independently; native BSD sed/macOS coverage still requires that platform.
