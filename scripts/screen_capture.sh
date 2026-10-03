#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source "$CURRENT_DIR/variables.sh" || exit 1
source "$CURRENT_DIR/shared.sh"

main() {
	supported_tmux_version_ok || return 1
	local file
	expand_tmux_format_path "${screen_capture_full_filename}" file || return 1
	capture_pane_to_file "$file" || return 1
	display_message "Screen capture saved to ${file}"
}
main
