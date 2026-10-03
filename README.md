# Tmux Logging

Changes from upstream                                                                                                                                     
                                                                                                                                                            
Replaced the sed-based ANSI escape code stripper with a VT100 screen emulator filter (scripts/logging_filter.py). The original approach only removed color
codes, leaving cursor movements, backspace sequences, and other control characters in the log — producing unreadable output.

The filter maintains an in-memory screen buffer matching the pane dimensions.
It records lines when they scroll off the top, before a full main-screen clear,
and when recording stops. Ordinary edits retain their final visible state:

- Backspace / autosuggestions — only the final visible text is logged
- Tab-completion menus — omitted when removed by partial screen redraws
- Full-screen apps (nano, vim, less) — use the alternate screen buffer, content is discarded
- Progress bars (\r) — overwrite in-place, only the last state is logged

The screen logger requires Python 3.8+ and tmux 3.7+. It starts from the pane's
current contents and cursor, follows resize/split/zoom, and preserves already
logged text when tmux brings history back onto the screen. Existing visible
main-screen text is included; older scrollback is used for synchronization only.
See [pane synchronization](docs/synchronization.md) for behavior and limitations.
If Python is unavailable, the ansifilter/sed fallback is used. All recording
backends require tmux 3.7+ to verify ownership of the pane's output pipe.

The screen model accounts for wide and combining Unicode characters and common
emoji sequences. It supports character/line insertion, deletion and erasure,
and excludes DCS/SOS/PM/APC payloads from the log. See the
[terminal text model](docs/terminal-model.md) for supported behavior and limits.

A full main-screen clear preserves pending visible text, so `clear` no longer
discards output that has not scrolled yet. This includes `CSI 2 J`, erasing from
the top-left corner to the end, and erasing from the bottom-right corner to the
start. Repeated clears of an empty screen add nothing. If an application clears
and redraws the same text, that text may be recorded again; anything visible at
the full clear, including a completion menu, belongs to that snapshot.
Partial erases and overwrites still discard intermediate states deliberately.
This readable journal is not a lossless archive of the terminal byte stream.

---

Features:

1. Readable logging of the current pane<br/>
   Records main-screen output according to the scroll, full-clear and shutdown
   rules above. Convenient for keeping track of your work.
2. Current pane "Screen Capture"<br/>
   All the text visible in the current pane is saved to a file. Like a
   screenshot, but textual.
3. Save a complete history of current pane<br/>
   Everything that has been typed and all the output since the creation of the
   current pane can be saved to a file.
4. Clear pane history with `prefix + alt + c`

Tested and working on Linux, OSX and Cygwin.

### 1. Logging

Toggle (start/stop) logging in the current pane.

At startup, edit the suggested filename or press Enter to use the default.
At shutdown, recording stops and the remaining output is written before the
filename prompt appears. Press Enter to keep the name, or enter a new one.
Escape cancels startup; at shutdown it keeps the saved file's current name.

Enter a filename, without a directory; files stay in the configured logging
directory. Names are used literally, with no automatic extension. A blank answer
also accepts the suggested name. Renaming never replaces an existing file.
Starting with an existing filename appends to that file. Safe renaming requires
hard-link support in the logging filesystem; a failure keeps the original name.
Direct calls to `scripts/toggle_logging.sh` remain noninteractive.

