#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

start_services=true
for argument in "$@"; do
    case "$argument" in
        --install-only) start_services=false ;;
        --help|-h)
            echo "Usage: $0 [--install-only]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $argument" >&2
            exit 2
            ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    command -v cargo >/dev/null || {
        echo "cargo is required to build ContextScroll." >&2
        exit 1
    }
    cargo build --release --locked
    exec sudo "$0" "$@"
fi

desktop_user=${SUDO_USER:-}
if [[ -z $desktop_user && ${PKEXEC_UID:-} =~ ^[0-9]+$ ]]; then
    desktop_user=$(id -nu "$PKEXEC_UID")
fi

[[ -x target/release/contextscroll ]] || {
    echo "Release binary is missing. Run this script as your desktop user first." >&2
    exit 1
}

for requirement in getent groupadd useradd setfacl udevadm; do
    command -v "$requirement" >/dev/null || {
        echo "$requirement is required to install ContextScroll." >&2
        exit 1
    }
done

service_user=contextscroll
service_group=contextscroll
service_account_created=false

if ! getent group "$service_group" >/dev/null; then
    groupadd --system "$service_group"
fi

if getent passwd "$service_user" >/dev/null; then
    actual_group=$(id -gn "$service_user")
    if [[ $actual_group != "$service_group" ]]; then
        echo "$service_user already exists with primary group $actual_group." >&2
        echo "Refusing to reuse an unrelated system account." >&2
        exit 1
    fi
else
    nologin_shell=$(command -v nologin || true)
    if [[ -z $nologin_shell ]]; then
        nologin_shell=/usr/sbin/nologin
    fi
    useradd \
        --system \
        --gid "$service_group" \
        --home-dir /nonexistent \
        --no-create-home \
        --shell "$nologin_shell" \
        --comment "ContextScroll input daemon" \
        "$service_user"
    service_account_created=true
fi

install -d -m755 /usr/lib/contextscroll
if [[ $service_account_created == true ]]; then
    install -m644 /dev/null /usr/lib/contextscroll/managed-system-user
fi

install -Dm755 target/release/contextscroll /usr/bin/contextscroll
install -Dm755 bin/contextscroll-context /usr/bin/contextscroll-context
install -d -m755 /usr/lib/contextscroll/contextscroll
install -m644 contextscroll/__init__.py /usr/lib/contextscroll/contextscroll/
install -m644 contextscroll/classifier.py /usr/lib/contextscroll/contextscroll/
install -m644 contextscroll/context_agent.py /usr/lib/contextscroll/contextscroll/
install -m644 contextscroll/pointer.py /usr/lib/contextscroll/contextscroll/
install -m644 contextscroll/protocol.py /usr/lib/contextscroll/contextscroll/
install -m755 scripts/set-extension-enabled.sh \
    /usr/lib/contextscroll/set-extension-enabled
install -Dm644 gnome-extension/metadata.json \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/metadata.json
install -Dm644 gnome-extension/extension.js \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/extension.js
install -Dm644 gnome-extension/icons/autoscroll-cursor.svg \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/autoscroll-cursor.svg
if [[ -n $desktop_user && $desktop_user != root ]]; then
    desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
    desktop_group=$(id -gn "$desktop_user")
    user_extension="$desktop_home/.local/share/gnome-shell/extensions/contextscroll-pointer@contextscroll"
    install -d -m755 -o "$desktop_user" -g "$desktop_group" \
        "$user_extension"
    install -m644 -o "$desktop_user" -g "$desktop_group" \
        gnome-extension/metadata.json "$user_extension/metadata.json"
    install -m644 -o "$desktop_user" -g "$desktop_group" \
        gnome-extension/extension.js "$user_extension/extension.js"
    install -m644 -o "$desktop_user" -g "$desktop_group" \
        gnome-extension/icons/autoscroll-cursor.svg \
        "$user_extension/autoscroll-cursor.svg"
fi
install -Dm644 systemd/contextscroll.service \
    /usr/lib/systemd/system/contextscroll.service
install -Dm644 systemd/contextscroll-context.service \
    /usr/lib/systemd/user/contextscroll-context.service
install -Dm644 udev/99-contextscroll.rules \
    /usr/lib/udev/rules.d/99-contextscroll.rules

if [[ ! -e /etc/contextscroll.conf ]]; then
    install -Dm644 config/contextscroll.conf /etc/contextscroll.conf
elif grep -qx 'SPEED_MULTIPLIER = 0.008' /etc/contextscroll.conf; then
    # Migrate the original default while preserving every other user setting.
    sed -i 's/^SPEED_MULTIPLIER = 0\.008$/SPEED_MULTIPLIER = 0.0112/' \
        /etc/contextscroll.conf
fi

udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=input --settle
udevadm trigger --action=change --name-match=uinput --settle
systemctl daemon-reload
if [[ -n $desktop_user && $desktop_user != root ]]; then
    desktop_uid=$(id -u "$desktop_user")
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        systemctl --user daemon-reload || true
fi

if [[ $start_services == false ]]; then
    echo
    echo "ContextScroll is installed but was not started."
    exit 0
fi

systemctl enable contextscroll.service
systemctl restart contextscroll.service
systemctl --global enable contextscroll-context.service

if [[ -n $desktop_user && $desktop_user != root ]]; then
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        /usr/lib/contextscroll/set-extension-enabled enable || true
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        gnome-extensions disable contextscroll-pointer@contextscroll || true
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        gnome-extensions enable contextscroll-pointer@contextscroll || true
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        systemctl --user enable contextscroll-context.service || true
    sudo -u "$desktop_user" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
        systemctl --user restart contextscroll-context.service || true
fi

echo
echo "ContextScroll is installed and running."
echo "Run scripts/diagnose.sh from your desktop session to verify context."
echo "Sign out and back in once if this was the first installation."
