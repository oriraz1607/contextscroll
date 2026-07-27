#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

remove_service_account=false
if [[ -e /usr/lib/contextscroll/managed-system-user ]]; then
    remove_service_account=true
fi

desktop_user=${SUDO_USER:-}
if [[ -z $desktop_user && ${PKEXEC_UID:-} =~ ^[0-9]+$ ]]; then
    desktop_user=$(id -nu "$PKEXEC_UID")
fi
if [[ -n $desktop_user && $desktop_user != root ]]; then
    desktop_uid=$(id -u "$desktop_user")
    desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        gnome-extensions disable contextscroll-pointer@contextscroll 2>/dev/null || true
    if [[ -x /usr/lib/contextscroll/set-extension-enabled ]]; then
        sudo -u "$desktop_user" \
            XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
            /usr/lib/contextscroll/set-extension-enabled disable || true
    fi
    if [[ -n $desktop_home && $desktop_home == /* && $desktop_home != / ]]; then
        user_extension="$desktop_home/.local/share/gnome-shell/extensions/contextscroll-pointer@contextscroll"
        if [[ -d $user_extension ]]; then
            rm -rf -- "$user_extension"
        fi
    fi
fi

systemctl disable --now contextscroll.service 2>/dev/null || true
systemctl --global disable contextscroll-context.service 2>/dev/null || true

rm -f /usr/lib/udev/rules.d/99-contextscroll.rules
if command -v udevadm >/dev/null; then
    udevadm control --reload-rules || true
fi
if getent group contextscroll >/dev/null && command -v setfacl >/dev/null; then
    for device_path in /dev/input/event* /dev/uinput; do
        [[ -e $device_path ]] || continue
        setfacl -x g:contextscroll "$device_path" 2>/dev/null || true
    done
fi

rm -f /usr/bin/contextscroll
rm -f /usr/bin/contextscroll-context
rm -f /usr/lib/systemd/system/contextscroll.service
rm -f /usr/lib/systemd/user/contextscroll-context.service
rm -rf /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll
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
