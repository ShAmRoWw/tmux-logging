#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# path to log file - global variable
FILE="$1"

python3_installed() {
	type python3 >/dev/null 2>&1 || return 1
}

ansifilter_installed() {
	type ansifilter >/dev/null 2>&1 || return 1
}

system_osx() {
	[ $(uname) == "Darwin" ]
}

# Best option: fixed-size VT100 screen emulator.  Lines reach the log
# only when they scroll off the top of the screen, so tab-completion
# menus (erased in-place) and full-screen apps (alternate buffer) never
# pollute the output.
pipe_pane_python3() {
	local cols=$(tmux display-message -p "#{pane_width}")
	local rows=$(tmux display-message -p "#{pane_height}")
	tmux pipe-pane "exec cat - | python3 '$CURRENT_DIR/logging_filter.py' $cols $rows >> '$FILE'"
}

pipe_pane_ansifilter() {
	tmux pipe-pane "exec cat - | ansifilter >> $FILE"
}

pipe_pane_sed_osx() {
	# Warning, very complex regex ahead.
	# Some characters below might not be visible from github web view.
	local ansi_codes_osx="(\[([0-9]{1,3}((;[0-9]{1,3})*)?)?[m|K]||]0;[^]+|[[:space:]]+$)"
	tmux pipe-pane "exec cat - | sed -E \"s/$ansi_codes_osx//g\" >> $FILE"
}

pipe_pane_sed() {
	local ansi_codes="(\x1B\[([0-9]{1,2}(;[0-9]{1,2})?)?[m|K]|)"
	tmux pipe-pane "exec cat - | sed -r 's/$ansi_codes//g' >> $FILE"
}

start_pipe_pane() {
	if python3_installed; then
		pipe_pane_python3
	elif ansifilter_installed; then
		pipe_pane_ansifilter
	elif system_osx; then
		# OSX uses sed '-E' flag and a slightly different regex
		pipe_pane_sed_osx
	else
		pipe_pane_sed
	fi
}

main() {
	start_pipe_pane
}
main
