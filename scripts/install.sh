#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

if [[ ${EUID} -eq 0 ]]; then
    echo "Run this installer as the desktop user, not as root." >&2
    exit 1
fi

start_services=true
from_source=false
graphical_auth=false
for argument in "$@"; do
    case "$argument" in
        --install-only) start_services=false ;;
        --from-source) from_source=true ;;
        --graphical-auth) graphical_auth=true ;;
        --help|-h)
            echo "Usage: $0 [--install-only] [--from-source] [--graphical-auth]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $argument" >&2
            exit 2
            ;;
    esac
done

privilege_command=(sudo)
if [[ $graphical_auth == true ]]; then
    command -v zenity >/dev/null || {
        echo "zenity is required for graphical authentication." >&2
        exit 1
    }
    [[ -x $project_dir/scripts/sudo-askpass.sh ]] || {
        echo "The graphical sudo helper is missing or not executable." >&2
        exit 1
    }
    export SUDO_ASKPASS="$project_dir/scripts/sudo-askpass.sh"
    privilege_command=(sudo --askpass)
fi

for requirement in install mktemp sha256sum stat sudo; do
    command -v "$requirement" >/dev/null || {
        echo "$requirement is required to install ContextScroll." >&2
        exit 1
    }
done

if [[ -x prebuilt/contextscroll && $from_source == false ]]; then
    [[ -f BUNDLE-MANIFEST.sha256 && ! -L BUNDLE-MANIFEST.sha256 ]] || {
        echo "The release bundle manifest is missing." >&2
        exit 1
    }
    sha256sum --strict --check BUNDLE-MANIFEST.sha256
    case "$(uname -m)" in
        x86_64) expected_target=x86_64-unknown-linux-musl ;;
        aarch64) expected_target=aarch64-unknown-linux-musl ;;
        *)
            echo "This release does not support architecture $(uname -m)." >&2
            exit 1
            ;;
    esac
    grep -qx "target=$expected_target" RELEASE-METADATA || {
        echo "The release bundle does not match this machine." >&2
        exit 1
    }
    daemon_binary=prebuilt/contextscroll
elif [[ $from_source == true ]]; then
    command -v cargo >/dev/null || {
        echo "cargo is required for a source installation." >&2
        exit 1
    }
    cargo build --release --locked
    daemon_binary=target/release/contextscroll
else
    echo "No verified release binary was found." >&2
    echo "Use a release bundle, or pass --from-source explicitly." >&2
    exit 1
fi

expected_version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
[[ $("$daemon_binary" --version) == "contextscroll $expected_version" ]] || {
    echo "The staged daemon version does not match Cargo.toml." >&2
    exit 1
}

desktop_user=$(id -un)
desktop_uid=$(id -u)
[[ $desktop_user != root && $desktop_user =~ ^[a-zA-Z0-9_.-]+$ ]] || {
    echo "Could not identify a safe desktop account." >&2
    exit 1
}

desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
[[ $desktop_home == /* && $desktop_home != / ]] || {
    echo "Desktop account has an invalid home directory." >&2
    exit 1
}
user_extension="$desktop_home/.local/share/gnome-shell/extensions/contextscroll-pointer@contextscroll"
if [[ -e $user_extension || -L $user_extension ]]; then
    extension_backup="${user_extension}.pre-v0.5.0-backup"
    if [[ -e $extension_backup || -L $extension_backup ]]; then
        echo "Cannot preserve the old user extension: $extension_backup exists." >&2
        exit 1
    fi
    mv -- "$user_extension" "$extension_backup"
    echo "Preserved the old user extension at $extension_backup."
fi

user_stage=$(mktemp -d -t contextscroll-user-stage.XXXXXXXX)
root_stage="/run/contextscroll-install.${desktop_uid}.${BASHPID}"

cleanup() {
    rm -rf -- "$user_stage"
}
trap cleanup EXIT

stage_file() {
    local mode=$1
    local source=$2
    local destination=$3
    [[ -f $source && ! -L $source ]] || {
        echo "Refusing non-regular installation source: $source" >&2
        exit 1
    }
    install -D -m "$mode" "$source" "$user_stage/payload/$destination"
}

stage_file 0755 "$daemon_binary" usr/bin/contextscroll
stage_file 0755 bin/contextscroll-context usr/bin/contextscroll-context
stage_file 0644 contextscroll/__init__.py usr/lib/contextscroll/contextscroll/__init__.py
stage_file 0644 contextscroll/classifier.py usr/lib/contextscroll/contextscroll/classifier.py
stage_file 0644 contextscroll/context_agent.py usr/lib/contextscroll/contextscroll/context_agent.py
stage_file 0644 contextscroll/pointer.py usr/lib/contextscroll/contextscroll/pointer.py
stage_file 0644 contextscroll/protocol.py usr/lib/contextscroll/contextscroll/protocol.py
stage_file 0755 scripts/set-extension-enabled.sh usr/lib/contextscroll/set-extension-enabled
stage_file 0644 gnome-extension/metadata.json usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/metadata.json
stage_file 0644 gnome-extension/extension.js usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/extension.js
stage_file 0644 gnome-extension/prefs.js usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/prefs.js
stage_file 0644 gnome-extension/icons/autoscroll-cursor.svg usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/autoscroll-cursor.svg
stage_file 0644 gnome-extension/icons/autoscroll-direction.svg usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/autoscroll-direction.svg
stage_file 0644 gnome-extension/schemas/org.contextscroll.gschema.xml usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas/org.contextscroll.gschema.xml
stage_file 0644 gnome-extension/schemas/org.contextscroll.gschema.xml usr/share/glib-2.0/schemas/org.contextscroll.gschema.xml
stage_file 0644 systemd/contextscroll.service usr/lib/systemd/system/contextscroll.service
stage_file 0644 systemd/contextscroll-context.service usr/lib/systemd/user/contextscroll-context.service
stage_file 0644 udev/99-contextscroll.rules usr/lib/udev/rules.d/99-contextscroll.rules
stage_file 0644 config/contextscroll.conf etc/contextscroll.conf

(
    cd "$user_stage"
    find payload -type f -print | LC_ALL=C sort |
        while IFS= read -r file; do
            sha256sum "$file"
        done > MANIFEST.sha256
)

"${privilege_command[@]}" install -d -m0700 "$root_stage"
"${privilege_command[@]}" install -m0700 scripts/install-root.sh "$root_stage/install-root"
"${privilege_command[@]}" install -m0600 "$user_stage/MANIFEST.sha256" "$root_stage/MANIFEST.sha256"
while IFS= read -r source; do
    relative=${source#"$user_stage/"}
    mode=$(stat -c '%a' "$source")
    "${privilege_command[@]}" install -D -m "$mode" "$source" "$root_stage/$relative"
done < <(find "$user_stage/payload" -type f -print | LC_ALL=C sort)

root_arguments=(--stage "$root_stage" --desktop-user "$desktop_user")
if [[ $start_services == true ]]; then
    root_arguments+=(--start)
fi
"${privilege_command[@]}" "$root_stage/install-root" "${root_arguments[@]}"
