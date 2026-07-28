#!/usr/bin/env bash
set -euo pipefail

exec /usr/bin/zenity \
    --password \
    --title="Authenticate ContextScroll installation" \
    --text="Enter your password to install ContextScroll system files."
