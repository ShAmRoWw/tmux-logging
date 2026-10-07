#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source "$CURRENT_DIR/variables.sh" || exit 1
source "$CURRENT_DIR/shared.sh"
source "$CURRENT_DIR/logging_state.sh"
source "$CURRENT_DIR/logging_prompt.sh"

main() {
	local file stopped owner title directory format started= ended= client= interactive=0
	if [ "${1:-}" = --prompt ]; then
		interactive=1
		shift
		client=${2:-}
	fi
	logging_target "${1:-}" || return 1
	logging_read_state || return 1
	if logging_is_owned; then
		owner=$LOGGING_OWNER
		logging_recording_file "$owner" file || return 1
		started=$(logging_tmux show-option -gqv "@tmux-logging-start-$owner") || return 1
		# Capture the end before draining or asking a question. A clock failure
		# must never prevent the user from stopping an active recording.
		if [ -n "$started" ]; then ended=$(logging_timestamp) || ended=; fi
		if [ -n "$started" ]; then
			stopped=$(logging_stop "$owner" "$ended") || return 1
		else
			stopped=$(logging_stop "$owner") || return 1
		fi
		if [ "$stopped" != stopped ]; then
			printf 'tmux-logging: recording changed while stopping\n' >&2
			return 1
		fi
		if [ -n "$started" ]; then
			# Only the successful stopper owns this result. A concurrent stop
			# which returns "changed" must not remove its predecessor's state.
			trap "logging_cleanup_result '$owner'" EXIT
			trap 'exit 1' HUP INT TERM
		fi
		if [ "$interactive" = 1 ] || [ -n "$started" ]; then
			logging_finish_name "$owner" "$file" "$client" "$started" "$ended"
		else
			display_message 'Ended logging'
		fi
	elif [ "$LOGGING_ACTIVE" = 1 ]; then
		printf 'tmux-logging: pane output pipe is already in use\n' >&2
		display_message 'Cannot start logging: pane output pipe is already in use'
		return 1
	else
		format=$logging_full_filename
		if [ "$interactive" = 1 ]; then format=$logging_title_full_filename; fi
		if ! expand_tmux_format_path "$format" file "$LOGGING_PANE"; then
			display_message 'Could not start logging'
			return 1
		fi
		if [ "$interactive" = 1 ]; then
			title=${file##*/}
			title=${title%.log}
			logging_choose_title "$client" 'Log filename (Enter = default)' "$title" title || return 0
			started=$(logging_timestamp) || { display_message 'Could not determine logging start time'; return 1; }
			directory=${file%/*}
			if [ "$directory" = "$file" ]; then directory=.; fi
			file="$directory/${title}__${started}.log"
			# Never merge two recordings that happen to get the same title and
			# second. noclobber also prevents a race with another starting pane.
			# Check nonregular entries too: Bash noclobber can open a FIFO and
			# wait indefinitely for a reader instead of reporting a collision.
			if [ -e "$file" ] || [ -L "$file" ]; then
				display_message 'Cannot start logging: filename already exists'
				return 1
			fi
			if ! (set -o noclobber; : > "$file") 2>/dev/null; then
				if [ -e "$file" ] || [ -L "$file" ]; then
					display_message 'Cannot start logging: filename already exists'
				else
					display_message 'Could not create log file'
				fi
				return 1
			fi
		fi
		if ! TMUX_LOGGING_START_TIME="$started" "$CURRENT_DIR/start_logging.sh" "$file" "$LOGGING_PANE"; then
			display_message 'Could not start logging'
			return 1
		fi
		display_message 'Started logging'
	fi
}
main "$@"
