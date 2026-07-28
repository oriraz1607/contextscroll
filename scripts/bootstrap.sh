#!/usr/bin/env bash
set -euo pipefail

repository=oriraz1607/contextscroll
repository_url="https://github.com/$repository.git"
release=v0.4.0

if [[ ${EUID} -eq 0 ]]; then
    echo "Run this bootstrap as your desktop user, not as root." >&2
    exit 1
fi

for requirement in git mktemp; do
    command -v "$requirement" >/dev/null || {
        echo "$requirement is required." >&2
        exit 1
    }
done

bootstrap_directory=$(mktemp -d -t contextscroll-install.XXXXXXXX)
checkout_directory="$bootstrap_directory/contextscroll"

cleanup() {
    rm -rf -- "$bootstrap_directory"
}
trap cleanup EXIT

echo "Downloading ContextScroll..."
git clone \
    --depth=1 \
    --single-branch \
    --branch="$release" \
    "$repository_url" \
    "$checkout_directory"

cd "$checkout_directory"
./scripts/install.sh "$@"
