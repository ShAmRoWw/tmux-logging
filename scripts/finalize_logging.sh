#!/usr/bin/env bash
# The writer calls this after closing the log, before publishing completion.
# No live tmux server is needed for the actual timestamp/rename operation.
CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$CURRENT_DIR/logging_files.sh"
source "$CURRENT_DIR/logging_state.sh"

if [ "${1:-}" = --timestamp ]; then
	logging_timestamp
	exit $?
fi

file=$1
started=$2
LOGGING_SOCKET=$3
owner=$4
LOGGING_PANE=$5
ended=${6:-}
[[ $started =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] &&
	[[ $file = *"__${started}.log" ]] &&
	[[ $owner =~ ^[a-zA-Z0-9_-]+:[0-9]+$ ]] || exit 1

# A guarded manual stop records its timestamp before closing the pane pipe.
# Natural EOF has no request, and last-pane exit may remove the server entirely.
request=$(logging_tmux show-option -gqv "@tmux-logging-stop-$owner" 2>/dev/null) || request=
manual=0
if [[ $request = requested:* ]]; then
	manual=1
	requested=${request#requested:}
	if [ -n "$requested" ]; then ended=$requested; fi
fi
if [ -z "$ended" ]; then ended=$(logging_timestamp) || ended=; fi

status=1
saved=$file
if [[ $ended =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]]; then
	destination="${file%.log}__${ended}.log"
	if [ -e "$destination" ] || [ -L "$destination" ]; then
		status=2
	else
		logging_rename_file "$file" "$destination"
		status=$?
	fi
	if [ "$status" = 0 ] || [ "$status" = 3 ]; then saved=$destination; fi
fi

if [ "$manual" = 1 ]; then
	# The result precedes removal of the filename completion marker. Native
	# quoting keeps paths literal, including quotes, formats and semicolons.
	logging_tmux if-shell -F "#{==:#{@tmux-logging-result-$owner},pending}" \
		"set-option -gq '@tmux-logging-result-$owner' $(quote_tmux_argument "$status:$saved")" >/dev/null 2>&1
elif [ "$status" != 0 ]; then
	case "$status" in
		2) message='Cannot rename log: filename already exists' ;;
		3) message='Log saved under both names: could not remove original name' ;;
		*) message='Cannot finalize log filename: original name kept' ;;
	esac
	logging_tmux display-message "$message" >/dev/null 2>&1
fi
exit "$status"
