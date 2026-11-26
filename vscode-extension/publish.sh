#!/usr/bin/env bash
# Publishes the extension so it can be found and installed from the Extensions
# view.  Builds the .vsix first, then uploads that same file.
#
#   VSCE_PAT=<token> ./publish.sh            VS Code Marketplace
#   OVSX_PAT=<token> ./publish.sh            Open VSX (Cursor, VSCodium, Windsurf)
#   VSCE_PAT=... OVSX_PAT=... ./publish.sh   both
#
# VSCE_PAT: Azure DevOps personal access token, scope "Marketplace > Manage",
#           for the publisher named in package.json.
# OVSX_PAT: access token from https://open-vsx.org/user-settings/tokens.
set -euo pipefail
cd "$(dirname "$0")"
if [ -z "${VSCE_PAT:-}" ] && [ -z "${OVSX_PAT:-}" ]; then
  echo "VSCE_PAT 또는 OVSX_PAT 환경 변수가 필요합니다. 이 파일 맨 위의 설명을 보세요." >&2
  exit 2
fi
./build.sh
VSIX="$(ls accepttc-*.vsix | sort | tail -n1)"
if [ -n "${VSCE_PAT:-}" ]; then
  npx --yes @vscode/vsce publish --packagePath "$VSIX"
fi
if [ -n "${OVSX_PAT:-}" ]; then
  npx --yes ovsx publish "$VSIX" --pat "$OVSX_PAT"
fi
echo "게시했습니다: $VSIX"
