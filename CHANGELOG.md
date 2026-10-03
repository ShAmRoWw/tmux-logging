# Changelog

### master

- Ask for a filename when the logging key starts a recording and offer to rename
  it after stopping and flushing. Enter keeps the suggested name; Escape cancels
  startup or keeps the stopped log. Preserve literal input and existing files.
- Point TPM and manual installation instructions to ShAmRoWw/tmux-logging.
  Document the shared-directory collision with upstream and a migration that
  preserves the old checkout, handles custom TPM paths and verifies the remote.
- Repair sed fallback filtering on both OS branches: restore CR removal, use
  explicit ESC/BEL bytes and shared CSI/OSC expressions, preserve hyperlink
  labels and ordinary whitespace, and document line-oriented filtering limits.
- Track recordings by permanent pane ID and verified pipe ownership. Preserve
  toggle behavior across renames/reindexing, report startup errors accurately,
  release idle pipes after reader failure, and protect replacement/foreign
  pipes from late cleanup. Supervise ansifilter/sed startup and exit as well;
  all recording backends now require tmux 3.7+ for pipe process identification.
- Bound CSI storage and parameter parsing to tmux's limits. Ignore oversized or
  malformed commands atomically and resume after their terminator/cancellation.
  Validate screen dimensions before allocation; reject unsupported startup or
  live sizes explicitly and release the recording relay on worker failure.
- Track Unicode display cells, combining marks and common emoji sequences
  through editing, startup captures and reflow. Implement ICH/DCH/ECH/IL/DL,
  inclusive erase, NEL and cursor/margin/pending-wrap behavior matching tmux.
- Exclude DCS/SOS/PM/APC payloads incrementally, including split terminators,
  cancellation and unfinished strings at shutdown, without buffering payloads.
- Preserve pending main-screen text before a full clear while keeping partial
  edits quiet and alternate-screen content excluded. Treat ED3 as a history-only
  clear, track tmux's scroll-on-clear setting, and retain text erased during
  resize reconciliation without re-emitting matched history.
- Preserve literal whitespace and special characters in screen/history capture
  paths. Trim trailing empty rows without loading the whole history into a shell
  variable, and publish captures atomically only after every stage succeeds.
  Failed captures leave the previous file intact and do not report success.
- Quote fallback command arguments and log paths for both POSIX shell parsing
  and tmux format/time expansion. Keep quotes, whitespace, hashes, percent signs
  and command-substitution syntax literal in filenames; protect the Python
  relay's installation path from the same second expansion.
- Initialize the Python logger from the actual pane contents, cursor, margins,
  terminal modes and pending escape sequence. Ordered control output and a
  pipe byte-count boundary keep startup and shutdown synchronized.
- Follow resize/split/zoom, reflow wrapped lines, retain saved-main geometry
  across alternate-screen resizes, and avoid re-emitting unchanged history.
  Verify layout replay against an authoritative pane capture after resizing.
- Require Python 3.8+ and tmux 3.7+ for synchronized screen logging; document
  remaining capture/control-interface limitations.
- Flush the saved main screen when logging stops inside an alternate-screen
  application, preserving pending shell output and excluding application text.
- Preserve escape-sequence state across reads, including split OSC terminators,
  CSI commands, and charset designations. Discard incomplete control sequences
  on EOF or shutdown instead of logging their payload.
- Resume logging after complete unsupported CSI sequences, including cursor
  shape commands and colour parameters separated by colons.
- Preserve hyperlink labels and visible text between OSC sequences by ending
  each sequence at its first BEL or ST terminator.
- Process available input without waiting for a full read buffer, preserving
  UTF-8 characters split across reads.
- On SIGTERM/SIGHUP, finish the current input and queued bytes before flushing
  the remaining screen once, without waiting for the producer to close.

### v2.1.0, 2015-03-18
- `capture-pane` gets a `-J` flag. It joins wrapped lines and preserves trailing
  spaces at each line's end.
- when capturing whole history, do not guess its size, but get it from
  `#{history_limit}` format flag

### v2.0.0, 2015-03-14
- update readme
- extract default keys and key options to `variables.sh` file
- require tmux version 1.9 or greater to use the plugin
- change all internal variable names (BREAKING CHANGE!)
- change internal script names
- lot of internal refactoring

### v1.0.0, 2014-11-09
- improve `capture-pane`. It is now piped directly to the target file.

### v0.0.2, 2014-08-26
- add other plugins list to the README
- update readme to reflect github organization change
- add a 'clear pane history' key binding

### v0.0.1, 2014-06-02

- first working release
