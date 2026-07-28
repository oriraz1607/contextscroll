#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 --target RUST_TARGET --binary PATH --sbom PATH [--output DIR]" >&2
}

target=
binary=
sbom=
output=dist
while (($#)); do
    case "$1" in
        --target) target=${2:-}; shift 2 ;;
        --binary) binary=${2:-}; shift 2 ;;
        --sbom) sbom=${2:-}; shift 2 ;;
        --output) output=${2:-}; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done

[[ -n $target && -f $binary && ! -L $binary && -f $sbom && ! -L $sbom ]] || {
    usage
    exit 2
}

case "$target" in
    x86_64-unknown-linux-musl) architecture=x86_64 ;;
    aarch64-unknown-linux-musl) architecture=aarch64 ;;
    *)
        echo "Unsupported release target: $target" >&2
        exit 1
        ;;
esac

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Could not read a release version from Cargo.toml." >&2
    exit 1
}

bundle_name="contextscroll-v${version}-linux-${architecture}"
work=$(mktemp -d -t contextscroll-bundle.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
bundle="$work/$bundle_name"
mkdir -p "$bundle/prebuilt"

install -m0755 "$binary" "$bundle/prebuilt/contextscroll"
for path in \
    bin config contextscroll gnome-extension scripts systemd udev \
    Cargo.lock Cargo.toml LICENSE README.md SECURITY.md; do
    cp -a -- "$path" "$bundle/"
done
find "$bundle" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$bundle" -type f -name '*.pyc' -delete
install -m0644 "$sbom" "$bundle/SBOM.spdx.json"

cat > "$bundle/RELEASE-METADATA" <<EOF
version=$version
target=$target
commit=${GITHUB_SHA:-unknown}
source=https://github.com/oriraz1607/contextscroll
EOF

bundle_manifest="$work/BUNDLE-MANIFEST.sha256"
(
    cd "$bundle"
    find . -type f -print |
        LC_ALL=C sort |
        while IFS= read -r file; do
            sha256sum "$file"
        done > "$bundle_manifest"
)
install -m0644 "$bundle_manifest" "$bundle/BUNDLE-MANIFEST.sha256"

mkdir -p "$output"
epoch=${SOURCE_DATE_EPOCH:-0}
tar \
    --sort=name \
    --mtime="@$epoch" \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    --format=posix \
    --pax-option=delete=atime,delete=ctime \
    -C "$work" \
    -cf - \
    "$bundle_name" |
    gzip -n > "$output/$bundle_name.tar.gz"
sha256sum "$output/$bundle_name.tar.gz"
