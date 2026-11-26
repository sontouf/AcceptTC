#!/usr/bin/env bash
# AcceptTC installer (Linux)
#
# Installs the test-case approval gate into a project that uses Claude Code:
# the gate tool, the tc-verifier subagent, the tc-approval skill, a hook that
# locks production code until test cases are approved, and Entire session
# capture, which the verifier reads.
#
#   ./install.sh [options] [PROJECT_DIR]        (default PROJECT_DIR: current directory)
#   curl -fsSL https://raw.githubusercontent.com/sontouf/AcceptTC/main/install.sh | bash -s -- [options] [PROJECT_DIR]
set -euo pipefail

REPO_URL="${ACCEPTTC_REPO:-https://github.com/sontouf/AcceptTC.git}"
REPO_REF="${ACCEPTTC_REF:-main}"
MIN_PY="3.8"

FRAMEWORK=""; LANG_OPT="ko"; PROJECT=""; MODE="install"
SETUP_ENTIRE=1; PUSH_SESSIONS=0; TELEMETRY="false"; FORCE_CONFIG=0; PURGE=0; PERMISSIONS=1; VSCODE=0

if [ -t 1 ]; then B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else B=""; R=""; G=""; Y=""; N=""; fi
step() { printf '%s==>%s %s%s%s\n' "$G" "$N" "$B" "$1" "$N"; }
warn() { printf '%s주의:%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '%s오류:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
AcceptTC 설치 스크립트 (Linux)

사용법:
  ./install.sh [옵션] [프로젝트_폴더]      프로젝트_폴더를 생략하면 현재 폴더에 설치합니다.

옵션:
  --framework 이름     테스트 프레임워크: gtest(기본), cunit, cmocka, unity, junit, pytest, jest, go
                       (생략하면 프로젝트를 보고 자동으로 고릅니다)
  --lang ko|en|any     근거 기록과 검증 판단을 쓸 언어 (기본 ko)
  --force-config       기존 .accepttc/config.json 을 새로 만듭니다
  --no-permissions     게이트 명령을 미리 허용하지 않습니다
                       (기본: tc_gate.py 와 entire session 조회를 묻지 않고 실행하도록 허용)
  --no-entire-setup    Entire 를 켜지 않습니다 (설치 여부만 확인)
  --push-sessions      Entire 의 기본 동작대로 git push 때 세션 로그도 올립니다
                       (기본: 올리지 않음. 공개 저장소면 대화 기록이 공개됩니다)
  --entire-telemetry   Entire 의 익명 사용 통계를 켭니다 (기본: 끔)
  --vscode             VS Code 확장도 설치합니다 (검증 현황을 VS Code 안에서 보기)
  --check              필요한 프로그램이 있는지만 확인합니다
  --uninstall          이 프로젝트에서 AcceptTC 를 제거합니다 (승인 기록은 남김)
  --purge              --uninstall 과 함께: 설정과 승인 기록까지 삭제합니다
  -h, --help           이 도움말

환경 변수:
  ACCEPTTC_REPO, ACCEPTTC_REF   curl 로 실행할 때 내려받을 저장소와 브랜치
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --framework) [ $# -ge 2 ] || die "--framework 뒤에 이름이 필요합니다."; FRAMEWORK="$2"; shift ;;
    --framework=*) FRAMEWORK="${1#*=}" ;;
    --lang) [ $# -ge 2 ] || die "--lang 뒤에 ko, en, any 중 하나가 필요합니다."; LANG_OPT="$2"; shift ;;
    --lang=*) LANG_OPT="${1#*=}" ;;
    --force-config) FORCE_CONFIG=1 ;;
    --no-permissions) PERMISSIONS=0 ;;
    --no-entire-setup) SETUP_ENTIRE=0 ;;
    --push-sessions) PUSH_SESSIONS=1 ;;
    --entire-telemetry) TELEMETRY="true" ;;
    --vscode) VSCODE=1 ;;
    --check) MODE="check" ;;
    --uninstall) MODE="uninstall" ;;
    --purge) PURGE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "알 수 없는 옵션: $1  (./install.sh --help)" ;;
    *) [ -z "$PROJECT" ] || die "프로젝트 폴더는 하나만 지정할 수 있습니다."; PROJECT="$1" ;;
  esac
  shift
done
case "$LANG_OPT" in ko|en|any) ;; *) die "--lang 은 ko, en, any 중 하나여야 합니다." ;; esac
case "$FRAMEWORK" in ""|gtest|cunit|cmocka|unity|pytest|junit|jest|go) ;;
  *) die "지원하지 않는 프레임워크: $FRAMEWORK (gtest, cunit, cmocka, unity, junit, pytest, jest, go)" ;; esac

# ---------------------------------------------------------------- prerequisites
pkg_hint() {   # install command for package $1 on this distribution
  local ids
  ids=" $( (. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}") ) "
  case "$ids" in
    *" debian "*|*" ubuntu "*) echo "sudo apt-get update && sudo apt-get install -y $1" ;;
    *" fedora "*|*" rhel "*|*" centos "*) echo "sudo dnf install -y $1" ;;
    *" arch "*) echo "sudo pacman -S --needed $1" ;;
    *" suse "*|*" opensuse "*|*" opensuse-leap "*|*" opensuse-tumbleweed "*) echo "sudo zypper install -y $1" ;;
    *" alpine "*) echo "sudo apk add $1" ;;
    *) echo "배포판의 패키지 관리자로 '$1' 패키지를 설치하세요" ;;
  esac
}

