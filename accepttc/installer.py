#!/usr/bin/env python3
"""File-level part of the AcceptTC installer (install.sh drives it).

  installer.py install   <project> [--framework F] [--lang L] [--force-config] [--no-permissions]
  installer.py entire-hooks <project> <git-toplevel>
  installer.py uninstall <project> [--purge]
  installer.py detect    <project>

Only the Python standard library is used.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PAYLOAD = Path(__file__).resolve().parent
STATE = ".accepttc"
TOOL_FILES = ("tc_gate.py", "dashboard_template.html")
HOOK_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"
# Fails open if the tool or python3 is missing, so a half-removed install can
# never block every edit in the project.
HOOK_COMMAND = ("sh -c 'f=\"$CLAUDE_PROJECT_DIR/.accepttc/tc_gate.py\"; "
                "if [ ! -f \"$f\" ] || ! command -v python3 >/dev/null 2>&1; then exit 0; fi; "
                "exec python3 \"$f\" hook'")
# Commands the author and the verifier run all the time; allowing them avoids a
# permission prompt on every step.  `record` is still limited to the verifier,
# and `human-approve`/`reset` are still refused, by the hook.
ALLOW_RULES = ("Bash(python3 .accepttc/tc_gate.py:*)", "Bash(entire session list:*)",
               "Bash(entire session info:*)", "Edit(/.accepttc/inbox/**)")
SNIPPET_RE = re.compile(r"\n*<!-- accepttc:start -->.*?<!-- accepttc:end -->\n?", re.S)
SKIP = {".git", ".accepttc", ".entire", ".claude", "node_modules", ".venv", "venv", "__pycache__",
        "build", "dist", "target", ".gradle", ".idea", ".tox"}
SPEC_DIRS = ("docs", "doc", "spec", "specs", "requirements", "artifacts/SA", "artifacts/SD")


def say(msg):
    print("  " + msg)


# ---------------------------------------------------------------- detection
def walk(project, limit=20000):
    """Repo-relative posix paths of project files (bounded, junk dirs skipped)."""
    out, stack = [], [project]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir() and not e.is_symlink():
                if e.name not in SKIP:
                    stack.append(e)
            elif e.is_file():
                out.append(e.relative_to(project).as_posix())
    return out


def read(project, name):
    try:
        return (project / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect(project):
    """Best guess at the project's test framework and layout."""
    files = walk(project)
    names = {f.rsplit("/", 1)[-1] for f in files}
    has = lambda pat: any(re.search(pat, f) for f in files)      # noqa: E731
    pkg = read(project, "package.json")

    if has(r"_test\.go$") or "go.mod" in names:
        fw, prod = "go", ["**/*.go"]
    elif has(r"^src/test/.*\.(java|kt)$") or names & {"pom.xml", "build.gradle", "build.gradle.kts"}:
        fw, prod = "junit", ["src/main/**"]
    elif has(r"\.(test|spec)\.[jt]sx?$") or re.search(r'"(jest|vitest|mocha)"', pkg):
        fw, prod = "jest", ["src/**"] if has(r"^src/") else ["**/*.js", "**/*.ts"]
    elif has(r"(^|/)(test_\w+|\w+_test)\.py$") or names & {"pytest.ini", "conftest.py", "pyproject.toml",
                                                          "setup.py", "tox.ini"}:
        pkgs = sorted({f.split("/", 1)[0] for f in files
                       if re.fullmatch(r"[^/]+/__init__\.py", f) and not f.startswith("test")})
        fw = "pytest"
        prod = ["src/**"] if has(r"^src/") else [p + "/**" for p in pkgs] or ["src/**"]
    elif any("unity.h" in read(project, f) for f in files if re.search(r"^tests?/.*\.c$", f)):
        fw, prod = "unity", ["src/**"]
    else:
        fw, prod = "gtest", ["src/**"]

    coverage = [f for f in files
                if f.split("/", 1)[0] in ("docs", "doc", "spec", "specs", "requirements")
                and re.search(r"(require|spec|srs)", f.rsplit("/", 1)[-1], re.I)
                and f.endswith((".md", ".txt", ".rst"))][:5]
    return {"framework": fw, "production_globs": prod,
            "spec_globs": [d + "/**" for d in SPEC_DIRS], "coverage_files": coverage}


