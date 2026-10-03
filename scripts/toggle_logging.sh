#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source "$CURRENT_DIR/variables.sh" || exit 1
source "$CURRENT_DIR/shared.sh"
source "$CURRENT_DIR/logging_state.sh"
source "$CURRENT_DIR/logging_prompt.sh"

main() {
	local file stopped owner client= interactive=0
	if [ "${1:-}" = --prompt ]; then
		interactive=1
		shift
		client=${2:-}
	fi
	logging_target "${1:-}" || return 1
	logging_read_state || return 1
	if logging_is_owned; then
		owner=$LOGGING_OWNER
		if [ "$interactive" = 1 ]; then logging_recording_file "$owner" file || return 1; fi
		stopped=$(logging_stop "$owner") || return 1
		if [ "$stopped" != stopped ]; then
			printf 'tmux-logging: recording changed while stopping\n' >&2
			return 1
		fi
		if [ "$interactive" = 1 ]; then
			logging_finish_name "$owner" "$file" "$client"
		else
			display_message 'Ended logging'
		fi
	elif [ "$LOGGING_ACTIVE" = 1 ]; then
		printf 'tmux-logging: pane output pipe is already in use\n' >&2
		display_message 'Cannot start logging: pane output pipe is already in use'
		return 1
	else
		if ! expand_tmux_format_path "$logging_full_filename" file "$LOGGING_PANE"; then
			display_message 'Could not start logging'
			return 1
		fi
		if [ "$interactive" = 1 ]; then
			logging_choose_name "$client" 'Log filename (Enter = default)' "$file" file || return 0
		fi
		if ! "$CURRENT_DIR/start_logging.sh" "$file" "$LOGGING_PANE"; then
			display_message 'Could not start logging'
			return 1
		fi
		display_message 'Started logging'
	fi
}
main "$@"
