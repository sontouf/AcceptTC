#!/usr/bin/env bash
# Builds the .vsix.  The installer and the gate tool are copied into payload/
# so the extension can set up a project by itself.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf payload accepttc-*.vsix
mkdir -p payload
cp ../install.sh payload/
cp -r ../accepttc payload/accepttc
find payload -name __pycache__ -type d -prune -exec rm -rf {} +
npx --yes @vscode/vsce package --no-dependencies
