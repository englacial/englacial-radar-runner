#!/bin/bash
# Build (or bring up to date) the Python environment of this repository from
# uv.lock, using only the wheels stored in wheelhouse/. Needs no internet and
# no root. Safe to run again: the environment ends up with exactly the locked
# packages. To start from nothing, delete .venv first.
#
#   setup/build_venv.sh            # builds <repository>/.venv
#   setup/build_venv.sh DIR        # builds the environment in DIR instead
#
# After a change to uv.lock run setup/fetch_wheels.py first (needs internet).
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENV=${1:-$REPO/.venv}
PYTHON=/usr/bin/python3
UV=${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}

die() { echo "build_venv: ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as the user that owns the checkout, not as root"
[ -x "$UV" ] || die "uv not found (expected in ~/.local/bin; see setup/SYSTEM_CHANGES.md)"
[ -f "$REPO/uv.lock" ] || die "$REPO/uv.lock is missing"
[ -d "$REPO/wheelhouse" ] || die "$REPO/wheelhouse is missing: run setup/fetch_wheels.py (needs internet)"

requirements=$(mktemp)
trap 'rm -f "$requirements"' EXIT

# The lock file as a requirements list with hashes (reads uv.lock only).
"$UV" export --project "$REPO" --frozen --offline --all-groups --no-emit-project \
    --format requirements-txt --quiet --output-file "$requirements"

if [ ! -x "$VENV/bin/python" ]; then
    "$UV" venv --offline --python "$PYTHON" "$VENV"
fi

# --no-index --find-links: wheels come from wheelhouse/ and nowhere else.
# --require-hashes: each wheel must match the hash recorded in uv.lock.
# --no-build: never compile from source.
"$UV" pip sync --offline --no-index --no-build --find-links "$REPO/wheelhouse" \
    --require-hashes --python "$VENV/bin/python" "$requirements"

echo "build_venv: $VENV matches uv.lock ($("$VENV/bin/python" --version))"
