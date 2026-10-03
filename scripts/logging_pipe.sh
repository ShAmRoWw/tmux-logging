#!/usr/bin/env bash
# Keep the pipe process alive while its filter runs, and clean up on failure
# even if the pane produces no more output for tmux to notice the closed fd.
CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$CURRENT_DIR/logging_state.sh"

LOGGING_SOCKET=$1
LOGGING_PANE=$2
token=$3
directory=$4
file=$5
shift 5
LOGGING_OPTION="@tmux-logging-$LOGGING_PANE"
owner="$token:$$"
file_option="@tmux-logging-file-$owner"
child=

cleanup() {
	local status=$?
	trap - EXIT HUP INT TERM
	if [ -n "$child" ]; then
		kill "$child" 2>/dev/null
		wait "$child" 2>/dev/null
	fi
	# The filename disappearing is the stop UI's flush/close barrier.
	exec 3>&-
	logging_tmux set-option -guq "$file_option" >/dev/null 2>&1
	logging_cleanup "$owner"
	if [ -d "$directory" ]; then
		printf 'ERROR\n' > "$directory/status"
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# Register our own PID, never a PID observed after an installation hook may
# have replaced the pipe. The caller can clean up this owner on startup timeout.
printf '%s\n' "$owner" > "$directory/owner" || exit 1
registered=$(logging_tmux if-shell -F -t "$LOGGING_PANE" \
	"#{&&:#{pane_pipe},#{==:#{pane_pipe_pid},$$}}" \
	"set-option -gq '$LOGGING_OPTION' '$owner' ; display-message -p registered") || exit 1
[ "$registered" = registered ] || exit 1
logging_read_state && logging_is_owned && [ "$LOGGING_OWNER" = "$owner" ] || exit 1
# Redirection errors must be reported before the caller announces success.
exec 3>> "$file" || exit 1
"$@" <&0 >&3 2>/dev/null &
child=$!
kill -0 "$child" 2>/dev/null || exit 1
# Native quoting also preserves a trailing semicolon, which tmux's argv
# command parser otherwise treats as a command separator.
logging_tmux if-shell -F 1 "set-option -gq '$file_option' $(quote_tmux_argument "$file")" || exit 1
printf 'READY %s\n' "$owner" > "$directory/status" || exit 1
wait "$child"
status=$?
child=
exit "$status"
