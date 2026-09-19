#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 --bundle PATH [--output DIR]" >&2
}

bundle=
output=dist
while (($#)); do
    case "$1" in
        --bundle) bundle=${2:-}; shift 2 ;;
        --output) output=${2:-}; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done

[[ -n $bundle && -f $bundle && ! -L $bundle ]] || {
    usage
    exit 2
}
for tool in rpmbuild rpm glib-compile-schemas sha256sum tar; do
    command -v "$tool" >/dev/null || {
        echo "$tool is required to build the RPM." >&2
        exit 1
    }
done

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
archive=$(basename "$bundle")
[[ $archive =~ ^contextscroll-v${version}-linux-(x86_64|aarch64)\.tar\.gz$ ]] || {
    echo "Bundle name does not match ContextScroll $version." >&2
    exit 1
}
architecture=${BASH_REMATCH[1]}
bundle=$(realpath -- "$bundle")
work=$(mktemp -d -t contextscroll-rpm.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/BUILD" "$work/BUILDROOT" "$work/RPMS" \
    "$work/SOURCES" "$work/SRPMS" "$work/tmp" "$work/verify"
install -m 0644 "$bundle" "$work/SOURCES/$archive"
tar -xzf "$bundle" -C "$work/verify"
bundle_root="$work/verify/contextscroll-v${version}-linux-${architecture}"
[[ -d $bundle_root && ! -L $bundle_root ]] || {
    echo "Bundle root is missing." >&2
    exit 1
}
(
    cd "$bundle_root"
    sha256sum --strict --check BUNDLE-MANIFEST.sha256 >/dev/null
    grep -qx "version=$version" RELEASE-METADATA
    grep -qx "target=${architecture}-unknown-linux-musl" RELEASE-METADATA
)

rpmbuild -bb packaging/contextscroll.spec \
    --target "$architecture" \
    --define "_topdir $work" \
    --define "_tmppath $work/tmp" \
    --define 'dist %{nil}' \
    --define "contextscroll_version $version"
rpm_file=$(find "$work/RPMS/$architecture" -maxdepth 1 -type f -name 'contextscroll-*.rpm' -print -quit)
[[ -n $rpm_file ]] || {
    echo "rpmbuild did not produce an RPM." >&2
    exit 1
}
[[ $(rpm -qp --qf '%{NAME} %{VERSION} %{ARCH}' "$rpm_file") == "contextscroll $version $architecture" ]] || {
    echo "Built RPM has unexpected metadata." >&2
    exit 1
}
expected_daemon_hash=$(sha256sum "$bundle_root/prebuilt/contextscroll" | cut -d' ' -f1)
rpm_daemon_hash=$(rpm -qp --dump "$rpm_file" |
    awk '$1 == "/usr/bin/contextscroll" { print $4 }')
[[ $rpm_daemon_hash == "$expected_daemon_hash" ]] || {
    echo "RPM daemon differs from the verified release bundle." >&2
    exit 1
}
mkdir -p "$output"
install -m 0644 "$rpm_file" "$output/$(basename "$rpm_file")"
sha256sum "$output/$(basename "$rpm_file")"
