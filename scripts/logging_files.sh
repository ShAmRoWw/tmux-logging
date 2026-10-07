# Filesystem-only helpers also work after the tmux server has exited.

logging_timestamp() {
	local stamp
	# date runs locally: setting TZ on a tmux CLI would not change the running
	# server's strftime timezone. LC_ALL makes the filename format predictable.
	stamp=$(TZ=Europe/Moscow LC_ALL=C date '+%Y-%m-%d_%H-%M-%S') || return 1
	[[ $stamp =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] || return 1
	printf '%s' "$stamp"
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
