#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source "$CURRENT_DIR/scripts/variables.sh"
source "$CURRENT_DIR/scripts/shared.sh"


main() {
	local toggle="$CURRENT_DIR/scripts/toggle_logging.sh"
	# Quote for the shell and prevent run-shell's tmux format expansion from
	# interpreting hashes in the installation path.
	toggle=${toggle//\'/\'\\\'\'}
	toggle=${toggle//#/#\'\'}
	tmux bind-key "$logging_key" run-shell -b "'$toggle' --prompt '#{pane_id}' #{q:client_name}"
	tmux bind-key "$pane_screen_capture_key" run-shell "$CURRENT_DIR/scripts/screen_capture.sh"
	tmux bind-key "$save_complete_history_key" run-shell "$CURRENT_DIR/scripts/save_complete_history.sh"
	tmux bind-key "$clear_history_key" run-shell "$CURRENT_DIR/scripts/clear_history.sh"
}

main
