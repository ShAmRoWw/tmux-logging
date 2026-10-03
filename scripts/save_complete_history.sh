#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source "$CURRENT_DIR/variables.sh" || exit 1
source "$CURRENT_DIR/shared.sh"

main() {
	supported_tmux_version_ok || return 1
	local file history_limit
	expand_tmux_format_path "${save_complete_history_full_filename}" file || return 1
	history_limit=$(tmux display-message -p -F "#{history_limit}") || return 1
	capture_pane_to_file "$file" -S "-${history_limit}" || return 1
	display_message "History saved to ${file}"
}
main