Recording follows the pane's permanent ID, so renaming a session or changing
pane/window indexes does not break the toggle. Startup is confirmed before a
success message is displayed. An existing output pipe owned by another program
is preserved. See [recording lifecycle](docs/synchronization.md#recording-lifecycle)
for status and failure handling.

* Key binding: `prefix + shift + p`
* File name format: `tmux-#{session_name}-#{window_index}-#{pane_index}-%Y%m%dT%H%M%S.log`
* File path: `$HOME` (user home dir)
  * Example file: `~/tmux-my-session-0-1-20140527T165614.log`

### 2. "Screen Capture"

Save visible text, in the current pane. Equivalent of a "textual screenshot".

* Key binding: `prefix + alt + p`
* File name format: `tmux-screen-capture-#{session_name}-#{window_index}-#{pane_index}-%Y%m%dT%H%M%S.log`
* File path: `$HOME` (user home dir)
  * Example file: `tmux-screen-capture-my-session-0-1-20140527T165614.log`

### 3. Save complete history

Save complete pane history to a file. Convenient if you retroactively remember
you need to log/save all the work.

* Key binding: `prefix + alt + shift + p`
* File name format: `tmux-history-#{session_name}-#{window_index}-#{pane_index}-%Y%m%dT%H%M%S.log`
* File path: `$HOME` (user home dir)
  * Example file: `tmux-history-my-session-0-1-20140527T165614.log`

**NOTE**: this functionality depends on the value of `history-limit` - the number
of lines Tmux keeps in the scrollback buffer. Only what Tmux kept will also be saved,
to a file.

Use `set -g history-limit 50000` in .tmux.conf, with modern computers
it is ok to set this option to a high number.

Both screen capture and history saving support spaces, quotes and newlines in
paths. They remove trailing empty rows while preserving spaces and internal
blank lines. A successful capture replaces the destination atomically; a failed
capture leaves the previous file intact and does not report success. Each saved
snapshot is a new file with owner-only permissions (`0600`). If the destination
is a symbolic link to a file, the link is replaced and its target is left intact.

### 4. Clear pane history

Key binding: `prefix + alt + c`

This is just a convenience key binding.

### Installation with [Tmux Plugin Manager](https://github.com/tmux-plugins/tpm) (recommended)

If upstream `tmux-plugins/tmux-logging` is already installed, follow the
[migration instructions](docs/migration.md) first. Both repositories use the
same TPM directory; changing the plugin declaration alone does not replace it.

For a new installation, add this fork to your tmux configuration before the
existing TPM initialization line:

```tmux
set -g @plugin 'ShAmRoWw/tmux-logging'
```

Reload that configuration inside the target tmux server, for example:

```sh
tmux source-file ~/.tmux.conf
```

Use your actual configuration path if it is under `$XDG_CONFIG_HOME/tmux` or
`~/.config/tmux`. Hit `prefix + I` (capital I) to fetch the plugin and source it.

You should now have all `tmux-logging` key bindings defined.

### Manual Installation

Use this method when TPM is not managing the plugin. Clone the fork into a new
directory:

```sh
git clone https://github.com/ShAmRoWw/tmux-logging.git ~/.tmux/manual-plugins/tmux-logging
```

Add this line to your tmux configuration:

```tmux
run-shell '"$HOME/.tmux/manual-plugins/tmux-logging/logging.tmux"'
```

Reload that configuration:

```sh
tmux source-file ~/.tmux.conf
```

When replacing a manual upstream installation, stop its recordings, retain the
old checkout, and replace its old `run-shell` line with the new one. Keep only
one installation enabled. Existing custom options can stay in your config.

### Fallback filters

When Python is unavailable, the plugin uses `ansifilter` if installed,
otherwise `sed`. To install `ansifilter` on macOS:
`$ brew install ansifilter`

[ansifilter](http://www.andre-simon.de/doku/ansifilter/en/ansifilter.php)
is a program specialized for removing (or working with) ANSI codes.

Both sed branches remove CR, complete CSI commands and OSC terminated by BEL or
ST within a line, while preserving link labels and ordinary whitespace. These
fallbacks filter the output stream without reconstructing the terminal screen.
See [fallback limits](docs/synchronization.md#fallback-filtering).

This feature improves the default `pipe-pane` logging mechanism by stripping
ANSI codes. This is how the plain `pipe-pane` log output looks like if you're
using terminal with coloring:

![garbled log output](/screenshots/garbled_log_output.png)

Garbled characters are called ANSI codes. They enable colors in terminal, but
are just making 'noise' in the textual log output.

A user will probably want to filter ANSI codes out of the log. Here's the same
log as above when this plugin is used:

![proper log output](/screenshots/proper_log_output.png)

### Configuration Docs

- [Changing default options](docs/configuration.md).

### Other plugins

You might also find these useful:

- [resurrect](https://github.com/tmux-plugins/tmux-resurrect) - restore tmux
  environment after system restart
- [pain control](https://github.com/tmux-plugins/tmux-pain-control) - useful standard
  bindings for controlling panes
- [sessionist](https://github.com/tmux-plugins/tmux-sessionist) - lightweight
  tmux utils for switching and creating sessions

### License

[MIT](LICENSE.md)
