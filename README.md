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

At startup, edit the suggested recording title or press Enter to use the
session/window/pane name. A blank answer also accepts the suggestion. The plugin
adds the start time automatically after the answer, for example:

```text
tmux-my_session-0-1__2026-10-07_14-30-00.log
```

At shutdown, recording stops and the remaining output is written before the
file is renamed to include its end time:

```text
tmux-my_session-0-1__2026-10-07_14-30-00__2026-10-07_15-45-12.log
```

Both timestamps use `Europe/Moscow`, regardless of the host or tmux server's
timezone, without a timezone suffix in the filename. Each includes its date so
recordings across midnight are unambiguous. The end time is captured when
stopping, before waiting for the file to close or for an answer to the prompt.
The shutdown prompt edits only the title and preserves both timestamps. Enter
keeps the title; Escape cancels startup or keeps the completed filename after
shutdown. Exiting the pane's shell, closing the pane, or ending the last tmux
session also finishes the filename automatically after saving the remaining
text, without a prompt. This works even after the tmux server has exited.
Detaching from tmux leaves recording running while the pane remains alive.
A forcibly killed logger or a power failure may leave only the start time and
lose text still held in memory.

Titles are literal text without a directory. An optional trailing `.log` is
removed before adding timestamps and the final `.log` extension. Files stay in
the configured logging directory. `@logging-filename`, when set, supplies the
suggested title through its existing tmux format expansion. Neither starting
nor renaming replaces an existing file; an occupied timestamped name reports an
error. Safe renaming requires hard-link support in the logging filesystem; a
failure keeps the preceding name and reports the problem.

Direct calls to `scripts/toggle_logging.sh` remain noninteractive and keep their
previous start-name format. Stopping a recording created by the key binding still
adds the end time. Explicit `scripts/start_logging.sh FILE` paths remain literal
and append to existing files, without automatic timestamps or renaming.

Recording follows the pane's permanent ID, so renaming a session or changing
pane/window indexes does not break the toggle. Startup is confirmed before a
success message is displayed. An existing output pipe owned by another program
is preserved. See [recording lifecycle](docs/synchronization.md#recording-lifecycle)
for status and failure handling.

* Key binding: `prefix + shift + p`
* Default title: `tmux-#{session_name}-#{window_index}-#{pane_index}`
* Completed file name: `<title>__YYYY-MM-DD_HH-MM-SS__YYYY-MM-DD_HH-MM-SS.log`
* File path: `$HOME` (user home dir)
  * Example file: `~/tmux-my_session-0-1__2026-10-07_14-30-00__2026-10-07_15-45-12.log`

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
[migration instructions below](#switching-from-upstream) first. Both repositories
use the same TPM directory; changing the plugin declaration alone does not
replace it.

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

### Switching from upstream

1. Stop active recordings and locate the installed `tmux-logging` directory.
   It is normally `~/.tmux/plugins/tmux-logging`; use the actual installation
   path when TPM is configured elsewhere, including a custom
   `TMUX_PLUGIN_MANAGER_PATH` or an XDG configuration. Check its repository with
   `git -C /path/to/tmux-logging remote get-url origin`.
2. Move that directory to a backup location outside TPM's plugin directory.
   Keep the whole checkout, including local changes, until the fork is verified.
3. Replace the upstream entry with `set -g @plugin 'ShAmRoWw/tmux-logging'`.
   If using the older `@tpm_plugins` list, replace the entry there instead.
   Keep only one logging plugin enabled and retain your custom logging options.
4. Reload your actual tmux configuration and press `prefix + I` to install the
   fork. Check `remote get-url origin` in the new checkout: it should identify
   `ShAmRoWw/tmux-logging` on GitHub.

To return to the previous installation, stop recordings, move the fork aside,
restore the backup to its original path, and restore the old plugin entry before
reloading your configuration.

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

### Configuration

The default logging directory is `$HOME`. To change it, add this to your tmux
configuration, replacing the example with your desired directory:

```tmux
set -g @logging-path '/path/to/logs'
```

`@logging-filename` changes the suggested recording title; see
[Logging](#1-logging) for filename prompts and automatic timestamps.

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
