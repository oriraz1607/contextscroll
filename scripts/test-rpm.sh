#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
work=$(mktemp -d -t contextscroll-rpm-test.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT

install -m 0755 /bin/true "$work/contextscroll"
printf '{"spdxVersion":"SPDX-2.3"}\n' > "$work/SBOM.spdx.json"
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)

check_package() {
    local package=$1 architecture=$2 label=$3
    local extension=/usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll
    local payload="$work/payload-$label-$architecture"
    local inventory="$work/files-$label-$architecture"
    local path

    [[ -f $package ]]
    [[ $(rpm -qp --qf '%{NAME} %{VERSION} %{ARCH}' "$package") == "contextscroll $version $architecture" ]]
    rpm -qpl "$package" > "$inventory"
    for path in \
        /usr/bin/contextscroll \
        /usr/bin/contextscroll-context \
        /usr/lib/systemd/system/contextscroll.service \
        /usr/lib/systemd/user/contextscroll-context.service \
        /usr/lib/udev/rules.d/99-contextscroll.rules \
        "$extension/metadata.json" \
        "$extension/extension.js" \
        "$extension/prefs.js" \
        "$extension/rule-validation.js" \
        "$extension/autoscroll-cursor.svg" \
        "$extension/autoscroll-direction.svg" \
        "$extension/schemas/org.contextscroll.gschema.xml" \
        "$extension/schemas/gschemas.compiled" \
        /etc/contextscroll.conf; do
        grep -Fqx -- "$path" "$inventory" || {
            echo "$label $architecture RPM is missing required runtime file: $path" >&2
            exit 1
        }
    done
    if grep -Eq "^$extension/icons(/|$)" "$inventory"; then
        echo "$label $architecture RPM contains the obsolete extension icons/ directory." >&2
        exit 1
    fi

    mkdir -p "$payload"
    rpm2cpio "$package" | (
        cd "$payload"
        cpio --extract --make-directories --quiet --no-absolute-filenames
    )
    python3 - "$payload" <<'PY'
from pathlib import Path
import re
import shlex
import sys

payload = Path(sys.argv[1])
extension = Path("usr/share/gnome-shell/extensions/contextscroll-pointer@contextscroll")

# Check paths used by the packaged extension, not merely the presence of SVGs.
source = (payload / extension / "extension.js").read_text()
assets = re.findall(r"\$\{this\.path\}/([^`]+\.svg)", source)
assert assets, "No extension-relative cursor asset references found"
for asset in assets:
    path = payload / extension / asset
    assert path.is_file() and path.stat().st_size, f"Runtime cursor asset missing or empty: {path}"

# The script installer's staging declarations must produce the same extension
# files as each RPM, without running the privileged installer during tests.
for line in Path("scripts/install.sh").read_text().splitlines():
    if not line.startswith("stage_file "):
        continue
    _, _, source, destination = shlex.split(line)
    if not destination.startswith(f"{extension}/"):
        continue
    installed = payload / destination
    assert installed.is_file(), f"Installer/RPM layout mismatch: {destination}"
    assert installed.read_bytes() == Path(source).read_bytes(), f"Installer/RPM content mismatch: {destination}"
PY
    echo "$label $architecture RPM metadata, runtime assets, and installer layout passed."
}

mkdir -p "$work"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,tmp}
for architecture in x86_64 aarch64; do
    # This exercises packaging only; the daemon is an inert fixture.
    SOURCE_DATE_EPOCH=1 ./scripts/build-bundle.sh \
        --target "$architecture-unknown-linux-musl" \
        --binary "$work/contextscroll" \
        --sbom "$work/SBOM.spdx.json" \
        --output "$work/SOURCES" >/dev/null
done

for architecture in x86_64 aarch64; do
    ./scripts/build-rpm.sh \
        --bundle "$work/SOURCES/contextscroll-v${version}-linux-${architecture}.tar.gz" \
        --output "$work/release" >"$work/build.log" 2>&1 || {
        cat "$work/build.log" >&2
        exit 1
    }
    check_package "$work/release/contextscroll-${version}-1.${architecture}.rpm" "$architecture" release

    # BuildRequires cannot be resolved by the RPM database on non-RPM CI hosts;
    # glib-compile-schemas is already required by build-rpm.sh above.
    rpmbuild -bb packaging/contextscroll-copr.spec --nodeps \
        --target "$architecture" \
        --define "_topdir $work" \
        --define "_tmppath $work/tmp" \
        --define 'dist %{nil}' >"$work/copr-build.log" 2>&1 || {
        cat "$work/copr-build.log" >&2
        exit 1
    }
    check_package "$work/RPMS/$architecture/contextscroll-${version}-1.${architecture}.rpm" "$architecture" COPR
done
