# Pane synchronization

The Python screen logger requires Python 3.8 or newer and tmux 3.7 or newer.
The latter provides the wrapped-line flags needed by the initial capture and
the pipe process ID needed to verify recording ownership. Starting any logging
backend on an older tmux fails with an explicit error. The
ansifilter/sed fallback used when Python is absent remains a stream filter and
does not reconstruct a screen.

## Fallback filtering

When Python is unavailable, `ansifilter` is preferred if installed; otherwise
the plugin uses sed. Linux and macOS sed branches share the same expressions,
with literal ESC, BEL and CR bytes generated explicitly by Bash. `LC_ALL=C`
keeps matching byte-oriented without changing the bytes of visible UTF-8 text.

The sed fallback removes CR and complete 7-bit CSI commands, including extended
color parameters, cursor commands and erasure. OSC strings end at the first
BEL or ST (ESC followed by a backslash), so title/URL payloads are removed and hyperlink labels
remain. Ordinary text without ESC, including `[31m`, spaces and tabs, is kept.

This is a line-oriented filter: sed may buffer output, and incomplete control
sequences or sequences spanning LF are not reconstructed. Other control
families such as DCS/APC and 8-bit C1 forms are not covered. Removing CR does not
apply cursor movement or redraws; progress updates remain successive text in
the log. Screen reconstruction and alternate-screen exclusion require the
Python backend.

Both branches and split writes were tested with GNU sed on Linux through an
isolated tmux server. Native BSD sed/macOS was not exercised.

## Recording lifecycle

The toggle resolves the target's permanent `pane_id` once and uses that target
for filename expansion, startup and stopping. Session names and pane/window
indexes are used only in the filename template. Renaming or reindexing does not
change which recording the next toggle stops.

Each recording registers a unique token and its pipe process ID under the
global option `@tmux-logging-<pane_id>`. The toggle requires both an active pipe
and a matching process ID; this registration alone is not a status flag. Old
options named after session/window/pane indexes are ignored. File setup and
reader readiness must succeed before startup reports success. Errors propagate
to the caller, and the next toggle can retry after the problem is corrected.

Startup refuses an existing output pipe. Stop and cleanup check the token and
current pipe process ID in tmux's command queue, so a reader finishing late
cannot stop or unregister a replacement recording. The Python worker and relay
clean up each other's failures, including an idle pane whose pipe tmux would
otherwise retain until the next output byte. The ansifilter/sed wrapper watches
its filter and removes its own registration and pipe when the filter exits.
External `pipe-pane` stops also end the corresponding recording.

For timestamped recordings created by the key binding, the writer adds the
Moscow end time after closing the file. Shell `exit`, pane closure, and the last
session's exit finalize the filename without a dialog or a live tmux server.
An explicit toggle records its requested stop time before closing the pipe;
the writer returns the finished path before the optional title-editing prompt.
Existing destination names are preserved and a failed rename leaves the log
under its previous name. Explicit unmarked script filenames remain unchanged.
Client detachment alone does not stop recording while its pane remains alive.

Normal Python shutdown flushes pending main-screen text. A forcibly killed
worker cannot flush text held only in its memory; clearing its status does not
recover that text or guarantee a completed filename. Lifecycle checks were
exercised on Linux with tmux 3.7c.

## Screen synchronization

At startup the logger captures the visible screen, existing scrollback, cursor,
scrolling margins, autowrap, origin and insert modes, tab stops, and a pending
escape sequence. Visible main-screen text becomes part of the recording.
Older scrollback supplies context for later resizes but is not exported again.
When recording starts inside an alternate-screen application, its contents are
excluded and the saved main screen is used when recording stops.

Before a full main-screen clear, pending visible text is committed without
trailing empty rows. Partial erases and in-place edits retain only their final
state. Alternate-screen clears never commit application text. `CSI 3 J` clears
only scrollback, leaving the visible screen and cursor unchanged.

The initial snapshot includes `scroll-on-clear`. When enabled, full clears also
move the used main-screen rows into the logger's history model, matching tmux;
emission markers prevent a later resize from recording those rows again.

The capture and installation of the pane pipe occur in one tmux command queue.
A read-only control client receives ordered output and layout notifications;
the pipe reader supplies byte counts that delimit the recording. This avoids
starting from a snapshot while also replaying the bytes already represented in
that snapshot. Closing the pane pipe ends recording, drains the delivered
prefix, flushes the main screen and disconnects the control client. SIGTERM and
SIGHUP follow the same shutdown path.

Resize, split and zoom update the screen dimensions, cursor and wrapped lines.
Height growth can restore lines from history; emission markers prevent those
unchanged lines from appearing twice in the journal. Editing a restored line
creates a new revision because an append-only journal cannot change its earlier
entry. The saved main screen retains its own dimensions while an alternate
screen is active and is reflowed when the application returns to it.

tmux may coalesce several layout changes and report only the final dimensions.
The logger therefore captures the pane again after layout notifications and
checks the replay before committing its output. A mismatching replay is rebased
against the captured history and screen using logical-line correspondence.
When that replay contains a full main-screen clear, its committed text is kept
even if the later capture no longer contains it. If intermediate dimensions
were not reported, physical line breaks in that preserved text may reflect the
last reported dimensions.

There are remaining limitations in tmux's public capture/control interface:

- If recording starts inside an alternate screen that was already made narrower
  than its saved main screen, `capture-pane -a` clips saved rows to the current
  width. The plugin cannot retrieve the hidden suffixes or the original width.
  A main screen already known to the logger is preserved across such resizes.
- General DEC saved-cursor state and the historical scroll-restoration boundary
  are not exposed. Current cursor and alternate saved cursor are available;
  a subsequent layout capture supplies the actual post-resize state.
- Textual history has no stable row identifiers. When coalesced resizes and a
  large burst of repeated output also evict history before recapture, textual
  correspondence alone cannot guarantee exact historical attribution.
- The control connection uses `ignore-size` and does not set pane dimensions,
  but tmux still lists it as an attached client. Settings and hooks based on
  attached-client counts can observe this connection.

Unicode width defaults, supported editing operations and remaining graphics or
width-configuration limits are described in the [terminal text model](terminal-model.md).
A readable screen journal is not a lossless archive of every byte written by an
application.

Regression tests use the standard library and isolated tmux servers. They cover
filled-pane startup, cursor and mode restoration, pending OSC, resize/reflow,
split/zoom, coalesced resize commands, alternate screens, history restoration,
byte boundaries, shutdown, and isolation from other panes. The current changes
have been exercised on Linux with tmux 3.7c; other platforms were not exercised.