# ---------------------------------------------------------------- install
def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file() and dst.read_bytes() == src.read_bytes():
        return "그대로"
    existed = dst.exists()
    shutil.copyfile(src, dst)
    return "갱신" if existed else "설치"


def load_settings(path):
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit("error: %s is not valid JSON (%s). Fix it and run the installer again." % (path, e))
    if not isinstance(data, dict):
        sys.exit("error: %s must contain a JSON object." % path)
    return data


def save_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".accepttc.bak")
    # keep one copy of the settings as they were before AcceptTC touched them
    if path.is_file() and not backup.exists() and "tc_gate.py" not in path.read_text(encoding="utf-8"):
        shutil.copyfile(path, backup)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def commands(entry):
    return [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]


def add_gate_hook(project):
    path = project / ".claude" / "settings.json"
    data = load_settings(path)
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    mine = [e for e in pre if isinstance(e, dict) and any("tc_gate.py" in c for c in commands(e))]
    wanted = {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 30}]}
    if mine == [wanted]:
        return "그대로"
    data["hooks"]["PreToolUse"] = [e for e in pre if e not in mine] + [wanted]
    save_settings(path, data)
    return "갱신" if mine else "설치"


def add_permissions(project):
    path = project / ".claude" / "settings.json"
    data = load_settings(path)
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    new = [r for r in ALLOW_RULES if r not in allow]
    if not new:
        return "그대로"
    allow.extend(new)
    save_settings(path, data)
    return "추가"


def remove_permissions(project):
    path = project / ".claude" / "settings.json"
    if not path.is_file():
        return False
    data = load_settings(path)
    allow = data.get("permissions", {}).get("allow", [])
    kept = [r for r in allow if r not in ALLOW_RULES]
    if len(kept) == len(allow):
        return False
    data["permissions"]["allow"] = kept
    if not kept:
        del data["permissions"]["allow"]
        if not data["permissions"]:
            del data["permissions"]
    save_settings(path, data)
    return True


def remove_gate_hook(project):
    path = project / ".claude" / "settings.json"
    if not path.is_file():
        return False
    data = load_settings(path)
    pre = data.get("hooks", {}).get("PreToolUse", [])
    kept = [e for e in pre if not (isinstance(e, dict) and any("tc_gate.py" in c for c in commands(e)))]
    if len(kept) == len(pre):
        return False
    data["hooks"]["PreToolUse"] = kept
    if not kept:
        del data["hooks"]["PreToolUse"]
    save_settings(path, data)
    return True


def add_snippet(project):
    snippet = (PAYLOAD / "claude" / "CLAUDE.snippet.md").read_text(encoding="utf-8").strip()
    path = project / "CLAUDE.md"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    base = SNIPPET_RE.sub("\n", old).rstrip()
    new = (base + "\n\n" if base else "") + snippet + "\n"
    if new == old:
        return "그대로"
    path.write_text(new, encoding="utf-8")
    return "갱신" if old else "생성"


def cmd_install(args):
    project = Path(args.project).resolve()
    state = project / STATE
    for name in TOOL_FILES:
        say("[%s] %s/%s" % (copy(PAYLOAD / name, state / name), STATE, name))
    for src in sorted((PAYLOAD / "claude").rglob("*")):
        relp = src.relative_to(PAYLOAD / "claude")
        if src.is_file() and relp.parts[0] in ("agents", "skills"):
            say("[%s] .claude/%s" % (copy(src, project / ".claude" / relp), relp.as_posix()))

    cfg_path = state / "config.json"
    if cfg_path.is_file() and not args.force_config:
        say("[그대로] %s/config.json (기존 설정을 유지했습니다)" % STATE)
    else:
        cfg = detect(project)
        if args.framework:
            cfg["framework"] = args.framework
        cfg.update(language=args.lang, max_attempts=3)
        ordered = {k: cfg[k] for k in ("framework", "production_globs", "spec_globs", "coverage_files",
                                       "language", "max_attempts")}
        cfg_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        say("[생성] %s/config.json  (framework: %s%s)"
            % (STATE, cfg["framework"], "" if args.framework else ", 자동 판별"))

    say("[%s] .claude/settings.json 의 게이트 훅 (승인 전 구현 코드 잠금)" % add_gate_hook(project))
    if args.no_permissions:
        say("[건너뜀] 권한 규칙 (게이트 명령을 실행할 때마다 허용 여부를 묻습니다)")
    else:
        say("[%s] .claude/settings.json 의 권한 규칙: %s"
            % (add_permissions(project), ", ".join(ALLOW_RULES)))
    ignore = state / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("inbox/\n", encoding="utf-8")
    say("[%s] CLAUDE.md 의 AcceptTC 안내 문단" % add_snippet(project))
    if (project / "AGENTS.md").is_file() and "@AGENTS.md" not in read(project, "CLAUDE.md"):
        say("[참고] Claude Code 는 AGENTS.md 가 아니라 CLAUDE.md 를 읽습니다. AGENTS.md 에 프로젝트 "
            "지침이 있다면 CLAUDE.md 에 `@AGENTS.md` 한 줄을 추가하세요.")
    return 0


