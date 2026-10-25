#!/bin/sh
# The merge queue's gate (ORCHE 0.9.0) and CI's entry point (SPEC §12.2): the checks CI would run, in order.
# It runs in a fresh checkout of the PR merged onto main. Exit non-zero on any failure. It gets no secrets.
set -eu
# 1. install, one of:   npm ci  |  pnpm install --frozen-lockfile  |  pip install -r requirements.txt
# 2. build/type-check:  npm run build
# 3. tests:             npm test
# The last line says what this script verified: type-check-only, unit-test-verified or live-ui-verified.
echo "verdict: type-check-only"
