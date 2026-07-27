#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

cargo test --all-targets --locked
python3 -m unittest discover -s tests -v
python3 -m compileall -q contextscroll bin/contextscroll-context

for script in scripts/*.sh; do
    bash -n "$script"
done
