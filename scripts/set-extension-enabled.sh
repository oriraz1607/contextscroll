#!/usr/bin/env bash
set -euo pipefail

uuid=contextscroll-pointer@contextscroll
mode=${1:-}

case "$mode" in
    enable|disable) ;;
    *)
        echo "Usage: $0 enable|disable" >&2
        exit 2
        ;;
esac

current=$(gsettings get org.gnome.shell enabled-extensions)
present=false
if [[ $current == *"'$uuid'"* ]]; then
    present=true
fi

if [[ $mode == enable && $present == false ]]; then
    if [[ $current == "[]" || $current == "@as []" ]]; then
        updated="['$uuid']"
    else
        updated="${current%]}, '$uuid']"
    fi
    gsettings set org.gnome.shell enabled-extensions "$updated"
elif [[ $mode == disable && $present == true ]]; then
    updated=${current//"'$uuid', "/}
    updated=${updated//", '$uuid'"/}
    updated=${updated//"'$uuid'"/}
    if [[ $updated == "[]" ]]; then
        updated="@as []"
    fi
    gsettings set org.gnome.shell enabled-extensions "$updated"
fi
