#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || {
    echo "The staged installer must run as root." >&2
    exit 1
}

stage=
desktop_user=
start_services=false
while (($#)); do
    case "$1" in
        --stage)
            stage=${2:-}
            shift 2
            ;;
        --desktop-user)
            desktop_user=${2:-}
            shift 2
            ;;
        --start)
            start_services=true
            shift
            ;;
        *)
            echo "Unknown staged-installer argument: $1" >&2
            exit 2
            ;;
    esac
done

[[ $stage =~ ^/run/contextscroll-install\.[0-9]+\.[0-9]+$ ]] || {
    echo "Invalid root staging path." >&2
    exit 1
}
stage_uid=${stage#/run/contextscroll-install.}
stage_uid=${stage_uid%%.*}
[[ -d $stage && ! -L $stage && $(stat -c '%u:%a' "$stage") == 0:700 ]] || {
    echo "Root staging directory has unsafe ownership or permissions." >&2
    exit 1
}
[[ $desktop_user =~ ^[a-zA-Z0-9_.-]+$ && $desktop_user != root ]] || {
    echo "Invalid desktop account." >&2
    exit 1
}
[[ $(id -u "$desktop_user") == "$stage_uid" ]] || {
    echo "Desktop account does not own the installation request." >&2
    exit 1
}

cleanup() {
    if [[ $stage =~ ^/run/contextscroll-install\.[0-9]+\.[0-9]+$ ]]; then
        rm -rf -- "$stage"
    fi
}
trap cleanup EXIT

cd "$stage"
[[ -f MANIFEST.sha256 && ! -L MANIFEST.sha256 ]] || {
    echo "Installation manifest is missing." >&2
    exit 1
}
if find payload -type l -o -type f ! -user root | grep -q .; then
    echo "Installation payload contains a link or non-root-owned file." >&2
    exit 1
fi
while IFS= read -r file; do
    [[ $(stat -c '%h' "$file") == 1 ]] || {
        echo "Installation payload contains a hard link: $file" >&2
        exit 1
    }
done < <(find payload -type f -print)

manifest_files=$(mktemp)
payload_files=$(mktemp)
trap 'rm -f -- "$manifest_files" "$payload_files"; cleanup' EXIT
awk '{print $2}' MANIFEST.sha256 | LC_ALL=C sort > "$manifest_files"
find payload -type f -print | LC_ALL=C sort > "$payload_files"
cmp -s "$manifest_files" "$payload_files" || {
    echo "Installation manifest does not exactly match the payload." >&2
    exit 1
}
sha256sum --strict --check MANIFEST.sha256

for requirement in getent groupadd useradd setfacl sudo udevadm glib-compile-schemas; do
    command -v "$requirement" >/dev/null || {
        echo "$requirement is required to install ContextScroll." >&2
        exit 1
    }
done

install_default_config=false
if [[ -L /etc/contextscroll.conf ]]; then
    echo "Refusing to replace symlinked /etc/contextscroll.conf." >&2
    exit 1
elif [[ -e /etc/contextscroll.conf && ! -f /etc/contextscroll.conf ]]; then
    echo "Refusing non-regular /etc/contextscroll.conf." >&2
    exit 1
elif [[ ! -e /etc/contextscroll.conf ]]; then
    install_default_config=true
fi

service_user=contextscroll
service_group=contextscroll
service_account_created=false

if getent passwd "$service_user" >/dev/null &&
    ! getent group "$service_group" >/dev/null; then
    echo "$service_user exists without its expected primary group." >&2
    exit 1
fi

if getent group "$service_group" >/dev/null; then
    service_gid=$(getent group "$service_group" | cut -d: -f3)
    group_members=$(getent group "$service_group" | cut -d: -f4)
    [[ $service_gid =~ ^[0-9]+$ && $service_gid -lt 1000 ]] || {
        echo "$service_group is not a system GID." >&2
        exit 1
    }
    [[ -z $group_members ]] || {
        echo "$service_group has unexpected supplementary members." >&2
        exit 1
    }
else
    groupadd --system "$service_group"
fi

if getent passwd "$service_user" >/dev/null; then
    passwd_entry=$(getent passwd "$service_user")
    service_uid=$(cut -d: -f3 <<< "$passwd_entry")
    service_home=$(cut -d: -f6 <<< "$passwd_entry")
    service_shell=$(cut -d: -f7 <<< "$passwd_entry")
    actual_group=$(id -gn "$service_user")
    all_groups=$(id -Gn "$service_user")
    [[ $service_uid =~ ^[0-9]+$ && $service_uid -lt 1000 ]] || {
        echo "$service_user is not a system UID." >&2
        exit 1
    }
    [[ $actual_group == "$service_group" && $all_groups == "$service_group" ]] || {
        echo "$service_user has unexpected group membership." >&2
        exit 1
    }
    [[ $service_home == /nonexistent ]] || {
        echo "$service_user has an unexpected home directory." >&2
        exit 1
    }
    case "$service_shell" in
        */nologin|/bin/false) ;;
        *)
            echo "$service_user has a login shell." >&2
            exit 1
            ;;
    esac
