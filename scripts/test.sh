#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

cargo test --all-targets --locked
python3 -m unittest discover -s tests -v
python3 -m compileall -q contextscroll bin/contextscroll-context

schema_test_dir=$(mktemp -d)
trap 'rm -rf -- "$schema_test_dir"' EXIT
install -m644 gnome-extension/schemas/org.contextscroll.gschema.xml \
    "$schema_test_dir/"
glib-compile-schemas --strict "$schema_test_dir"
node --check gnome-extension/extension.js
node --check gnome-extension/prefs.js
if grep -Eq '\?\.[A-Za-z_$][A-Za-z0-9_$]*[[:space:]]*=' \
    gnome-extension/*.js; then
    echo "GJS does not support assignment through optional chaining." >&2
    exit 1
fi

for script in scripts/*.sh; do
    bash -n "$script"
done

grep -qx 'User=contextscroll' systemd/contextscroll.service
grep -qx 'Group=contextscroll' systemd/contextscroll.service
grep -Fq 'ENV{ID_INPUT_MOUSE}=="1"' udev/99-contextscroll.rules
grep -Fq 'ATTRS{name}!="ContextScroll virtual: *"' \
    udev/99-contextscroll.rules
