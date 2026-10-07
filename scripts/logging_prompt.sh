# Interactive naming is used by the key binding. Direct script invocations
# retain their noninteractive behavior for automation.

source "$(dirname "${BASH_SOURCE[0]}")/logging_files.sh"

logging_recording_file() {
	local value
	value=$(logging_tmux show-option -gqv "@tmux-logging-file-$1" && printf '.') || return 1
	value=${value%.}
	value=${value%$'\n'}
	printf -v "$2" '%s' "$value"
}

logging_cleanup_result() {
	# A slow writer may still need the requested stop time after the UI exits.
	# Only the backend clears it; removing pending result ownership prevents
	# that writer from recreating an abandoned result option later.
	logging_tmux set-option -guq "@tmux-logging-result-$1" >/dev/null 2>&1
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

logging_choose_title() {
	local response
	response=$(logging_name_prompt "$1" "$2" "$3" && printf '.') || return 1
	response=${response%.}
	# An empty answer accepts the existing title without stripping it again.
	if [ -z "$response" ] || [ "$response" = "$3" ]; then
		response=$3
	else
		response=${response%.log}
	fi
	case "$response" in
		''|*/*|.|..)
			display_message 'Invalid log filename: enter a name without a directory'
			return 1 ;;
	esac
	printf -v "$4" '%s' "$response"
}

logging_finish_name() {
	local owner=$1 file=$2 client=$3 started=${4:-} ended=${5:-}
	local remaining renamed attempt title directory base result status
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
	if [ -n "$started" ]; then
		# The owner-specific marker distinguishes our generated names from an
		# explicit caller filename which happens to look like a timestamp.
		result=$(logging_tmux show-option -gqv "@tmux-logging-result-$owner" && printf '.') || return 1
		result=${result%.}
		result=${result%$'\n'}
		status=${result%%:*}
		if [ "$status" != 0 ]; then
			case "$status" in
				2) display_message 'Cannot rename log: filename already exists' ;;
				3) display_message 'Log saved under both names: could not remove original name' ;;
				*) display_message 'Ended logging: could not complete filename; original name kept' ;;
			esac
			return 1
		fi
		if [[ $file != *"__${started}.log" ]]; then
			display_message 'Ended logging: could not complete filename; original name kept'
			return 1
		fi
		base=${file%__"$started".log}
		file=${result#*:}
		ended=${file%.log}
		ended=${ended##*__}
		if [ -z "$client" ]; then
			display_message 'Ended logging'
			return 0
		fi
		# Finalize before the prompt: Escape or detach still keeps both times.
		title=${base##*/}
		directory=${file%/*}
		logging_choose_title "$client" 'Save log as (Enter = keep current)' "$title" title || return 0
		renamed="$directory/${title}__${started}__${ended}.log"
	else
		logging_choose_name "$client" 'Save log as (Enter = keep current)' "$file" renamed || return 0
	fi
	if [ "$renamed" = "$file" ]; then
		display_message 'Ended logging'
		return 0
	fi
	logging_change_name "$file" "$renamed" || return 1
	display_message 'Ended logging; file renamed'
}

logging_change_name() {
	local status
	if [ -e "$2" ] || [ -L "$2" ]; then
		display_message 'Cannot rename log: filename already exists'
		return 1
	fi
	logging_rename_file "$1" "$2"
	status=$?
	case "$status" in
		0) ;;
		2) display_message 'Cannot rename log: filename already exists' ;;
		3) display_message 'Log saved under both names: could not remove original name' ;;
		*) display_message 'Cannot rename log: original name kept' ;;
	esac
	return "$status"
}