else
    nologin_shell=$(command -v nologin || true)
    [[ -n $nologin_shell ]] || nologin_shell=/usr/sbin/nologin
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

install -d -m0755 /usr/lib/contextscroll
if [[ $service_account_created == true ]]; then
    install -m0644 /dev/null /usr/lib/contextscroll/managed-system-user
fi

install_payload() {
    local mode=$1
    local relative=$2
    local destination="/$relative"
    local parent
    local name
    local temporary
    parent=$(dirname "$destination")
    name=$(basename "$destination")
    temporary="$parent/.${name}.contextscroll-install.$$"
    install -d -m0755 "$parent"
    install -m "$mode" "payload/$relative" "$temporary"
    mv -fT -- "$temporary" "$destination"
}

install_payload 0755 usr/bin/contextscroll
install_payload 0755 usr/bin/contextscroll-context
for module in __init__.py classifier.py context_agent.py pointer.py protocol.py; do
    install_payload 0644 "usr/lib/contextscroll/contextscroll/$module"
done
install_payload 0755 usr/lib/contextscroll/set-extension-enabled
for extension_file in \
    metadata.json extension.js prefs.js autoscroll-cursor.svg \
    autoscroll-direction.svg schemas/org.contextscroll.gschema.xml; do
    install_payload 0644 \
        "usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/$extension_file"
done
install_payload 0644 usr/share/glib-2.0/schemas/org.contextscroll.gschema.xml
install_payload 0644 usr/lib/systemd/system/contextscroll.service
install_payload 0644 usr/lib/systemd/user/contextscroll-context.service
install_payload 0644 usr/lib/udev/rules.d/99-contextscroll.rules

glib-compile-schemas /usr/share/glib-2.0/schemas
glib-compile-schemas \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas

if [[ $install_default_config == true ]]; then
    install_payload 0644 etc/contextscroll.conf
fi

desktop_uid=$(id -u "$desktop_user")
[[ $desktop_uid =~ ^[0-9]+$ ]] || {
    echo "Desktop account metadata is invalid." >&2
    exit 1
}

udevadm control --reload-rules
# Remove permissions left by releases that granted access through the
# contextscroll group; current releases use a direct account ACL.
for device_path in /dev/input/event* /dev/uinput; do
    [[ -e $device_path ]] || continue
    setfacl -x g:contextscroll "$device_path" 2>/dev/null || true
done
udevadm trigger --action=change --subsystem-match=input --settle
udevadm trigger --action=change --name-match=uinput --settle
systemctl daemon-reload
systemctl --global disable contextscroll-context.service 2>/dev/null || true
sudo -u "$desktop_user" \
    XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
    systemctl --user daemon-reload || true

if [[ $start_services == false ]]; then
    echo
    echo "ContextScroll is installed but was not started."
    exit 0
fi

systemctl enable contextscroll.service
systemctl restart contextscroll.service
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

echo
echo "ContextScroll is installed and running."
echo "Run scripts/diagnose.sh from your desktop session to verify context."
echo "Sign out and back in once if this was the first installation."
