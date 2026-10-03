# Terminal text model

The Python logger stores display cells rather than indexing text by Unicode
code point. A wide glyph occupies two columns; combining marks stay with their
base glyph. Cursor movement, overwrite, insert/delete, captured startup state,
and resize use cell columns. Emission markers follow cells through edits and
reflow, preserving new revisions without repeating unchanged recorded text.

The model covers ordinary CJK and combining characters, emoji variation
selectors, skin modifiers, regional-indicator pairs and ZWJ sequences. It uses
the host C library's `wcwidth`, tmux's default emoji width rules and its 32-byte
cell limit. A Unicode-data fallback is used where `wcwidth` is unavailable;
no additional Python package is required.

Supported editing includes ICH, DCH, ECH, IL and DL, inclusive line/display
erase, NEL, scrolling regions, origin/insert/autowrap modes and cursor movement
with a pending wrap. The model follows tmux's cell movement and reflow behavior,
including retained blank cells after deletion. LF preserves the column in the
synchronized tmux backend; the standalone filter keeps its historical
LF-as-newline default. CR and NEL explicitly move to column zero.

DCS, SOS, PM and APC control strings are consumed without adding their payload
to the journal. Terminators and parser state survive input boundaries, and an
unfinished string is discarded at shutdown. Payload size does not increase the
parser's memory use. OSC retains its existing BEL/ST handling.

The [full-clear policy](synchronization.md) remains unchanged: full main-screen
clears preserve pending visible text; partial edits keep only their final state;
alternate-screen application text is excluded.

## Input limits

CSI parsing follows tmux's limits: at most 63 parameter bytes, 23 parameter
fields and 3 private/intermediate bytes; numeric values range from 0 to
2147483647. A command exceeding these limits or using malformed parameters is
ignored as a whole. Its remaining bytes stay hidden until the final byte,
cancellation or a new escape sequence, so a long tail cannot become another
command. Unfinished CSI state remains bounded across input reads and is
discarded at shutdown. Valid scrolling and editing counts are limited to the
applicable screen region.

Screen dimensions must be decimal integers from 1 to 10000, with no more than
1000000 visible cells in total. The CLI rejects invalid dimensions with a short
error and a nonzero status before allocating the screen or reading input. The
tmux backend checks startup dimensions before installing a pane pipe and checks
snapshots and subsequent layouts as well. An unsupported live size stops the
recording with an error after processing the preceding output; the existing
screen is preserved for the final flush.

## Limits

- Width rules assume tmux's defaults on the same host. Custom `codepoint-widths`,
  `variation-selector-always-wide=off`, a different server locale, or a tmux
  build using different Unicode tables may give different cell widths.
- The listed combinations are supported; this is not a complete Unicode
  grapheme segmentation or font shaping implementation.
- Sixel and other image protocols are not rendered. Their payload stays out of
  the text journal, but their graphical layout and cursor effects are not
  reconstructed. tmux passthrough DCS contents are likewise excluded.
- Styling is omitted and terminal commands outside the supported text model
  may still require a later pane capture to restore synchronization. The log
  remains a readable text journal, not a byte-for-byte terminal archive.

Regression tests compare text, cursor position, editing and reflow against
isolated tmux servers. The current suite was exercised on Linux with tmux 3.7c.
