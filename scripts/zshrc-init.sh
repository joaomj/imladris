#!/bin/sh
# Fire-and-forget hook for interactive shells. Launches the detached
# weekly trigger in the background and always succeeds fast.
# Usage from .zshrc: . $HOME/projects/x-digest/scripts/zshrc-init.sh

_XDIGEST_TRIGGER="$HOME/projects/x-digest/scripts/weekly-shell-trigger.sh"
if [ -x "$_XDIGEST_TRIGGER" ]; then
    "$_XDIGEST_TRIGGER" 1800 >/dev/null 2>&1 &
fi
unset _XDIGEST_TRIGGER
true
