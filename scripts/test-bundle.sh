#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

work=$(mktemp -d -t contextscroll-bundle-test.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT

install -m0755 /bin/true "$work/contextscroll"
printf '{"spdxVersion":"SPDX-2.3"}\n' > "$work/SBOM.spdx.json"

for run in first second; do
    SOURCE_DATE_EPOCH=1 ./scripts/build-bundle.sh \
        --target x86_64-unknown-linux-musl \
        --binary "$work/contextscroll" \
        --sbom "$work/SBOM.spdx.json" \
        --output "$work/$run" \
        >/dev/null
done

version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
archive_name="contextscroll-v${version}-linux-x86_64.tar.gz"
cmp "$work/first/$archive_name" "$work/second/$archive_name"
tar -xzf "$work/first/$archive_name" -C "$work"
bundle="$work/contextscroll-v${version}-linux-x86_64"
(
    cd "$bundle"
    sha256sum --strict --check BUNDLE-MANIFEST.sha256 >/dev/null
)

printf '\ntampered\n' >> "$bundle/README.md"
if (cd "$bundle" && sha256sum --strict --check BUNDLE-MANIFEST.sha256 >/dev/null 2>&1); then
    echo "Bundle verification accepted tampered content." >&2
    exit 1
fi