def cmd_entire_hooks(args):
    """When the project is a subdirectory of the git repository, Entire installs
    its Claude Code hooks at the repository root.  Claude Code started in the
    project directory reads that directory's settings, so copy them down."""
    project, top = Path(args.project).resolve(), Path(args.toplevel).resolve()
    if project == top:
        return 0
    src = load_settings(top / ".claude" / "settings.json").get("hooks", {})
    path = project / ".claude" / "settings.json"
    data = load_settings(path)
    hooks, added = data.setdefault("hooks", {}), 0
    for event, entries in src.items():
        for e in entries:
            if isinstance(e, dict) and any("entire hooks" in c for c in commands(e)):
                if e not in hooks.setdefault(event, []):
                    hooks[event].append(e)
                    added += 1
    if added:
        save_settings(path, data)
    say("[%s] %s/.claude/settings.json 의 Entire 훅 (프로젝트가 git 저장소의 하위 폴더라서 복사)"
        % ("복사" if added else "그대로", project.name))
    return 0


def cmd_uninstall(args):
    project = Path(args.project).resolve()
    if remove_gate_hook(project):
        say("[삭제] .claude/settings.json 의 게이트 훅")
    if remove_permissions(project):
        say("[삭제] .claude/settings.json 의 권한 규칙")
    for relp in (".claude/agents/tc-verifier.md", ".claude/skills/tc-approval"):
        target = project / relp
        if target.is_dir():
            shutil.rmtree(target)
            say("[삭제] " + relp)
        elif target.is_file():
            target.unlink()
            say("[삭제] " + relp)
    md = project / "CLAUDE.md"
    if md.is_file() and SNIPPET_RE.search(md.read_text(encoding="utf-8")):
        text = SNIPPET_RE.sub("\n", md.read_text(encoding="utf-8")).strip()
        if text:
            md.write_text(text + "\n", encoding="utf-8")
        else:
            md.unlink()
        say("[삭제] CLAUDE.md 의 AcceptTC 안내 문단")
    state = project / STATE
    if args.purge and state.is_dir():
        shutil.rmtree(state)
        say("[삭제] %s/ (도구, 설정, 승인 기록 전부)" % STATE)
    else:
        for name in TOOL_FILES:
            if (state / name).is_file():
                (state / name).unlink()
                say("[삭제] %s/%s" % (STATE, name))
        if state.is_dir():
            say("[유지] %s/ 의 설정과 승인 기록 (지우려면 --purge)" % STATE)
    say("[참고] Entire 는 그대로 두었습니다. 끄려면: entire disable")
    return 0


def cmd_detect(args):
    print(json.dumps(detect(Path(args.project).resolve()), indent=2, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("project")
    p.add_argument("--framework")
    p.add_argument("--lang", default="ko")
    p.add_argument("--force-config", action="store_true")
    p.add_argument("--no-permissions", action="store_true")
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("entire-hooks")
    p.add_argument("project")
    p.add_argument("toplevel")
    p.set_defaults(fn=cmd_entire_hooks)
    p = sub.add_parser("uninstall")
    p.add_argument("project")
    p.add_argument("--purge", action="store_true")
    p.set_defaults(fn=cmd_uninstall)
    p = sub.add_parser("detect")
    p.add_argument("project")
    p.set_defaults(fn=cmd_detect)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
