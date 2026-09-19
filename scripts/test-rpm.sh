#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
work=$(mktemp -d -t contextscroll-rpm-test.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT

install -m 0755 /bin/true "$work/contextscroll"
printf '{"spdxVersion":"SPDX-2.3"}\n' > "$work/SBOM.spdx.json"
SOURCE_DATE_EPOCH=1 ./scripts/build-bundle.sh \
    --target x86_64-unknown-linux-musl \
    --binary "$work/contextscroll" \
    --sbom "$work/SBOM.spdx.json" \
    --output "$work" >/dev/null
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
./scripts/build-rpm.sh \
    --bundle "$work/contextscroll-v${version}-linux-x86_64.tar.gz" \
    --output "$work" >"$work/build.log" 2>&1 || {
    cat "$work/build.log" >&2
    exit 1
}
package="$work/contextscroll-${version}-1.x86_64.rpm"
[[ -f $package ]]
[[ $(rpm -qp --qf '%{NAME} %{VERSION} %{ARCH}' "$package") == "contextscroll $version x86_64" ]]
for path in \
    /usr/bin/contextscroll \
    /usr/bin/contextscroll-context \
    /usr/lib/systemd/system/contextscroll.service \
    /usr/lib/systemd/user/contextscroll-context.service \
    /usr/lib/udev/rules.d/99-contextscroll.rules \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/prefs.js \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/rule-validation.js \
    /usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll/schemas/gschemas.compiled \
    /etc/contextscroll.conf; do
    rpm -qpl "$package" | grep -Fqx -- "$path"
done
echo 'RPM metadata and installed file inventory passed.'
