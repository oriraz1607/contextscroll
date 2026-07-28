#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    desktop_user=$(id -un)
    desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
    gnome-extensions disable \
        contextscroll-pointer@contextscroll 2>/dev/null || true
    systemctl --user disable --now contextscroll-context.service \
        2>/dev/null || true
    if [[ -x /usr/lib/contextscroll/set-extension-enabled ]]; then
        /usr/lib/contextscroll/set-extension-enabled disable || true
    elif [[ -x scripts/set-extension-enabled.sh ]]; then
        scripts/set-extension-enabled.sh disable || true
    fi
    if [[ $desktop_home == /* && $desktop_home != / ]]; then
        user_extension="$desktop_home/.local/share/gnome-shell/extensions/contextscroll-pointer@contextscroll"
        if [[ -d $user_extension || -L $user_extension ]]; then
            rm -rf -- "$user_extension"
        fi
    fi
    exec sudo "$0" --system-only
fi

if (($#)); then
    [[ $# == 1 && $1 == --system-only ]] || {
        echo "Usage: $0" >&2
        exit 2
    }
fi

remove_service_account=false
if [[ -e /usr/lib/contextscroll/managed-system-user ]]; then
    remove_service_account=true
fi

systemctl disable --now contextscroll.service 2>/dev/null || true
systemctl --global disable contextscroll-context.service 2>/dev/null || true

rm -f /usr/lib/udev/rules.d/99-contextscroll.rules
if command -v udevadm >/dev/null; then
    udevadm control --reload-rules || true
fi
if getent passwd contextscroll >/dev/null && command -v setfacl >/dev/null; then
    for device_path in /dev/input/event* /dev/uinput; do
        [[ -e $device_path ]] || continue
        setfacl -x u:contextscroll "$device_path" 2>/dev/null || true
        setfacl -x g:contextscroll "$device_path" 2>/dev/null || true
    done
fi

rm -f /usr/bin/contextscroll
rm -f /usr/bin/contextscroll-context
rm -f /usr/lib/systemd/system/contextscroll.service
rm -f /usr/lib/systemd/user/contextscroll-context.service
rm -rf /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll
rm -f /usr/share/glib-2.0/schemas/org.contextscroll.gschema.xml
if command -v glib-compile-schemas >/dev/null; then
    glib-compile-schemas /usr/share/glib-2.0/schemas
fi
rm -rf /usr/lib/contextscroll

if [[ $remove_service_account == true ]]; then
    if getent passwd contextscroll >/dev/null; then
        userdel contextscroll
    fi
    if getent group contextscroll >/dev/null; then
        groupdel contextscroll
    fi
fi

systemctl daemon-reload

echo "ContextScroll was removed."
echo "/etc/contextscroll.conf was preserved."
echo "No other mouse service was started, stopped, installed, or removed."
