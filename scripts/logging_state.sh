# Recording ownership is independent of session names and pane indexes.
# Mutations compare ownership in tmux, including after pipe-pane hooks run.
logging_tmux() {
	tmux -S "$LOGGING_SOCKET" "$@"
}

logging_target() {
	local target=${1:-${TMUX_PANE:-}}
	local metadata version
	local args=()
	if [ -n "$target" ]; then args=(-t "$target"); fi
	metadata=$(tmux display-message -p "${args[@]}" '#{pane_id}|#{version}') || return 1
	LOGGING_PANE=${metadata%%|*}
	version=${metadata#*|}
	if [[ ! $LOGGING_PANE =~ ^%[0-9]+$ ]]; then
		printf 'tmux-logging: cannot resolve pane\n' >&2
		return 1
	fi
	if [[ ! $version =~ ^([0-9]+)\.([0-9]+) ]] ||
		(( BASH_REMATCH[1] < 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] < 7) )); then
		printf 'tmux-logging: recording ownership requires tmux 3.7 or newer\n' >&2
		return 1
	fi
	LOGGING_SOCKET=$(tmux display-message -p -t "$LOGGING_PANE" '#{socket_path}' && printf '.') || return 1
	LOGGING_SOCKET=${LOGGING_SOCKET%.}
	LOGGING_SOCKET=${LOGGING_SOCKET%$'\n'}
	LOGGING_OPTION="@tmux-logging-$LOGGING_PANE"
}

logging_read_state() {
	local state
	state=$(logging_tmux display-message -p -t "$LOGGING_PANE" \
		"#{pane_pipe}|#{pane_pipe_pid}|#{$LOGGING_OPTION}") || return 1
	IFS='|' read -r LOGGING_ACTIVE LOGGING_PID LOGGING_OWNER <<< "$state"
}

logging_is_owned() {
	[ "$LOGGING_ACTIVE" = 1 ] &&
		[[ $LOGGING_OWNER =~ ^[a-zA-Z0-9_-]+:[0-9]+$ ]] &&
		[ "${LOGGING_OWNER##*:}" = "$LOGGING_PID" ]
}

logging_owner_condition() {
	printf '#{==:#{%s},%s}' "$LOGGING_OPTION" "$1"
}

logging_pipe_condition() {
	printf '#{&&:%s,#{&&:#{pane_pipe},#{==:#{pane_pipe_pid},%s}}}' \
		"$(logging_owner_condition "$1")" "${1##*:}"
}

# One quoting pass for tmux's command parser (no shell or format expansion).
quote_tmux_argument() {
	local value=$1
	value=${value//\\/\\\\}
	value=${value//\"/\\\"}
	value=${value//\$/\\\$}
	value=${value//$'\n'/\\n}
	value=${value//$'\r'/\\r}
	value=${value//$'\t'/\\t}
	printf '"%s"' "$value"
}

logging_cleanup() {
	local owner=$1
	# A replacement pipe may be running before the old reader exits. Clear
	# only our old registration, and close only our own still-installed pipe.
	logging_tmux if-shell -F -t "$LOGGING_PANE" "$(logging_pipe_condition "$owner")" \
		"pipe-pane -t '$LOGGING_PANE'" \; \
		if-shell -F -t "$LOGGING_PANE" "$(logging_owner_condition "$owner")" \
		"set-option -guq '$LOGGING_OPTION'" >/dev/null 2>&1
}

logging_stop() {
	local clear
	clear="set-option -guq '$LOGGING_OPTION'"
	logging_tmux if-shell -F -t "$LOGGING_PANE" "$(logging_pipe_condition "$1")" \
		"pipe-pane -t '$LOGGING_PANE' ; if-shell -F -t '$LOGGING_PANE' '$(logging_owner_condition "$1")' $(quote_tmux_argument "$clear") ; display-message -p stopped" \
		'display-message -p changed'
}
