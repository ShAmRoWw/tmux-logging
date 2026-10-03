get_tmux_option() {
	local option=$1
	local default_value=$2
	local option_value=$(tmux show-option -gqv "$option")
	if [ -z "$option_value" ]; then
		echo $default_value
	else
		echo $option_value
	fi
}

# Ensures a message is displayed for 5 seconds in tmux prompt
display_message() {
	local message=$1

	# display_duration defaults to 5 seconds, if not passed as an argument
	if [ "$#" -eq 2 ]; then
		local display_duration=$2
	else
		local display_duration="5000"
	fi

	# saves user-set 'display-time' option
	local saved_display_time=$(get_tmux_option "display-time" "750")

	# sets message display time to 5 seconds
	tmux set-option -gq display-time "$display_duration"

	# displays message
	tmux display-message "$message"

	# restores original 'display-time' value
	tmux set-option -gq display-time "$saved_display_time"
}

# Publish a capture only after both capture-pane and trimming succeed.
# The subshell keeps pipeline settings and cleanup traps local to this capture.
capture_pane_to_file() (
	local file=$1
	shift
	local directory=${file%/*}
	local temporary
	if [ "$directory" = "$file" ]; then
		directory=.
	elif [ -z "$directory" ]; then
		directory=/
	fi
	case "$directory" in
		/*|./*|../*) ;;
		*) directory="./$directory" ;;
	esac
	if [ -d "$file" ]; then
		printf 'Cannot save capture to directory: %s\n' "$file" >&2
		return 1
	fi
	temporary=$(mktemp "$directory/.tmux-logging.XXXXXX") || return 1
	trap 'rm -f -- "$temporary"' EXIT
	trap 'exit 1' HUP INT TERM
	set -o pipefail

	# Buffer only a count of empty rows, never the entire pane history.
	# Keep the previous single-newline result for a completely empty capture.
	tmux capture-pane -J -p "$@" | awk '
		length($0) == 0 { empty++; next }
		{
			while (empty > 0) { print ""; empty-- }
			print
			printed = 1
		}
		END { if (!printed) print "" }
	' > "$temporary" || return 1
	mv -f -- "$temporary" "$file"
)

supported_tmux_version_ok() {
	"$CURRENT_DIR/check_tmux_version.sh" "$SUPPORTED_VERSION"
}

# Checking full path to logfile and expanding tmux format in normal path
# As example: expand %Y-%m-%d to current date
expand_tmux_format_path() {
	local tmux_format_path=$1
	local full_path
	local target_args=()
	if [ -n "${3:-}" ]; then target_args=(-t "$3"); fi
	# A sentinel retains newlines belonging to the filename. Remove only the
	# single newline appended by tmux itself.
	full_path=$(tmux display-message -p "${target_args[@]}" "${tmux_format_path}" && printf '.') || return 1
	full_path=${full_path%.}
	full_path=${full_path%$'\n'}
	local full_directory_path=${full_path%/*}
	if [ "$full_directory_path" = "$full_path" ]; then
		full_directory_path=.
	elif [ -z "$full_directory_path" ]; then
		full_directory_path=/
	fi
	mkdir -p -- "${full_directory_path}" || return 1
	if [ "$#" -gt 1 ]; then
		# Optional output variable avoids stripping trailing filename newlines
		# in a caller's command substitution (also supported by Bash 3.2).
		printf -v "$2" '%s' "$full_path"
	else
		printf '%s\n' "$full_path"
	fi
}
