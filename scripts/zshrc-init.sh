#!/bin/sh
# Source this hook from an interactive shell after setting XDIGEST_PROJECT_DIR.
# The weekly trigger detaches itself, so no shell job notifications appear.

case "${XDIGEST_PROJECT_DIR:-}" in
    /*) ;;
    *)
        printf '%s\n' 'Set XDIGEST_PROJECT_DIR to the absolute Imladris checkout path before sourcing this hook.' >&2
        return 1
        ;;
esac

_XDIGEST_TRIGGER="$XDIGEST_PROJECT_DIR/scripts/weekly-shell-trigger.sh"
if [ ! -x "$_XDIGEST_TRIGGER" ]; then
    printf '%s\n' 'XDIGEST_PROJECT_DIR must point to a checkout with an executable scripts/weekly-shell-trigger.sh.' >&2
    unset _XDIGEST_TRIGGER
    return 1
fi

if "$_XDIGEST_TRIGGER" 1800 </dev/null >/dev/null 2>&1; then
    unset _XDIGEST_TRIGGER
else
    unset _XDIGEST_TRIGGER
    printf '%s\n' 'Imladris weekly trigger failed; check XDIGEST_PROJECT_DIR and checkout permissions.' >&2
    return 1
fi
