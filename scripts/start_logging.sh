#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$CURRENT_DIR/logging_state.sh"

# path to log file - global variable
FILE="$1"
case "$FILE" in
	/*) ;;
	*) FILE="$PWD/$FILE" ;;
esac

python3_installed() {
	type python3 >/dev/null 2>&1 || return 1
}

ansifilter_installed() {
	type ansifilter >/dev/null 2>&1 || return 1
}

system_osx() {
	[ $(uname) == "Darwin" ]
}

# The Python worker captures the pane before consuming ordered output and
# layout notifications. It installs its pipe only after the capture barrier.
pipe_pane_python3() {
	python3 -B "$CURRENT_DIR/tmux_logging.py" start "$FILE" "$LOGGING_PANE"
}

quote_pipe_pane_argument() {
	local value="$1"
	# pipe-pane expands strftime and tmux formats before passing the command
	# to /bin/sh. Quote for POSIX sh (not Bash's printf %q), then split after
	# each hash so even #(), #{...}, and #[...] remain literal characters.
	value=${value//\'/\'\\\'\'}
	value=${value//#/#\'\'}
	value=${value//%/%%}
	printf "'%s'" "$value"
}

pipe_pane_filtered() (
	local command="exec"
	local argument directory token installed owner status success=0
	directory=$(mktemp -d "${TMPDIR:-/tmp}/tmux-logging-start.XXXXXXXX") || return 1
	token=${directory##*/}
	token=${token//[^a-zA-Z0-9]/}
	trap '[ "$success" = 1 ] || { [ -z "$owner" ] || logging_cleanup "$owner"; }; rm -rf -- "$directory"' EXIT
	trap 'exit 1' HUP INT TERM
	for argument in bash "$CURRENT_DIR/logging_pipe.sh" "$LOGGING_SOCKET" \
		"$LOGGING_PANE" "$token" "$directory" "$FILE" "${TMUX_LOGGING_START_TIME:-}" "$@"; do
		command="$command $(quote_pipe_pane_argument "$argument")"
	done
	command="$command 2> $(quote_pipe_pane_argument "$directory/error")"
	installed=$(logging_tmux if-shell -F -t "$LOGGING_PANE" '#{pane_pipe}' \
		'display-message -p busy' \
		"pipe-pane -t '$LOGGING_PANE' $(quote_tmux_argument "$command") ; display-message -p installed") || return 1
	if [ "$installed" != installed ]; then
		printf 'tmux-logging: pane output pipe is already in use\n' >&2
		return 1
	fi
	local attempt
	for ((attempt=0; attempt<250; attempt++)); do
		if [ -s "$directory/owner" ]; then IFS= read -r owner < "$directory/owner"; fi
		if [ -s "$directory/status" ]; then
			IFS= read -r status < "$directory/status"
			# Readiness can arrive between the owner-file check and this read.
			if [ -s "$directory/owner" ]; then IFS= read -r owner < "$directory/owner"; fi
			if [ "$status" = "READY $owner" ] && logging_read_state &&
				[ "$LOGGING_OWNER" = "$owner" ] && logging_is_owned; then
				success=1
				return 0
			fi
			break
		fi
		sleep 0.02
	done
	if [ -s "$directory/error" ]; then cat "$directory/error" >&2; fi
	printf 'tmux-logging: filter did not start\n' >&2
	return 1
)

pipe_pane_ansifilter() {
	pipe_pane_filtered ansifilter
}

pipe_pane_sed_osx() {
	pipe_pane_sed_filter -E
}

pipe_pane_sed() {
	pipe_pane_sed_filter -r
}

pipe_pane_sed_filter() {
	# Generate real control bytes in Bash: BSD sed does not interpret \x1B.
	# Keep both platforms on the same byte-oriented expressions. OSC must be
	# removed first so its payload cannot be mistaken for visible CSI/text.
	local esc=$'\033' bel=$'\007' cr=$'\r'
	local osc="${esc}\\]([^${bel}${esc}]|${esc}+[^${bel}${esc}\\\\])*(${esc}*${bel}|${esc}+\\\\)"
	local csi="${esc}\\[[0-?]*[ -/]*[@-~]"
	pipe_pane_filtered env LC_ALL=C sed "$1" \
		-e "s/$osc//g" -e "s/$csi//g" -e "s/$cr//g"
}

start_pipe_pane() {
	if python3_installed; then
		pipe_pane_python3
	elif ansifilter_installed; then
		pipe_pane_ansifilter
	elif system_osx; then
		# BSD sed uses '-E'; both branches share the same expressions.
		pipe_pane_sed_osx
	else
		pipe_pane_sed
	fi
}

main() {
	logging_target "${2:-}" || return 1
	logging_read_state || return 1
	if [ "$LOGGING_ACTIVE" = 1 ]; then
		printf 'tmux-logging: pane output pipe is already in use\n' >&2
		return 1
	fi
	start_pipe_pane
}
main "$@"
