#!/usr/bin/env bash
# Run the full ORCHE test suite (stdlib unittest, no network).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -v "$@"
