# Interactive naming is used by the key binding. Direct script invocations
# retain their noninteractive behavior for automation.

logging_recording_file() {
	local value
	value=$(logging_tmux show-option -gqv "@tmux-logging-file-$1" && printf '.') || return 1
	value=${value%.}
	value=${value%$'\n'}
	printf -v "$2" '%s' "$value"
}

logging_name_prompt() (
	local client=$1 label=$2 initial=$3
	local option="@tmux-logging-input-$$-$RANDOM-$RANDOM"
	local command child= client_pid result
	trap '[ -z "$child" ] || { kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; }; logging_tmux set-option -guq "$option" \; set-option -guq "$option-initial" >/dev/null 2>&1' EXIT
	trap 'exit 1' HUP INT TERM
	[ -n "$client" ] || return 1
	client_pid=$(logging_tmux display-message -p -c "$client" '#{client_pid}') || return 1
	[ -n "$client_pid" ] || return 1
	# Expand an option once: its contents are not reinterpreted as formats or
	# strftime directives. -l keeps commas from splitting the input field.
	command="set-option -gq '$option-initial' $(quote_tmux_argument "$initial") ; "
	# Parse a real command block before the user types. Substitution then
	# copies the answer into one argument, without reparsing it as commands.
	# A string template containing %% is unsafe for arbitrary names in 3.7.
	command+="command-prompt -t $(quote_tmux_argument "$client") -l -p $(quote_tmux_argument "$label ") -I '#{$option-initial}' { set-option -gq '$option' \"answer:%1\" }"
	tmux -S "$LOGGING_SOCKET" if-shell -F 1 "$command" >/dev/null 2>&1 &
	child=$!
	while kill -0 "$child" 2>/dev/null; do
		# tmux 3.7 does not release a waiting command-prompt on client detach.
		[ "$(logging_tmux display-message -p -c "$client" '#{client_pid}' 2>/dev/null)" = "$client_pid" ] || return 1
		sleep 0.1
	done
	wait "$child" || return 1
	child=
	result=$(logging_tmux show-option -gqv "$option" && printf '.') || return 1
	result=${result%.}
	result=${result%$'\n'}
	[[ $result = answer:* ]] || return 1
	printf '%s' "${result#answer:}"
)

logging_choose_name() {
	local client=$1 label=$2 original=$3 answer directory
	answer=$(logging_name_prompt "$client" "$label" "${original##*/}" && printf '.') || return 1
	answer=${answer%.}
	if [ -z "$answer" ]; then
		printf -v "$4" '%s' "$original"
		return 0
	fi
	case "$answer" in
		*/*|.|..)
			display_message 'Invalid log filename: enter a name without a directory'
			return 1 ;;
	esac
	directory=${original%/*}
	if [ "$directory" = "$original" ]; then directory=.; fi
	printf -v "$4" '%s' "$directory/$answer"
}

logging_finish_name() {
	local owner=$1 file=$2 client=$3 remaining renamed attempt status
	if [ -z "$file" ]; then
		display_message 'Ended logging: original filename is unavailable'
		return 0
	fi
	for ((attempt=0; attempt<100; attempt++)); do
		logging_recording_file "$owner" remaining || break
		[ -n "$remaining" ] || break
		sleep 0.1
	done
	if [ -n "$remaining" ]; then
		display_message 'Ended logging: file is still closing; name kept'
		return 1
	fi
	logging_choose_name "$client" 'Save log as (Enter = keep current)' "$file" renamed || return 0
	if [ "$renamed" = "$file" ]; then
		display_message 'Ended logging'
		return 0
	fi
	if [ -e "$renamed" ] || [ -L "$renamed" ]; then
		display_message 'Cannot rename log: filename already exists'
		return 1
	fi
	logging_rename_file "$file" "$renamed"
	status=$?
	case "$status" in
		0) display_message 'Ended logging; file renamed' ;;
		2) display_message 'Cannot rename log: filename already exists' ;;
		3) display_message 'Log saved under both names: could not remove original name' ;;
		*) display_message 'Cannot rename log: original name kept' ;;
	esac
	return "$status"
}

logging_rename_file() (
	local original=$1 destination=$2 directory stage basename
	directory=${destination%/*}
	basename=${destination##*/}
	stage=$(mktemp -d "$directory/.tmux-logging-rename.XXXXXXXX") || return 1
	trap 'rm -rf -- "$stage"' EXIT
	trap 'exit 1' HUP INT TERM
	# A hard link publishes the new name without replacing an existing entry.
	# Pass a fixed parent directory to the final ln: even a destination that
	# becomes a directory during the operation then fails with EEXIST, rather
	# than moving the log inside that directory. -P preserves symbolic links.
	ln -P -- "$original" "$stage/$basename" || return 1
	if ! ln -P -- "$stage/$basename" "$directory/"; then
		if [ -e "$destination" ] || [ -L "$destination" ]; then return 2; fi
		return 1
	fi
	rm -- "$original" || return 3
)