MISSING=0
need() {       # need <이름> <찾음?0/1> <설명> <설치 명령...>
  local name="$1" found="$2" detail="$3"; shift 3
  if [ "$found" = 0 ]; then
    printf '  %s[있음]%s %-8s %s\n' "$G" "$N" "$name" "$detail"
  else
    MISSING=1
    printf '  %s[없음]%s %-8s %s\n' "$R" "$N" "$name" "$detail"
    printf '         설치 방법:\n'
    for line in "$@"; do printf '           %s\n' "$line"; done
  fi
}

step "필요한 프로그램 확인"
[ "$(uname -s)" = "Linux" ] || warn "이 스크립트는 Linux 용입니다. $(uname -s) 에서는 동작을 확인하지 않았습니다."

if command -v git >/dev/null 2>&1; then need git 0 "$(git --version | head -n1)"
else need git 1 "버전 관리 도구" "$(pkg_hint git)"; fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    need python3 0 "$(python3 --version 2>&1)"
  else
    need python3 1 "$(python3 --version 2>&1) - $MIN_PY 이상이 필요합니다" "$(pkg_hint python3)"
  fi
else
  need python3 1 "Python $MIN_PY 이상" "$(pkg_hint python3)"
fi

if ! command -v entire >/dev/null 2>&1 && [ -x "$HOME/.local/bin/entire" ]; then
  need entire 1 "설치되어 있지만 PATH 에 없습니다 ($HOME/.local/bin/entire)" \
    "echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" \
    "export PATH=\"\$HOME/.local/bin:\$PATH\""
elif command -v entire >/dev/null 2>&1; then
  need entire 0 "$(entire version 2>/dev/null | head -n1)"
else
  need entire 1 "에이전트 세션 로그를 기록하는 Entire CLI" \
    "curl -fsSL https://entire.io/install.sh | bash" \
    "export PATH=\"\$HOME/.local/bin:\$PATH\"     # 설치 후 새 터미널을 열어도 됩니다" \
    "(안내: https://docs.entire.io/cli/installation)"
fi

if command -v claude >/dev/null 2>&1; then
  printf '  %s[있음]%s %-8s %s\n' "$G" "$N" "claude" "$(claude --version 2>/dev/null | head -n1)"
else
  printf '  %s[참고]%s %-8s %s\n' "$Y" "$N" "claude" "Claude Code 가 PATH 에 없습니다. 설치는 계속되지만 사용하려면 필요합니다: https://claude.com/claude-code"
fi

if [ "$MISSING" = 1 ]; then
  echo
  die "필요한 프로그램이 없습니다. 위 명령으로 설치한 뒤 이 스크립트를 다시 실행하세요."
fi
[ "$MODE" = "check" ] && { echo; echo "필요한 프로그램이 모두 있습니다."; exit 0; }

# ---------------------------------------------------------------- locate things
PROJECT="${PROJECT:-$PWD}"
[ -d "$PROJECT" ] || die "프로젝트 폴더가 없습니다: $PROJECT"
PROJECT="$(cd "$PROJECT" && pwd -P)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || echo "")"
PAYLOAD="$SCRIPT_DIR/accepttc"
if [ -z "$SCRIPT_DIR" ] || [ ! -f "$PAYLOAD/tc_gate.py" ]; then
  step "AcceptTC 내려받기 ($REPO_URL, $REPO_REF)"
  TMP_SRC="$(mktemp -d)"
  trap 'rm -rf "$TMP_SRC"' EXIT
  git clone --quiet --depth 1 --branch "$REPO_REF" "$REPO_URL" "$TMP_SRC/AcceptTC" \
    || die "저장소를 내려받지 못했습니다: $REPO_URL"
  PAYLOAD="$TMP_SRC/AcceptTC/accepttc"
  [ -f "$PAYLOAD/tc_gate.py" ] || die "내려받은 저장소에 accepttc/tc_gate.py 가 없습니다."
elif [ "$PROJECT" = "$SCRIPT_DIR" ]; then
  die "AcceptTC 저장소 자체에는 설치하지 않습니다. 대상 프로젝트 폴더를 지정하세요: ./install.sh /path/to/project"
fi

if [ "$MODE" = "uninstall" ]; then
  step "AcceptTC 제거: $PROJECT"
  if [ "$PURGE" = 1 ]; then python3 "$PAYLOAD/installer.py" uninstall "$PROJECT" --purge
  else python3 "$PAYLOAD/installer.py" uninstall "$PROJECT"; fi
  echo; echo "제거했습니다."
  exit 0
fi

if ! git -C "$PROJECT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "$PROJECT 는 git 저장소가 아닙니다. Entire 는 git 저장소에서만 동작합니다.
       먼저 저장소를 만드세요:
           cd \"$PROJECT\" && git init && git add -A && git commit -m \"initial commit\""
