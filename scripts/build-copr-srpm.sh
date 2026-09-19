#!/usr/bin/env bash
set -euo pipefail

output=${1:-.}
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

for tool in curl rpmbuild sha256sum; do
    command -v "$tool" >/dev/null || {
        echo "$tool is required to build the COPR source RPM." >&2
        exit 1
    }
done

version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' Cargo.toml | head -n1)
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Could not read a release version from Cargo.toml." >&2
    exit 1
}
spec_version=$(sed -n 's/^Version:[[:space:]]*//p' \
    packaging/contextscroll-copr.spec | head -n1)
[[ $spec_version == "$version" ]] || {
    echo "The COPR spec version ($spec_version) does not match Cargo.toml ($version)." >&2
    exit 1
}

release_url="https://github.com/oriraz1607/contextscroll/releases/download/v${version}"
work=$(mktemp -d -t contextscroll-copr-srpm.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,tmp} "$output"

for architecture in x86_64 aarch64; do
    archive="contextscroll-v${version}-linux-${architecture}.tar.gz"
    curl --fail --location --retry 3 \
        --output "$work/SOURCES/$archive" "$release_url/$archive"
done
curl --fail --location --retry 3 \
    --output "$work/SOURCES/SHA256SUMS" "$release_url/SHA256SUMS"

(
    cd "$work/SOURCES"
    grep -E "contextscroll-v${version}-linux-(x86_64|aarch64)\\.tar\\.gz$" \
        SHA256SUMS > BUNDLE-SHA256SUMS
    [[ $(wc -l < BUNDLE-SHA256SUMS) -eq 2 ]]
    sha256sum --strict --check BUNDLE-SHA256SUMS
)

install -m 0644 packaging/contextscroll-copr.spec \
    "$work/SPECS/contextscroll-copr.spec"
rpmbuild -bs "$work/SPECS/contextscroll-copr.spec" \
    --define "_topdir $work" \
    --define "_tmppath $work/tmp"

srpm=$(find "$work/SRPMS" -maxdepth 1 -type f \
    -name "contextscroll-${version}-*.src.rpm" -print -quit)
[[ -n $srpm ]] || {
    echo "rpmbuild did not produce a source RPM." >&2
    exit 1
}
install -m 0644 "$srpm" "$output/$(basename "$srpm")"
printf '%s\n' "$output/$(basename "$srpm")"
