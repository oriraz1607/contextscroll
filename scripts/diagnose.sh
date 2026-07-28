#!/usr/bin/env bash
set -u

failed=0

check() {
    local label=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf 'ok    %s\n' "$label"
    else
        printf 'FAIL  %s\n' "$label"
        failed=1
    fi
}

check "Rust daemon installed" command -v contextscroll
check "Python helper installed" command -v contextscroll-context
check "settings schema available" gsettings list-keys org.contextscroll
check "python3-gi available" python3 -c 'import gi'
check "AT-SPI introspection available" python3 -c \
    'import gi; gi.require_version("Atspi", "2.0"); from gi.repository import Atspi'
if [[ ${XDG_SESSION_TYPE:-} == wayland ]]; then
    check "GNOME pointer bridge available" gdbus call --session \
        --dest org.contextscroll.Pointer \
        --object-path /org/contextscroll/Pointer \
        --method org.contextscroll.Pointer.GetSnapshot
else
    check "X11 pointer available" contextscroll-context --check-pointer
fi
check "uinput device available" test -e /dev/uinput
check "system daemon active" systemctl is-active --quiet contextscroll.service
daemon_user=$(systemctl show contextscroll.service --property=User --value \
    2>/dev/null)
check "system daemon unprivileged" test "$daemon_user" = contextscroll
check "session helper active" systemctl --user is-active --quiet \
    contextscroll-context.service
check "context socket present" test -S /run/contextscroll/context.sock
check "accessibility bus reachable" gdbus call --session \
    --dest org.a11y.Bus --object-path /org/a11y/bus \
    --method org.a11y.Bus.GetAddress

printf '\nSession: %s\n' "${XDG_SESSION_TYPE:-unknown}"
printf 'Desktop: %s\n' "${XDG_CURRENT_DESKTOP:-unknown}"
printf '\nRecent daemon log:\n'
journalctl -u contextscroll.service -n 8 --no-pager 2>/dev/null || true
printf '\nRecent helper log:\n'
journalctl --user -u contextscroll-context.service -n 8 --no-pager \
    2>/dev/null || true

exit "$failed"