fi
TOP="$(cd "$(git -C "$PROJECT" rev-parse --show-toplevel)" && pwd -P)"

# ---------------------------------------------------------------- Entire
if [ "$SETUP_ENTIRE" = 1 ]; then
  step "Entire 설정"
  cd "$PROJECT"
  if entire status </dev/null 2>/dev/null | grep -q "Enabled"; then
    echo "  이미 켜져 있습니다."
  else
    entire enable --agent claude-code --telemetry="$TELEMETRY" </dev/null >/dev/null 2>&1 \
      || die "'entire enable' 이 실패했습니다. 직접 실행해 오류를 확인하세요:
           cd \"$PROJECT\" && entire enable --agent claude-code"
    echo "  Entire 를 켰습니다 (Claude Code 세션 기록)."
  fi
  if [ "$PUSH_SESSIONS" = 0 ]; then
    entire configure --local --skip-push-sessions </dev/null >/dev/null 2>&1 \
      && echo "  세션 로그는 git push 때 올리지 않도록 설정했습니다 (이 PC 에만 보관)." \
      || warn "세션 로그 업로드 설정을 바꾸지 못했습니다. 확인: entire status"
  else
    warn "git push 때 세션 로그(에이전트 대화 기록)도 함께 올라갑니다. 공개 저장소라면 누구나 볼 수 있습니다."
  fi
else
  warn "Entire 설정을 건너뛰었습니다. 검증에는 Entire 세션 기록이 필요합니다: entire enable --agent claude-code"
fi

# ---------------------------------------------------------------- files
step "AcceptTC 설치: $PROJECT"
ARGS=(install "$PROJECT" --lang "$LANG_OPT")
[ -n "$FRAMEWORK" ] && ARGS+=(--framework "$FRAMEWORK")
[ "$FORCE_CONFIG" = 1 ] && ARGS+=(--force-config)
[ "$PERMISSIONS" = 0 ] && ARGS+=(--no-permissions)
python3 "$PAYLOAD/installer.py" "${ARGS[@]}"
[ "$SETUP_ENTIRE" = 1 ] && python3 "$PAYLOAD/installer.py" entire-hooks "$PROJECT" "$TOP"

# ---------------------------------------------------------------- VS Code (optional)
if [ "$VSCODE" = 1 ]; then
  step "VS Code 확장 설치"
  VSIX="$(ls "$PAYLOAD"/../vscode-extension/accepttc-*.vsix 2>/dev/null | sort | tail -n1 || true)"
  if [ -z "$VSIX" ]; then
    warn "확장 파일(vscode-extension/accepttc-*.vsix)을 찾지 못했습니다."
  elif ! command -v code >/dev/null 2>&1; then
    warn "'code' 명령이 없습니다. VS Code 의 터미널에서 직접 설치하세요:
       code --install-extension \"$VSIX\""
  elif code --install-extension "$VSIX" --force >/dev/null 2>&1; then
    echo "  설치했습니다. 명령 팔레트에서 'AcceptTC: 검증 현황 열기' 를 실행하세요."
  else
    warn "확장을 설치하지 못했습니다. VS Code 의 터미널에서 직접 실행하세요:
       code --install-extension \"$VSIX\""
  fi
fi

# ---------------------------------------------------------------- verify
step "설치 확인"
cd "$PROJECT"
if python3 .accepttc/tc_gate.py doctor; then OK=1; else OK=0; fi

echo
if [ "$OK" = 1 ]; then
  printf '%s설치가 끝났습니다.%s\n' "$G$B" "$N"
else
  warn "설치는 했지만 위에 !! 로 표시된 항목을 고쳐야 검증이 동작합니다."
fi
cat <<EOF

다음에 할 일
  1. 설정 확인:  $PROJECT/.accepttc/config.json
       framework        테스트 프레임워크
       production_globs 테스트 케이스가 승인되기 전까지 잠기는 구현 코드
       spec_globs       근거로 인용할 수 있는 명세 문서 위치
  2. 이 폴더에서 Claude Code 를 새로 시작하세요 (훅과 검증 에이전트는 새 세션부터 적용됩니다):
       cd "$PROJECT" && claude
     처음 실행할 때 "이 폴더를 신뢰합니까?" 를 승인해야 설치한 권한 규칙이 적용됩니다.
  3. 평소처럼 테스트 작성을 시키면 됩니다. 예: "요구사항 3번의 테스트 케이스를 작성하고 검증받아줘"
  4. 검증 과정 보기:  $PROJECT/.accepttc/verification/index.html   (첫 판정이 기록되면 생깁니다)
       브라우저로 그 파일을 열거나, VS Code 확장(./install.sh --vscode)의 'AcceptTC: 검증 현황 열기' 를 쓰세요.
  5. 상태 보기:       python3 .accepttc/tc_gate.py status
EOF
[ "$TOP" != "$PROJECT" ] && warn "프로젝트가 git 저장소($TOP)의 하위 폴더입니다. Claude Code 는 반드시 $PROJECT 에서 시작하세요."
exit $((1 - OK))
