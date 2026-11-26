# AcceptTC

AI 코딩 에이전트가 쓴 테스트 케이스가 **명세에 근거해서 정확하게 쓰였는지** 확인한 뒤에만 구현으로 넘어가게 하는 승인 게이트입니다. Claude Code 프로젝트에 설치해서 씁니다.

에이전트는 테스트 케이스마다 "이 입력과 기대값이 명세의 어느 문장에서 나왔는지"를 근거 기록으로 남깁니다. 별도의 검증 에이전트가 그 근거 기록, 명세 원문, 그리고 [Entire](https://entire.io)가 기록한 작성 에이전트의 동작 로그를 보고 테스트 케이스별로 승인 또는 거부합니다. 승인되지 않은 테스트 케이스가 하나라도 있으면 구현 코드는 수정할 수 없습니다.

## 동작 방식

```
테스트 + 근거 기록 작성 ──> 기계 검사 ──> 검증 에이전트 ──> 승인 ──> 구현 코드 잠금 해제
   (작성 에이전트)        (스크립트)     (tc-verifier)   │
        ^                                               └─ 거부 ──> 지적 사항을 보고 재작성
        └────────────────── 최대 3회 ────────────────────────┘   (3회 거부 시 사람에게 넘김)
```

1. **작성**: 에이전트가 테스트 파일 옆에 근거 기록(`<테스트 파일>.trace.json`)을 씁니다. 인용한 명세 문장(파일, 줄, 원문), 입력을 고른 이유, 기대값을 도출한 과정이 들어갑니다.
2. **기계 검사** (모델을 쓰지 않음): 인용문이 그 줄에 실제로 있는지, 모든 입력과 기대값에 근거가 달렸는지, Entire 로그상 테스트를 쓰기 전에 그 명세를 읽었는지 확인합니다.
3. **검증**: `tc-verifier` 서브에이전트가 명세만으로 기대값을 다시 도출해 테스트와 비교하고, 테스트 케이스별로 판정을 기록합니다.
4. **잠금**: 훅이 승인 전의 구현 코드 수정을 막습니다. 승인 뒤에 테스트나 근거 기록을 고치면 승인이 무효가 됩니다.
5. **확인**: 판정이 기록될 때마다 검증 현황 페이지가 다시 만들어집니다. 테스트 케이스별로 명세 원문과 기대값의 대응, 검증 회차별 판단 근거, 수정 전후 코드, 에이전트 동작 로그를 볼 수 있습니다.

## 설치

Linux(WSL 포함)에서 다음이 필요합니다. 없으면 설치 스크립트가 설치 명령을 알려 줍니다.

| 프로그램 | 용도 |
|---|---|
| git | Entire와 수정 전후 비교 |
| python3 (3.8 이상) | 게이트 도구 (추가 패키지 없음) |
| [Entire CLI](https://docs.entire.io/cli/installation) | 에이전트 세션 로그 기록 |
| [Claude Code](https://claude.com/claude-code) | 에이전트 실행 |

설치 방법은 세 가지이고 결과는 같습니다.

**VS Code 확장으로** (확장 하나만 있으면 됩니다)

1. VS Code 의 확장 보기(Ctrl+Shift+X)에서 **AcceptTC** 를 검색해 설치합니다. 파일로 설치하려면 [`vscode-extension/accepttc-0.4.1.vsix`](vscode-extension/accepttc-0.4.1.vsix)를 받아 `code --install-extension accepttc-0.4.1.vsix` 를 실행합니다.
2. 프로젝트 폴더를 열고 명령 팔레트(Ctrl+Shift+P)에서 `AcceptTC: 이 프로젝트에 설치`를 실행합니다.

**한 줄로**

```bash
cd /path/to/your/project
curl -fsSL https://raw.githubusercontent.com/sontouf/AcceptTC/main/install.sh | bash
```

**저장소를 받아서**

```bash
git clone https://github.com/sontouf/AcceptTC.git
cd AcceptTC
./install.sh /path/to/your/project
```

설치 스크립트가 하는 일은 다음과 같습니다.

- 필요한 프로그램 확인 (`./install.sh --check`로 확인만 할 수 있습니다)
- Entire 켜기 (`entire enable --agent claude-code`). 세션 로그는 기본으로 `git push` 때 올리지 않게 설정합니다.
- 프로젝트에 파일 설치
  - `.accepttc/`: 게이트 도구, 설정(`config.json`), 승인 기록
  - `.claude/agents/tc-verifier.md`: 검증 에이전트
  - `.claude/skills/tc-approval/`: 작성 에이전트가 따르는 절차
  - `.claude/settings.json`: 잠금 훅과 권한 규칙 추가 (기존 내용은 유지하고 `settings.json.accepttc.bak`으로 백업)
  - `CLAUDE.md`: 안내 문단 추가
- 테스트 프레임워크와 폴더 구조를 자동으로 판별해 `config.json` 작성
- 설치 점검 (`python3 .accepttc/tc_gate.py doctor`)

여러 번 실행해도 안전하고, 다시 실행하면 도구만 최신으로 바뀝니다. 설정과 승인 기록은 유지됩니다.

| 옵션 | 뜻 |
|---|---|
| `--framework 이름` | `gtest`(기본), `cunit`, `junit`, `cmocka`, `unity`, `pytest`, `jest`, `go` 중 하나로 지정. 나중에 설정 탭에서 바꿀 수 있습니다 |
| `--lang ko\|en\|any` | 근거 기록과 검증 판단을 쓸 언어 (기본 `ko`) |
| `--vscode` | VS Code 확장도 설치 |
| `--no-permissions` | 게이트 명령을 미리 허용하지 않음 |
| `--push-sessions` | Entire 기본 동작대로 세션 로그를 `git push` 때 올림 |
| `--no-entire-setup` | Entire를 켜지 않음 |
| `--uninstall` / `--purge` | 제거 / 설정과 승인 기록까지 삭제 |

## 사용

설치한 폴더에서 Claude Code를 **새로 시작**하고, 처음에 폴더 신뢰 여부를 승인합니다. 그 뒤로는 평소처럼 일을 시키면 됩니다.

```
요구사항 3번의 테스트 케이스를 작성하고 검증받아줘
```

에이전트는 `tc-approval` 스킬에 따라 테스트와 근거 기록을 쓰고, 검증 에이전트를 부르고, 거부되면 고쳐서 다시 검증받습니다. 승인 전에 구현 코드를 고치려 하면 훅이 막습니다.

### 검증 과정 보기

- **브라우저**: `.accepttc/verification/index.html`을 엽니다. HTML 파일 하나라서 서버가 필요 없습니다.
  - **추적 그래프** (첫 화면): 명세, 테스트 케이스, 구현 코드를 점으로, 근거와 호출 관계를 선으로 보여줍니다. 점을 누르면 연결된 것만 남고 옆에 내용이 나옵니다. 테스트 케이스가 인용하지 않은 요구사항 줄과, 어떤 테스트도 호출하지 않는 구현 파일은 점선으로 표시됩니다. 명세는 줄 단위/파일 단위, 구현은 함수 단위/파일 단위로 바꿔 볼 수 있습니다.
  - **테스트 케이스 / 요구사항 커버리지 / 코드 변경**: 테스트 케이스별 검증 과정, 명세 줄별 인용 현황, git 변경 이력.
  - **설정**: 테스트 프레임워크와 폴더 구조(테스트, 구현 코드, 명세 위치)를 고릅니다. 위치를 고치는 동안 그 패턴에 맞는 파일 수가 바로 표시됩니다. VS Code 확장 안에서는 저장 버튼으로 바로 반영되고, 브라우저에서는 `config.json`을 내려받아 `.accepttc/`에 넣습니다.
- **VS Code**: 확장을 설치하면 왼쪽 활동 표시줄에 AcceptTC 아이콘이 생깁니다. 확장은 확장 보기에서 AcceptTC 를 검색해 설치하거나, `code --install-extension vscode-extension/accepttc-0.4.1.vsix` 또는 `./install.sh --vscode`로 설치합니다.
  - **사이드바 목록**: 구현 코드가 잠겼는지와, 테스트 케이스를 상태별(거부, 미검증, 승인 등)로 보여줍니다. 테스트 케이스를 펼치면 테스트 코드, 인용한 명세 줄, 검증 회차별 보고서로 바로 갈 수 있습니다.
  - **코드 위 표시**: 테스트 파일을 열면 각 테스트 케이스 위에 `✓ TC-XXX-001 · 승인`처럼 상태가 나오고, 누르면 그 테스트 케이스의 검증 과정이 열립니다.
  - **상태 표시줄**: `TC 승인 수/전체 수`.
  - **검증 현황 탭**: 브라우저에서 보는 것과 같은 페이지를 편집기 탭으로 엽니다. 검증이 기록되면 자동으로 갱신됩니다.

### 명령

```bash
python3 .accepttc/tc_gate.py status       # 테스트 케이스별 승인 상태
python3 .accepttc/tc_gate.py dashboard    # 검증 현황 페이지 다시 만들기
python3 .accepttc/tc_gate.py doctor       # 설치와 설정 점검
```

사람만 쓰는 명령입니다. 에이전트가 실행하면 훅이 막으므로 터미널에서 직접 실행하거나 Claude Code 프롬프트에서 `!`를 앞에 붙입니다.

```bash
python3 .accepttc/tc_gate.py human-approve TC-CTRL-001 --by 이름 --reason "이유"   # 직접 승인
python3 .accepttc/tc_gate.py reset TC-CTRL-001                                    # 거부 횟수 초기화
```

## 설정

`.accepttc/config.json`은 사람이 고칩니다 (에이전트의 수정은 훅이 막습니다). 검증 현황의 **설정** 탭에서 고르거나 파일을 직접 고칩니다.

```json
{
  "framework": "gtest",
  "production_globs": ["src/**"],
  "spec_globs": ["docs/**", "artifacts/SA/**"],
  "coverage_files": ["docs/requirements.md"],
  "language": "ko",
  "max_attempts": 3
}
```

| 항목 | 뜻 |
|---|---|
| `framework` | 테스트 프레임워크. 아래 표 참고 |
| `test_globs` | 테스트 파일 위치. 생략하면 프레임워크 기본값 |
| `production_globs` | 테스트 케이스가 모두 승인되기 전까지 잠기는 구현 코드. 테스트 파일은 자동으로 제외됩니다 |
| `spec_globs` | 근거로 인용할 수 있는 명세 문서 위치 |
| `coverage_files` | 요구사항 커버리지 탭에 항상 보여줄 명세 파일 |
| `language` | 근거 기록과 검증 판단의 언어. `ko`이면 한국어가 아닌 설명을 거부합니다 |
| `max_attempts` | 이 횟수만큼 거부되면 자동 진행을 멈추고 사람에게 넘깁니다 |

| `framework` | 테스트 케이스 하나의 단위 | 기본 테스트 위치 |
|---|---|---|
| `gtest` | `TEST`, `TEST_F`, `TEST_P` 블록 | `tests/**/*.cpp`, `test/**/*.cpp` |
| `cunit` | `void test_xxx(void)` 함수 (`CU_ASSERT...`) | `tests/**/*.c`, `test/**/*.c` |
| `cmocka` | `void xxx(void **state)` 함수 (`assert_...`) | `tests/**/*.c`, `test/**/*.c` |
| `unity` | `void test_xxx(void)` 함수 | `tests/**/*.c`, `test/**/*.c` |
| `pytest` | `def test_xxx` 함수 (데코레이터 포함) | `tests/**/test_*.py` |
| `junit` | `@Test` 메서드 (Java, Kotlin) | `src/test/**/*.java`, `*.kt` |
| `jest` | `it(...)`, `test(...)` 호출 (Jest, Vitest, Mocha) | `**/*.test.ts`, `**/*.spec.js` 등 |
| `go` | `func TestXxx` 함수 | `**/*_test.go` |
| `custom` | `custom_framework`에 정규식으로 직접 정의 | `test_globs`로 지정 |

## 토큰과 속도

검증은 여러 검증 에이전트가 나눠서 동시에 합니다. 작성 에이전트는 테스트를 한 묶음 써 놓고 `python3 .accepttc/tc_gate.py plan`을 실행합니다. 이 명령이 판정이 필요한 테스트 케이스를 최대 4몫으로 나눠 주고(테스트 파일 단위, 파일 수가 적으면 한 파일을 나눔), 작성 에이전트는 그 수만큼 `tc-verifier`를 한 번에 띄웁니다. 동시에 띄울 수는 `plan --workers N`으로 바꿉니다. 같은 파일을 나눠 맡은 검증 에이전트들은 판정 파일을 따로 쓰고, 기록은 잠금으로 직렬화되어 서로 덮어쓰지 않습니다.

근거 기록도 짧게 쓸 수 있습니다. 여러 테스트 케이스가 함께 인용하는 명세 문장은 근거 기록 파일 맨 위의 `sources`에 한 번만 쓰고, 각 테스트 케이스는 그 `ref`만 적습니다.

이미 승인된 테스트 케이스는 건너뜁니다. 검증 에이전트는 파일을 하나씩 읽는 대신 `tc_gate.py brief` 한 번으로 필요한 것을 받습니다. 여기에는 Entire 로그에서 뽑은 작성 과정, 인용된 명세 원문, 테스트 코드와 근거 기록, 이전 판정과 그 뒤의 변경이 들어 있습니다. 기계 검사, 상태 확인, 검증 현황 페이지 생성은 모델을 쓰지 않습니다.

테스트 케이스 1개짜리 파일로 잰 검증 에이전트 1회 실행 결과입니다 (한 번씩 잰 값이라 참고용입니다).

| | 도구 호출 | 출력 토큰 | 최대 컨텍스트 | 소요 시간 |
|---|---|---|---|---|
| 브리프 도입 전 | 6회 | 약 10,500 | 약 23,800 | 109초 |
| 브리프 도입 후 | 3회 | 약 3,000 | 약 15,000 | 35초 |

더 빠르고 싸게 돌리려면 `.claude/agents/tc-verifier.md` 머리말에 `model: sonnet` 같은 줄을 넣어 검증 에이전트만 가벼운 모델로 바꿀 수 있습니다. 대신 판정의 정확도가 떨어질 수 있습니다.

## 알아둘 점

- **로그가 보여주는 범위**: 로그는 에이전트가 명세를 읽었는지, 구현 코드를 먼저 봤는지, 테스트 실행 뒤에 기대값을 고쳤는지까지 보여줍니다. 읽은 내용으로 올바르게 추론했는지는 검증 에이전트가 판단하며, 검증 에이전트도 틀릴 수 있습니다. 최종 확인은 검증 현황 페이지를 보는 사람의 몫입니다.
- **테스트는 메인 에이전트가 써야 합니다**: Entire의 실시간 로그에는 메인 에이전트의 행동만 들어 있어서, 서브에이전트나 셸 명령으로 쓴 테스트는 작성 기록이 없다는 이유로 거부됩니다.
- **우회 방지는 완벽하지 않습니다**: 훅은 파일 편집 도구와 흔한 셸 쓰기 패턴을 막습니다. 작정하고 우회하는 것까지 막지는 못하며, 그런 시도는 Entire 로그에 남습니다.
- **권한 규칙**: 설치 스크립트는 `tc_gate.py` 실행과 `entire session` 조회를 묻지 않고 실행하도록 `.claude/settings.json`에 허용 규칙을 넣습니다. 이 규칙은 Claude Code에서 그 폴더를 신뢰한다고 승인한 뒤에 적용됩니다. 원치 않으면 `--no-permissions`로 설치하세요.
- **프로젝트가 git 저장소의 하위 폴더인 경우**: Claude Code를 반드시 AcceptTC를 설치한 폴더에서 시작해야 합니다.
- **화면 언어**: 검증 현황 페이지와 검사 메시지는 한국어로만 나옵니다. `language` 설정은 에이전트가 쓰는 설명의 언어만 바꿉니다.
- **세션 로그 업로드**: `--push-sessions`를 쓰면 에이전트 대화 기록이 저장소에 함께 올라갑니다. 공개 저장소라면 누구나 볼 수 있습니다.

## 제거

```bash
./install.sh --uninstall /path/to/your/project           # 승인 기록과 설정은 남김
./install.sh --uninstall --purge /path/to/your/project   # 전부 삭제
```

Entire는 그대로 둡니다. 끄려면 프로젝트에서 `entire disable`을 실행합니다.

## 저장소 구성

```
install.sh               설치 스크립트
accepttc/                프로젝트에 설치되는 파일
  tc_gate.py               게이트 도구 (검사, 브리프, 기록, 훅, 검증 현황 페이지)
  dashboard_template.html  검증 현황 페이지의 틀
  installer.py             설치 스크립트가 쓰는 파일 처리
  claude/                  검증 에이전트, 스킬, CLAUDE.md 안내 문단
vscode-extension/        VS Code 확장 (소스와 .vsix, build.sh 로 다시 묶음)
selftest/                도구 자체 테스트
```

개발할 때는 다음으로 확인합니다.

```bash
python3 selftest/test_tc_gate.py                      # 게이트 도구 테스트
node selftest/vscode_smoke.js /path/to/your/project   # VS Code 확장 동작 확인 (VS Code 없이)
```

## 라이선스

[MIT](LICENSE)
