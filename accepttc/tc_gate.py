#!/usr/bin/env python3
"""tc_gate - traceability gate for TDD test cases.

A test case (one GoogleTest TEST block) may only be used to drive production
code after an independent verifier has approved it.  This tool supplies the
deterministic half of that gate:

  check     mechanical checks on a test file and its trace record
  brief     everything the verifier needs for a test file, in one output
  evidence  what the agent session log shows about how the tests were written
  record    store the verifier's verdicts (verifier subagent only)
  status    approval state of every test case
  dashboard write the web page that shows how each test case was verified
  hook      PreToolUse hook: blocks production-code edits until tests are approved
  human-approve / reset   human overrides (run by the user, never by an agent)

The judgement half (does the oracle really follow from the spec?) is done by
the `tc-verifier` subagent, see .claude/agents/tc-verifier.md.

Only the Python standard library is used.
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# The tool is installed at <project>/.accepttc/tc_gate.py; the project is its parent.
ROOT = Path(os.environ.get("TC_GATE_ROOT") or Path(__file__).resolve().parent.parent).resolve()

STATE_DIR = ".accepttc"                     # tool, config and approval records
CONFIG_FILE = STATE_DIR + "/config.json"
REGISTRY = STATE_DIR + "/registry.json"
REPORT_DIR = STATE_DIR + "/verification"
DASHBOARD = REPORT_DIR + "/index.html"
INBOX = STATE_DIR + "/inbox"                # where the verifier drops its verdict files
ENTIRE = (os.environ.get("TC_GATE_ENTIRE") or shutil.which("entire")
          or str(Path.home() / ".local" / "bin" / "entire"))
VERIFIER_AGENT = "tc-verifier"
ID_RE = re.compile(r"^TC-[A-Z0-9]+-\d{3,}$")
MIN_QUOTE_CHARS = 12
DEFAULT_READ_LINES = 2000                   # Read tool default window
SKIP_DIRS = {".git", ".accepttc", ".entire", ".claude", "node_modules", ".venv", "venv",
             "__pycache__", "build", "dist", "target", ".gradle", ".idea", ".tox"}

# How a test case looks in each supported framework.
#   head   regex matching the start of one test case (named groups feed `name`)
#   block  how the body is delimited: brace { }, paren ( ), or indent
#   assert regex counting assertions inside the body
#   globs  default test file locations
FRAMEWORKS = {
    "gtest": {
        "head": r"^[ \t]*(?:TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*\(\s*(?P<a>\w+)\s*,\s*(?P<b>\w+)\s*\)",
        "name": "{a}.{b}", "block": "brace",
        "assert": r"\b(?:EXPECT|ASSERT)_[A-Z_]+\s*\(",
        "globs": ["tests/**/*.cpp", "tests/**/*.cc", "test/**/*.cpp", "test/**/*.cc"]},
    "unity": {
        "head": r"^[ \t]*(?:static\s+)?void\s+(?P<a>test_?\w+)\s*\(\s*void\s*\)",
        "name": "{a}", "block": "brace",
        "assert": r"\bTEST_(?:ASSERT|FAIL)\w*\s*\(",
        "globs": ["tests/**/*.c", "test/**/*.c"]},
    "pytest": {
        "head": r"^(?P<indent>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<a>test\w*)[ \t]*\(",
        "name": "{cls}{a}", "block": "indent",
        "assert": r"^[ \t]*assert\b|\bpytest\.(?:raises|warns|approx)\b|\bself\.assert\w+\s*\(",
        "globs": ["tests/**/test_*.py", "tests/**/*_test.py", "test/**/test_*.py", "test_*.py"]},
    "junit": {
        "head": (r"^[ \t]*@(?:Test|ParameterizedTest|RepeatedTest)\b(?:\([^)\n]*\))?\s*"
                 r"(?:@[\w.]+(?:\([^)\n]*\))?\s*)*"
                 r"(?:(?:public|protected|private|internal|static|final|open|suspend)\s+)*"
                 r"(?:fun\s+|[\w<>\[\],.? ]+?\s+)(?P<a>`[^`\n]+`|\w+)(?=\s*\()"),
        "name": "{cls}{a}", "block": "brace",
        "assert": r"\b(?:assert\w*|verify|fail)\s*\(|\bassertThat\b|\.should\w*\b",
        "globs": ["src/test/**/*.java", "src/test/**/*.kt"]},
    "jest": {
        "head": (r"^[ \t]*(?:it|test)(?:\.(?:only|skip|concurrent|failing))?\s*\(\s*"
                 r"(?P<q>['\"`])(?P<a>(?:\\.|(?!(?P=q)).)*)(?P=q)"),
        "name": "{a}", "block": "paren",
        "assert": r"\bexpect\s*\(|\bassert(?:\.\w+)?\s*\(",
        "globs": ["**/*.test.js", "**/*.test.ts", "**/*.test.jsx", "**/*.test.tsx",
                  "**/*.spec.js", "**/*.spec.ts"]},
    "go": {
        "head": r"^func[ \t]+(?P<a>Test\w+)(?=[ \t]*\()",
        "name": "{a}", "block": "brace",
        "assert": r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|\b(?:assert|require)\.\w+\s*\(",
        "globs": ["**/*_test.go"]},
}

DEFAULTS = {
    "framework": "gtest",
    "test_globs": None,                     # default: the framework's own globs
    "spec_globs": ["docs/**", "artifacts/SA/**", "artifacts/SD/**"],   # citable sources
    "production_globs": ["src/**"],         # locked until every test case is approved
    "coverage_files": [],                   # spec files always listed on the coverage tab
    "language": "ko",                       # language of explanations: ko | en | any
    "max_attempts": 3,                      # rejections before a test case is escalated
    "custom_framework": None,               # {"head","name","block","assert"} when framework=custom
}
CONFIG_ERROR = None


def load_config():
    global CONFIG_ERROR
    cfg = dict(DEFAULTS)
    path = ROOT / CONFIG_FILE
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(user, dict):
                raise ValueError("top level must be an object")
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS and v is not None})
        except ValueError as e:
            CONFIG_ERROR = "%s 을(를) 읽지 못했습니다: %s" % (CONFIG_FILE, e)
    fw = cfg["custom_framework"] if cfg["framework"] == "custom" else FRAMEWORKS.get(cfg["framework"])
    try:
        if not isinstance(fw, dict):
            raise ValueError("framework \"%s\" 은(는) 지원하지 않습니다 (%s, custom)"
                             % (cfg["framework"], ", ".join(sorted(FRAMEWORKS))))
        re.compile(fw["head"], re.M), re.compile(fw["assert"], re.M)
        if fw.get("block", "brace") not in ("brace", "paren", "indent"):
            raise ValueError("block must be brace, paren or indent")
        cfg["max_attempts"] = max(1, int(cfg["max_attempts"]))
    except (ValueError, KeyError, TypeError, re.error) as e:
        CONFIG_ERROR = CONFIG_ERROR or "%s 설정 오류: %s" % (CONFIG_FILE, e)
        fw, cfg["max_attempts"] = FRAMEWORKS["gtest"], 3
    cfg["fw"] = fw
    if not cfg.get("test_globs"):
        cfg["test_globs"] = list(fw.get("globs") or [])
    return cfg


def glob_re(glob, shell=False):
    """Regex source for a path glob (** crosses directories).  With shell=True
    the result is safe to search for inside a shell command line."""
    any_path, any_name, one = ((r"[\w./-]*", r"[\w.-]*", r"[\w.-]") if shell
                               else (r".*", r"[^/]*", r"[^/]"))
    dirs = r"(?:[\w.-]+/)*" if shell else r"(?:[^/]+/)*"
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + dirs, i + 3
        elif glob.startswith("**", i):
            out, i = out + any_path, i + 2
        elif glob[i] == "*":
            out, i = out + any_name, i + 1
        elif glob[i] == "?":
            out, i = out + one, i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return out


CFG = load_config()
MAX_ATTEMPTS = CFG["max_attempts"]
_TEST_RE = re.compile("|".join("(?:%s)" % glob_re(g) for g in CFG["test_globs"]) or r"(?!)")
_SPEC_RE = re.compile("|".join("(?:%s)" % glob_re(g) for g in CFG["spec_globs"]) or r"(?!)")
_PROD_RE = re.compile("|".join("(?:%s)" % glob_re(g) for g in CFG["production_globs"]) or r"(?!)")


def is_test(relpath):
    return bool(relpath) and (bool(_TEST_RE.fullmatch(relpath)) or relpath.endswith(".trace.json"))


def is_spec(relpath):
    return bool(relpath) and bool(_SPEC_RE.fullmatch(relpath))


def is_prod(relpath):
    """Production code: locked until every test case is approved."""
    return bool(relpath) and bool(_PROD_RE.fullmatch(relpath)) and not is_test(relpath)


APPROVED, REJECTED, ESCALATED = "APPROVED", "REJECTED", "ESCALATED"
UNVERIFIED, STALE = "UNVERIFIED", "STALE"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def squash(text):
    """Whitespace-insensitive form used for hashing and quote matching."""
    return re.sub(r"\s+", " ", text).strip()


def korean(text):
    """Is this explanation in the configured language?  Only Korean can be
    checked mechanically; quotes and code may be mixed in."""
    if CFG.get("language") != "ko":
        return bool(str(text or "").strip())
    return len(re.findall(r"[\uac00-\ud7a3]", str(text or ""))) >= 4


def rel(path):
    """Repo-relative posix path, or None when the path is outside the repo."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return Path(os.path.normpath(p)).relative_to(ROOT).as_posix()
    except ValueError:
        return None


def under(relpath, roots):
    return relpath is not None and any(relpath.startswith(r) for r in roots)


def git_top():
    top = (_git("rev-parse", "--show-toplevel") or "").strip()
    return os.path.realpath(top) if top else str(ROOT)


# --------------------------------------------------------------------------
# Test files and trace records
# --------------------------------------------------------------------------
def _skip_string(text, i):
    """i is at a quote character; returns the index just past the literal."""
    q, n = text[i], len(text)
    if text.startswith(q * 3, i):
        j = text.find(q * 3, i + 3)
        return n if j < 0 else j + 3
    i += 1
    while i < n and text[i] != q:
        if text[i] == "\n" and q != "`":
            return i                       # unterminated on this line: not a literal
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _match(text, i, opener, closer):
    """i is at `opener`; index just past its matching `closer`, skipping
    comments and string literals.  None if unbalanced."""
    depth, n = 0, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if two == 'R"':
            m = re.match(r'R"([^()\\ ]{0,16})\(', text[i:])
            if m:
                close = ")" + m.group(1) + '"'
                j = text.find(close, i + m.end())
                i = n if j < 0 else j + len(close)
                continue
        if c in "\"'`":
            i = _skip_string(text, i)
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _brace_end(text, i):
    """End of the { } body that follows a test header ending at i."""
    limit = min(len(text), i + 600)
    while i < limit:
        c = text[i]
        if c == "{":
            return _match(text, i, "{", "}")
        if c == "(":
            i = _match(text, i, "(", ")") or limit
            continue
        if c in ";=":
            return None                    # declaration or expression body
        i += 1
    return None


def _indent_block(text, m):
    """(start, end) of an indentation-delimited test, decorators included."""
    indent = len(m.group("indent").expandtabs(4))
    start = m.start()
    while start > 0:                        # pull in decorators directly above
        prev = text.rfind("\n", 0, start - 1) + 1
        line = text[prev:start - 1]
        if not line.strip():
            break
        ind = len(line[:len(line) - len(line.lstrip())].expandtabs(4))
        if ind == indent and line.lstrip().startswith("@") or ind > indent or (
                ind == indent and line.lstrip()[:1] in ")]}"):
            start = prev
        else:
            break
    if not text[start:m.start()].lstrip().startswith("@"):
        start = m.start()
    end = text.find("\n", m.end())
    end = len(text) if end < 0 else end + 1
    last = end
    while end < len(text):
        nxt = text.find("\n", end)
        nxt = len(text) if nxt < 0 else nxt + 1
        line = text[end:nxt]
        if line.strip():
            if len(line[:len(line) - len(line.lstrip())].expandtabs(4)) <= indent:
                break
            last = nxt
        end = nxt
    return start, last


def _enclosing_class(text, m, fw):
    if "{cls}" not in fw["name"]:
        return ""
    best = ""
    indent = len(m.groupdict().get("indent") or "") if fw.get("block") == "indent" else None
    for c in re.finditer(r"^([ \t]*)(?:[\w]+[ \t]+)*class[ \t]+(\w+)", text[:m.start()], re.M):
        if indent is None or len(c.group(1)) < indent:
            best = c.group(2) + "."
    return best if indent is None or indent > 0 else ""


def parse_tests(text):
    """Return ([{name, body, asserts, line}], context) for a test file.

    `context` is the file with every test block removed (fixtures, helpers,
    imports).  It is hashed into every test case, so changing a fixture
    invalidates the approvals of the tests that depend on it.
    """
    fw = CFG["fw"]
    head = re.compile(fw["head"], re.M)
    asserts = re.compile(fw["assert"], re.M)
    kind = fw.get("block", "brace")
    tests, pieces, pos = [], [], 0
    for m in head.finditer(text):
        if m.start() < pos:
            continue
        start = m.start()
        if kind == "indent":
            start, end = _indent_block(text, m)
            if start < pos:
                start = m.start()
        elif kind == "paren":
            end = _match(text, text.index("(", m.start()), "(", ")")
            if end is not None and text[end:end + 1] == ";":
                end += 1
        else:
            end = _brace_end(text, m.end())
        if end is None:
            continue
        block = text[start:end]
        groups = {k: (v or "") for k, v in m.groupdict().items()}
        groups["cls"] = _enclosing_class(text, m, fw)
        tests.append({
            "name": fw["name"].format(**groups).strip("`"),
            "body": block,
            "asserts": len(asserts.findall(block)),
            "line": text.count("\n", 0, m.start()) + 1,
        })
        pieces.append(text[pos:start])
        pos = end
    pieces.append(text[pos:])
    return tests, "".join(pieces)


def trace_path(test_rel):
    return str(Path(test_rel).with_suffix("")) + ".trace.json"


_TEST_FILES = None


def all_test_files():
    """Files matching test_globs that contain at least one test case."""
    global _TEST_FILES
    if _TEST_FILES is None:
        head, out = re.compile(CFG["fw"]["head"], re.M), []
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                relp = (Path(dirpath) / name).relative_to(ROOT).as_posix()
                if _TEST_RE.fullmatch(relp):
                    try:
                        text = (ROOT / relp).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if head.search(text):
                        out.append(relp)
        _TEST_FILES = out
    return _TEST_FILES


def load_trace(test_rel):
    """Return (cases, error).  cases is a list of dicts."""
    tp = ROOT / trace_path(test_rel)
    if not tp.is_file():
        return [], "근거 기록 %s 파일이 없습니다" % trace_path(test_rel)
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
    except ValueError as e:
        return [], "근거 기록이 올바른 JSON이 아닙니다: %s" % e
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        return [], '근거 기록은 "cases" 목록을 가진 객체여야 합니다'
    return [c for c in cases if isinstance(c, dict)], None


def case_hash(test, context, case):
    """Hash of everything an approval covers: body, fixture context, trace."""
    return sha("\n".join([
        squash(test["body"]), squash(context),
        json.dumps(case, sort_keys=True, ensure_ascii=False)]))


def parse_lines(spec):
    """'12' or '12-15' -> (12, 15); None if malformed."""
    m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(spec))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return (a, b) if 1 <= a <= b else None


# --------------------------------------------------------------------------
# Agent session log, read through the Entire CLI
# --------------------------------------------------------------------------
LOG = {"source": "", "sessions": [], "error": None}


def _entire(*args):
    r = subprocess.run([ENTIRE, *args], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    if r.returncode != 0:
        raise RuntimeError("`entire %s` failed: %s"
                           % (" ".join(args), (r.stderr or r.stdout).strip()[:200]))
    return r.stdout


def transcripts():
    """[(session_id, jsonl_text)] for every session Entire tracks in this worktree.

    `entire session info <id> --transcript` streams the live transcript of the
    session's main agent, so work done earlier in the current turn is included.
    Subagent transcripts are not part of that stream: a verifier's own reading
    can never count as the author's evidence, and tests must be written by the
    main agent for their history to be visible.
    """
    fixture = os.environ.get("TC_GATE_TRANSCRIPT_DIR")       # self-test seam
    if fixture:
        LOG["source"] = "fixture " + fixture
        return [(q.stem, q.read_text(encoding="utf-8", errors="replace"))
                for q in sorted(Path(fixture).glob("*.jsonl"))]
    LOG["source"] = "entire session info <session> --transcript"
    out, roots = [], {str(ROOT), git_top()}
    for sess in json.loads(_entire("session", "list", "--json") or "[]"):
        if os.path.realpath(sess.get("worktree_path") or "") not in roots:
            continue
        sid = sess["session_id"]
        out.append((sid, _entire("session", "info", sid, "--transcript")))
    return out


TEST_RUN_RE = re.compile(r"\b(ctest|gtest|--gtest_\w+|make\s+test|cmake\s+--build)\b")


def load_events():
    """Chronological tool-call events of the main agent, from Entire."""
    events, seen, failed, said = [], set(), set(), set()
    LOG.update(sessions=[], error=None)
    try:
        logs = transcripts()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        LOG["error"] = str(e)
        return []
    for sid, text in logs:
        LOG["sessions"].append(sid)
        for line in text.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            msg = o.get("message") if isinstance(o, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list) or o.get("isSidechain"):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result" and c.get("is_error"):
                    failed.add(c.get("tool_use_id"))
                if c.get("type") == "text" and o.get("type") == "assistant" and squash(c.get("text") or ""):
                    key = (o.get("uuid"), len(events))
                    if o.get("uuid") not in said:
                        said.add(o.get("uuid"))
                        events.append({"kind": "say", "id": "say-%s" % (key,), "session": sid,
                                       "ts": o.get("timestamp") or "", "text": squash(c["text"])})
                if c.get("type") != "tool_use" or c.get("id") in seen:
                    continue
                seen.add(c.get("id"))        # forked sessions repeat earlier events
                ev = _event(c.get("name"), c.get("input") or {})
                if ev:
                    ev.update(id=c.get("id"), ts=o.get("timestamp") or "", session=sid)
                    events.append(ev)
    events = [e for e in events if e["id"] not in failed]
    events.sort(key=lambda e: e["ts"])
    return events


def _event(name, inp):
    if name == "Read":
        p = rel(inp.get("file_path"))
        if p is None:
            return None
        first = int(inp.get("offset") or 1)
        last = first + int(inp.get("limit") or DEFAULT_READ_LINES) - 1
        return {"kind": "read", "path": p, "lines": (first, last)}
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = rel(inp.get("file_path") or inp.get("notebook_path"))
        return {"kind": "write", "path": p, "tool": name} if p else None
    if name == "Grep":
        p = rel(inp.get("path") or ".")
        return {"kind": "search", "path": p or ".", "detail": str(inp.get("pattern"))[:80]}
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        if "tc_gate.py" in cmd:
            return None
        ev = {"kind": "bash", "cmd": cmd, "detail": squash(cmd)[:140]}
        if TEST_RUN_RE.search(cmd):
            ev["testrun"] = True
        return ev
    return None


def touches(ev, path):
    """Does this event show the agent looking at `path`?  Returns strength."""
    if ev["kind"] == "read" and ev["path"] == path:
        return "read"
    if ev["kind"] == "search" and (path == ev["path"] or path.startswith(ev["path"].rstrip("/") + "/")
                                   or ev["path"] == "."):
        return "search"
    if ev["kind"] == "bash" and (path in ev["cmd"] or Path(path).name in ev["cmd"]):
        return "shell"
    return None


def last_write(events, path):
    ts = [e["ts"] for e in events if e["kind"] == "write" and e["path"] == path]
    return max(ts) if ts else None


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def check_file(test_rel, events=None):
    """Mechanical checks.  Returns {"file_errors": [...], "cases": {id: {...}}}."""
    result = {"test_file": test_rel, "trace_file": trace_path(test_rel),
              "file_errors": [], "cases": {}}
    tf = ROOT / test_rel
    if not tf.is_file():
        result["file_errors"].append("테스트 파일이 없습니다")
        return result
    tests, context = parse_tests(tf.read_text(encoding="utf-8", errors="replace"))
    cases, err = load_trace(test_rel)
    if err:
        result["file_errors"].append(err)
    if events is None:
        events = load_events()
    if CONFIG_ERROR:
        result["file_errors"].append("CONFIG_ERROR: " + CONFIG_ERROR)
    if LOG["error"]:
        result["file_errors"].append("LOG_UNAVAILABLE: Entire로 세션 로그를 읽지 못했습니다 (%s)" % LOG["error"])
    written = last_write(events, test_rel)
    by_test = {}
    for c in cases:
        by_test.setdefault(c.get("test"), []).append(c)
    names = {t["name"] for t in tests}
    for c in cases:
        if c.get("test") not in names:
            result["file_errors"].append(
                '근거 기록 %s 이(가) 가리키는 테스트 "%s" 가 %s 에 없습니다'
                % (c.get("id"), c.get("test"), test_rel))

    for t in tests:
        matches = by_test.get(t["name"], [])
        case = matches[0] if matches else {}
        cid = case.get("id") or "(no-id:%s)" % t["name"]
        fails, flags = [], []
        if not matches:
            fails.append("NO_TRACE: 이 테스트의 근거 기록이 없습니다")
        if len(matches) > 1:
            fails.append("DUP_TRACE: 근거 기록 %d개가 이 테스트 하나를 가리킵니다" % len(matches))
        if matches and not ID_RE.match(str(case.get("id"))):
            fails.append("BAD_ID: id는 TC-<영역>-001 형식이어야 합니다")
        if t["asserts"] == 0:
            fails.append("NO_ASSERT: 테스트 본문에 assertion이 없습니다")
        if written is None and not LOG["error"]:
            fails.append("WRITE_NOT_IN_LOG: Entire 세션 로그에 메인 에이전트가 %s 을(를) Write/Edit한 기록이 "
                         "없어, 이 테스트가 어떻게 만들어졌는지 확인할 수 없습니다" % test_rel)

        refs = {}
        for s in case.get("sources") or []:
            ref = s.get("ref")
            refs[ref] = s
            f = rel(s.get("file"))
            rng = parse_lines(s.get("lines"))
            quote = squash(str(s.get("quote") or ""))
            if not is_spec(f):
                fails.append("SRC_NOT_SPEC[%s]: %s 은(는) 명세 위치(%s)가 아닙니다"
                             % (ref, s.get("file"), ", ".join(CFG["spec_globs"])))
                continue
            if not (ROOT / f).is_file():
                fails.append("SRC_MISSING[%s]: %s 파일이 없습니다" % (ref, f))
                continue
            if rng is None:
                fails.append('BAD_LINES[%s]: "lines"는 "N" 또는 "N-M" 형식이어야 합니다' % ref)
                continue
            src_lines = (ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
            cited = squash(" ".join(src_lines[rng[0] - 1:rng[1]]))
            if len(quote) < MIN_QUOTE_CHARS:
                fails.append("QUOTE_TOO_SHORT[%s]: 인용문이 너무 짧습니다. 요구사항 문장을 그대로 인용해야 합니다" % ref)
            elif quote not in cited:
                where = ("파일의 다른 줄에는 있음" if quote in squash(" ".join(src_lines))
                         else "파일 어디에도 없음")
                fails.append("QUOTE_MISMATCH[%s]: 인용문이 %s:%s 에 없습니다 (%s)"
                             % (ref, f, s.get("lines"), where))
            # Did the agent look at this source before the test was last written?
            seen = [(e, touches(e, f)) for e in events
                    if touches(e, f) and (written is None or e["ts"] <= written)]
            strong = [e for e, k in seen if k == "read"
                      and e["lines"][0] <= rng[0] and e["lines"][1] >= rng[1]]
            if strong:
                pass
            elif seen:
                flags.append("WEAK_READ_EVIDENCE[%s]: 테스트 작성 전에 %s 을(를) Read로 읽지 않고 %s 로만 조회했습니다"
                             % (ref, f, "/".join(sorted({k for _, k in seen}))))
            else:
                fails.append("NO_READ_EVIDENCE[%s]: 세션 로그에 테스트 작성 전에 %s 을(를) 읽은 기록이 없습니다"
                             % (ref, f))
        if matches and not refs:
            fails.append("NO_SOURCES: 근거 기록에 인용한 명세가 하나도 없습니다")

        for field, need in (("inputs", "rationale"), ("oracle", "derivation")):
            items = case.get(field) or []
            if field == "oracle" and matches and not items:
                fails.append("NO_ORACLE: 근거 기록에 기대값(oracle) 항목이 없습니다")
            for i, it in enumerate(items):
                tag = "%s[%d]" % (field, i)
                cited_refs = it.get("sources") or []
                if not cited_refs:
                    fails.append("UNSOURCED %s: 인용한 근거가 없습니다" % tag)
                for r in cited_refs:
                    if r not in refs:
                        fails.append("BAD_REF %s: 근거 %s 이(가) sources에 정의되어 있지 않습니다" % (tag, r))
                if not str(it.get(need) or "").strip():
                    fails.append("NO_%s %s: 설명이 비어 있습니다" % (need.upper(), tag))
                elif not korean(it.get(need)):
                    fails.append("NOT_KOREAN %s: %s 설명은 한국어로 써야 합니다" % (tag, need))
        if matches and case.get("objective") and not korean(case.get("objective")):
            fails.append("NOT_KOREAN objective: 목적 설명은 한국어로 써야 합니다")
        if matches and t["asserts"] > len(case.get("oracle") or []) > 0:
            flags.append("ASSERT_COUNT: 본문의 assertion은 %d개인데 기대값(oracle) 항목은 %d개입니다"
                         % (t["asserts"], len(case.get("oracle") or [])))
        for a in case.get("assumptions") or []:
            if not (isinstance(a, dict) and a.get("approved_by")):
                flags.append("ASSUMPTION: %s" % (a.get("text") if isinstance(a, dict) else a))

        result["cases"][cid] = {
            "id": cid, "test": t["name"], "line": t["line"], "asserts": t["asserts"],
            "hash": case_hash(t, context, case), "fails": fails, "flags": flags,
            "body": t["body"], "trace": case,
        }

    # Session-level signals for the verifier (same for every case in the file).
    file_flags = []
    if written is not None:
        first = min(e["ts"] for e in events if e["kind"] == "write" and e["path"] == test_rel)
        prod = sorted({e["path"] for e in events if e["kind"] == "read"
                       and is_prod(e["path"]) and e["ts"] <= written})
        if prod:
            file_flags.append("PROD_READ_BEFORE_TEST: 테스트를 마지막으로 쓰기 전에 구현 코드를 "
                              "읽었습니다: %s" % ", ".join(prod))
        runs = [e for e in events if e.get("testrun") and first < e["ts"] < written]
        if runs:
            file_flags.append("TEST_EDITED_AFTER_TEST_RUN: 빌드/테스트를 %d번 실행한 뒤에 테스트 파일을 "
                              "고쳤습니다. 기대값을 실행 결과에 맞춘 것이 아닌지 확인하세요" % len(runs))
    result["file_flags"] = file_flags
    result["context"] = context
    return result


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def load_registry():
    p = ROOT / REGISTRY
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"cases": {}}


def save_registry(reg):
    p = ROOT / REGISTRY
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def rejected_count(entry):
    return sum(1 for a in entry.get("attempts", []) if a["verdict"] == REJECTED)


def case_status(entry, current_hash):
    """Status of a case given its registry entry and its current hash."""
    if not entry or not entry.get("attempts"):
        return UNVERIFIED
    if entry.get("status") == ESCALATED:
        return ESCALATED
    last = entry["attempts"][-1]
    if last["hash"] != current_hash:
        return STALE                       # changed since it was judged
    return last["verdict"]


def collect_status(events=None):
    """Status rows for every test case in the repo."""
    reg = load_registry()
    rows = []
    if events is None:
        events = []                        # status does not need the log
        LOG["error"] = None
    for tf in all_test_files():
        res = check_file(tf, events)
        for cid, c in res["cases"].items():
            entry = reg["cases"].get(cid)
            rows.append({"id": cid, "test_file": tf, "test": c["test"],
                         "status": case_status(entry, c["hash"]),
                         "rejected": rejected_count(entry or {})})
    return rows


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_check(args):
    test_rel = rel(args.test_file)
    res = check_file(test_rel)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("MECHANICAL CHECK  %s  (trace: %s)" % (test_rel, res["trace_file"]))
        for e in res["file_errors"]:
            print("  FILE ERROR: " + e)
        for f in res.get("file_flags", []):
            print("  FLAG: " + f)
        for cid, c in res["cases"].items():
            print("\n  %s  %s  (line %d)  -> %s"
                  % (cid, c["test"], c["line"], "FAIL" if c["fails"] else "PASS"))
            for f in c["fails"]:
                print("    FAIL " + f)
            for f in c["flags"]:
                print("    FLAG " + f)
        if not res["cases"] and not res["file_errors"]:
            print("  no TEST blocks found")
    bad = res["file_errors"] or any(c["fails"] for c in res["cases"].values())
    return 1 if bad else 0


def timeline(test_rel, events, cited):
    """Log events that bear on a test file: [{ts, session, kind, text, path}]."""
    tp, out = trace_path(test_rel), []
    for e in events:
        kind = text = None
        path = e.get("path")
        if e["kind"] == "write" and path in (test_rel, tp):
            kind, text = "WRITE", "%s (%s)" % (path, e["tool"])
        elif e["kind"] == "write" and is_prod(path):
            kind, text = "PROD_WRITE", "%s (%s)" % (path, e["tool"])
        elif e["kind"] == "read" and is_spec(path):
            kind, text = "READ", "%s lines %d-%d" % (path, e["lines"][0], e["lines"][1])
        elif e["kind"] == "read" and is_prod(path):
            kind, text = "PROD_READ", "%s lines %d-%d" % (path, e["lines"][0], e["lines"][1])
        elif e["kind"] == "search" and (is_spec(path) or path == "."):
            kind, text = "SEARCH", "%s  /%s/" % (path, e["detail"])
        elif e["kind"] == "bash" and e.get("testrun"):
            kind, text = "TESTRUN", e["detail"]
        elif e["kind"] == "bash" and any(touches(e, c) for c in cited):
            kind, text = "SHELL", e["detail"]
            path = next(c for c in cited if touches(e, c))
        if kind:
            out.append({"ts": e["ts"], "session": e["session"], "kind": kind,
                        "text": text, "path": path})
    return out


def cited_files(test_rel):
    cases, _ = load_trace(test_rel)
    return sorted({rel(x.get("file")) for c in cases for x in c.get("sources") or []
                   if isinstance(x, dict) and rel(x.get("file"))})


def cmd_evidence(args):
    test_rel = rel(args.test_file)
    events = load_events()
    cited = cited_files(test_rel)
    print("SESSION LOG EVIDENCE for %s" % test_rel)
    print("source: %s" % LOG["source"])
    if LOG["error"]:
        print("LOG_UNAVAILABLE: %s" % LOG["error"])
        return 1
    for sid in LOG["sessions"]:
        print("  session %s   (inspect: entire session info %s)" % (sid, sid))
    print("cited sources: %s\n" % (", ".join(cited) or "(none)"))
    print("timeline (UTC) of the authoring agent - specification reads, production-code "
          "reads,\ntest writes, build/test runs:")
    rows = timeline(test_rel, events, cited)
    for r in rows:
        note = "  <- production code" if r["kind"].startswith("PROD") else ""
        print("  %s  [%s]  %-10s %s%s" % (r["ts"][:19], r["session"][:8], r["kind"], r["text"], note))
    if not rows:
        print("  (no relevant events found in the session log)")
    print()
    res = check_file(test_rel, events)
    for f in res.get("file_flags", []):
        print("FLAG: " + f)
    return 0


BRIEF_FULL_SPEC_LINES = 250      # a cited spec file this short is shown in full
BRIEF_WINDOW = 6                 # otherwise: this many lines around each cited passage


def _numbered(text, first=1, mark=()):
    return "\n".join("%s%4d| %s" % ("*" if first + i in mark else " ", first + i, line)
                     for i, line in enumerate(text.splitlines()))


def cmd_brief(args):
    """One compact document with everything needed to judge a test file, so
    the verifier does not have to collect it with many separate tool calls."""
    test_rel = rel(args.test_file)
    events = load_events()
    res = check_file(test_rel, events)
    reg = load_registry()
    out = []
    w = out.append

    pending, skipped = [], []
    for cid, c in res["cases"].items():
        st = case_status(reg["cases"].get(cid), c["hash"])
        (skipped if st in (APPROVED, ESCALATED) else pending).append((cid, c, st))
    w("VERIFICATION BRIEF  %s" % test_rel)
    w("language of findings: %s    verdict file: %s    then run: python3 %s/tc_gate.py record %s"
      % (CFG["language"], verdict_file(test_rel), STATE_DIR, test_rel))
    w("judge: %s" % (", ".join(cid for cid, _, _ in pending) or "(nothing pending)"))
    if skipped:
        w("skip:  %s" % ", ".join("%s (%s)" % (cid, st) for cid, _, st in skipped))
    for e in res["file_errors"]:
        w("FILE ERROR: " + e)
    if not pending or LOG["error"]:
        print("\n".join(out))
        return 1 if res["file_errors"] else 0

    # ---- session log -------------------------------------------------------
    cited = cited_files(test_rel)
    rows = timeline(test_rel, events, cited)
    w("\n== SESSION LOG of the authoring agent (from Entire; sessions: %s) =="
      % ", ".join(sid[:8] for sid in LOG["sessions"]))
    for f in res.get("file_flags", []):
        w("FLAG " + f)
    shown = rows[-25:]
    if len(rows) > len(shown):
        w("  (%d earlier events omitted)" % (len(rows) - len(shown)))
    for r in shown:
        w("  %s  %-10s %s" % (r["ts"][11:19], r["kind"], r["text"]))
    writes = [e for e in events if e["kind"] == "write" and e.get("path") == test_rel]
    if writes:
        last = writes[-1]
        notes = [e for e in events if e["kind"] == "say" and e["session"] == last["session"]
                 and e["ts"] <= last["ts"]][-3:]
        if notes:
            w("what the author said just before the last write of the test (its own words, not evidence):")
            for n in notes:
                w("  %s  \"%s\"" % (n["ts"][11:19], n["text"][:420] + ("..." if len(n["text"]) > 420 else "")))
    w("  (for more: entire session info <session-id> --transcript | grep -n '<text>')")

    # ---- specification -----------------------------------------------------
    w("\n== SPECIFICATION (lines marked * are cited by a pending test case) ==")
    marks = {}
    for _, c, _ in pending:
        for x in c["trace"].get("sources") or []:
            f, rng = rel(x.get("file")), parse_lines(x.get("lines"))
            if f and rng:
                marks.setdefault(f, set()).update(range(rng[0], rng[1] + 1))
    for f in sorted(marks):
        if not (ROOT / f).is_file():
            w("%s: MISSING" % f)
            continue
        lines = (ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= BRIEF_FULL_SPEC_LINES:
            w("%s (%d lines, complete):" % (f, len(lines)))
            w(_numbered("\n".join(lines), 1, marks[f]))
            continue
        w("%s (%d lines; only the cited passages with %d lines of context are shown. Read the "
          "file for other requirements that bear on a scenario):" % (f, len(lines), BRIEF_WINDOW))
        keep = set()
        for n in marks[f]:
            keep.update(range(max(1, n - BRIEF_WINDOW), min(len(lines), n + BRIEF_WINDOW) + 1))
        prev = 0
        for n in sorted(keep):
            if n != prev + 1:
                w("      ...")
            w("%s%4d| %s" % ("*" if n in marks[f] else " ", n, lines[n - 1]))
            prev = n

    # ---- fixture -----------------------------------------------------------
    context = "\n".join(l for l in res.get("context", "").splitlines() if l.strip())
    if context:
        ctx = context.splitlines()
        w("\n== TEST FILE OUTSIDE THE TEST CASES (fixtures, helpers, includes) ==")
        w("\n".join(ctx[:80]) + ("\n... (%d more lines; Read %s)" % (len(ctx) - 80, test_rel) if len(ctx) > 80 else ""))

    # ---- cases -------------------------------------------------------------
    for cid, c, st in pending:
        entry = reg["cases"].get(cid) or {}
        t = c["trace"]
        w("\n== %s  %s  (%s:%d)  status %s, rejected %d/%d =="
          % (cid, c["test"], test_rel, c["line"], st, rejected_count(entry), MAX_ATTEMPTS))
        w("mechanical check: %s" % ("FAIL" if c["fails"] else "PASS"))
        for f in c["fails"]:
            w("  FAIL " + f)
        for f in c["flags"]:
            w("  FLAG " + f)
        attempts = entry.get("attempts") or []
        if attempts:
            a = attempts[-1]
            w("previous attempt %d: %s" % (a["n"], a["verdict"]))
            for f in a.get("findings") or []:
                w("  - " + f)
            snap = a.get("snapshot") or {}
            if "body" in snap:
                d = text_diff(snap["body"], c["body"])
                w("test code changed since that attempt:" if d else "test code unchanged since that attempt.")
                if d:
                    w(d)
        w("test code:")
        w(_numbered(c["body"], c["line"]))
        w("trace record:")
        w("  objective: %s" % t.get("objective"))
        for x in t.get("sources") or []:
            w("  source %s  %s:%s  \"%s\"" % (x.get("ref"), x.get("file"), x.get("lines"), x.get("quote")))
        for kind, a, b in (("input", "item", "rationale"), ("oracle", "assertion", "derivation")):
            for i, it in enumerate(t.get(kind + "s" if kind == "input" else kind) or []):
                w("  %s[%d] %s\n      %s: %s\n      sources: %s"
                  % (kind, i, it.get(a), b, it.get(b), ", ".join(it.get("sources") or []) or "(none)"))
        for a in t.get("assumptions") or []:
            a = a if isinstance(a, dict) else {"text": a}
            w("  assumption: %s  [%s]" % (a.get("text"), "approved by " + a["approved_by"]
                                         if a.get("approved_by") else "NOT approved by the human"))
    print("\n".join(out))
    return 0


def cmd_status(args):
    rows = collect_status()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no test cases found (framework: %s, test_globs: %s)"
              % (CFG["framework"], ", ".join(CFG["test_globs"])))
        return 0
    for r in rows:
        extra = "  rejected %d/%d" % (r["rejected"], MAX_ATTEMPTS) if r["rejected"] else ""
        print("%-11s %-16s %-44s %s%s" % (r["status"], r["id"], r["test"], r["test_file"], extra))
    pending = [r for r in rows if r["status"] != APPROVED]
    print("\n%d test case(s), %d approved, %d blocking production code"
          % (len(rows), len(rows) - len(pending), len(pending)))
    return 0


def verdict_file(test_rel):
    """Where the verifier writes the verdicts for a test file."""
    return "%s/%s.json" % (INBOX, Path(test_rel).name)


def cmd_record(args):
    """Store verifier verdicts.

    The verdicts are read from the verdict file the verifier wrote with the
    Write tool (see verdict_file), so no JSON has to travel on a shell command
    line.  `--verdicts -` reads standard input instead.
    """
    test_rel = rel(args.test_file)
    source = args.verdicts or verdict_file(test_rel)
    try:
        if source == "-":
            raw = sys.stdin.read()
        elif (ROOT / source).is_file():
            raw = (ROOT / source).read_text(encoding="utf-8")
        elif not args.verdicts and not sys.stdin.isatty():
            raw, source = sys.stdin.read(), "-"
        else:
            raw = ""
        if not raw.strip():
            raise ValueError("no verdicts found")
        payload = json.loads(raw)
        verdicts = payload["verdicts"]
    except (ValueError, KeyError, TypeError) as e:
        print("record: write the verdicts with the Write tool to %s as JSON "
              "{\"verdicts\": [{\"id\": ..., \"verdict\": \"APPROVED\"|\"REJECTED\", "
              "\"findings\": [...]}]} and run record again. (%s)" % (verdict_file(test_rel), e),
              file=sys.stderr)
        return 2
    events = load_events()
    res = check_file(test_rel, events)
    if res["file_errors"]:
        print("record: fix the file errors first: %s" % "; ".join(res["file_errors"]), file=sys.stderr)
        return 2
    rows = timeline(test_rel, events, cited_files(test_rel))
    reg = load_registry()
    problems, done = [], []
    for v in verdicts:
        cid, verdict = v.get("id"), v.get("verdict")
        c = res["cases"].get(cid)
        if c is None:
            problems.append("%s: not a test case of %s" % (cid, test_rel))
            continue
        if verdict not in (APPROVED, REJECTED):
            problems.append("%s: verdict must be APPROVED or REJECTED" % cid)
            continue
        findings = v.get("findings") or []
        if not findings or not all(korean(f) for f in findings):
            problems.append("%s: not recorded. Every finding must be a sentence in the configured "
                            "language (%s); the human reads them on the web page. Keep code, paths "
                            "and quotes as they are, rewrite the findings and run record again."
                            % (cid, {"ko": "Korean", "en": "English"}.get(CFG["language"], "any")))
            continue
        entry = reg["cases"].setdefault(cid, {"attempts": []})
        if entry.get("status") == ESCALATED:
            problems.append("%s: escalated to the human after %d rejections; "
                            "only the user can reset it" % (cid, MAX_ATTEMPTS))
            continue
        if verdict == APPROVED and c["fails"]:
            problems.append("%s: cannot be APPROVED while mechanical checks fail: %s"
                            % (cid, "; ".join(c["fails"])))
            continue
        attempts = entry["attempts"]
        if attempts and attempts[-1]["hash"] == c["hash"]:
            attempts.pop()                 # re-judging unchanged content is not a new attempt
        n = len(attempts) + 1
        attempts.append({"n": n, "verdict": verdict, "hash": c["hash"], "at": now(),
                         "by": VERIFIER_AGENT, "findings": v.get("findings") or [],
                         # what exactly was judged, kept for the audit page
                         "snapshot": {"body": c["body"], "trace": c["trace"],
                                      "fails": c["fails"], "flags": c["flags"],
                                      "file_flags": res.get("file_flags", []),
                                      "timeline": rows, "sessions": list(LOG["sessions"])}})
        entry.update(test_file=test_rel, test=c["test"], status=verdict)
        if rejected_count(entry) >= MAX_ATTEMPTS and verdict == REJECTED:
            entry["status"] = ESCALATED
        report = ROOT / REPORT_DIR / cid / ("attempt-%d.md" % n)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(_report(cid, c, n, v, res, entry["status"]), encoding="utf-8")
        done.append("%s: %s (attempt %d) -> %s" % (cid, verdict, n, report.relative_to(ROOT)))
        if entry["status"] == ESCALATED:
            done.append("%s: ESCALATED - rejected %d times, stop and ask the human" % (cid, MAX_ATTEMPTS))
    save_registry(reg)
    print("\n".join(done))
    print("dashboard: %s" % write_dashboard(events))
    if problems:
        print("\n".join("record: " + p for p in problems), file=sys.stderr)
        return 1
    if source != "-":
        try:
            (ROOT / source).unlink()       # consumed; a refused file is kept for correction
        except OSError:
            pass
    return 0


def _report(cid, c, n, v, res, status):
    out = ["# %s - 검증 %d회차" % (cid, n), "",
           "- 테스트: `%s` (%s:%d)" % (c["test"], res["test_file"], c["line"]),
           "- 판정: **%s**" % v["verdict"],
           "- 상태: %s" % status, "- 기록: %s, 판정자 %s" % (now(), VERIFIER_AGENT),
           "- 내용 해시: `%s`" % c["hash"], "", "## 검증자의 판단 근거", ""]
    out += ["- %s" % f for f in (v.get("findings") or ["(없음)"])]
    out += ["", "## 기계 검사", ""]
    out += ["- FAIL %s" % f for f in c["fails"]] + ["- FLAG %s" % f for f in c["flags"]]
    out += ["- FLAG %s" % f for f in res.get("file_flags", [])]
    if not (c["fails"] or c["flags"] or res.get("file_flags")):
        out.append("- 모두 통과")
    return "\n".join(out) + "\n"


def cmd_human(args):
    reg = load_registry()
    rows = {r["id"]: r for r in collect_status()}
    if args.id not in rows:
        print("unknown test case %s" % args.id, file=sys.stderr)
        return 1
    entry = reg["cases"].setdefault(args.id, {"attempts": []})
    if args.cmd == "reset":
        entry["attempts"], entry["status"] = [], UNVERIFIED
        print("%s reset: attempt counter cleared, needs verification again" % args.id)
    else:
        res = check_file(rows[args.id]["test_file"], [])
        c = res["cases"][args.id]
        entry["attempts"].append({"n": len(entry["attempts"]) + 1, "verdict": APPROVED,
                                  "hash": c["hash"], "at": now(), "by": "human:" + args.by,
                                  "findings": ["Human override: " + args.reason],
                                  "snapshot": {"body": c["body"], "trace": c["trace"]}})
        entry.update(test_file=rows[args.id]["test_file"], test=c["test"], status=APPROVED)
        print("%s approved by %s" % (args.id, args.by))
    save_registry(reg)
    print("dashboard: %s" % write_dashboard())
    return 0


# --------------------------------------------------------------------------
# git: what changed, before and after
# --------------------------------------------------------------------------
CODE_PATHS = [":(glob)" + g for g in CFG["test_globs"] + CFG["production_globs"]] + [
    ":(glob)**/*.trace.json"]
MAX_DIFF_LINES = 800
GIT_FMT = ("%H%x1f%h%x1f%aI%x1f%an%x1f%s%x1f"
           "%(trailers:key=Entire-Checkpoint,valueonly,separator=%x20)%x1e")


def _git(*args, ok=(0,), cwd=None):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace",
                           cwd=str(cwd or ROOT), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode in ok else None


def clip(diff):
    lines = (diff or "").splitlines()
    if len(lines) > MAX_DIFF_LINES:
        more = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES] + ["@@ %d more diff lines not shown @@" % more]
    return "\n".join(lines)


def text_diff(before, after):
    """Unified-diff hunks between two texts (git diff; difflib if git is missing)."""
    before, after = before or "", after or ""
    if before == after:
        return ""
    with tempfile.TemporaryDirectory() as d:
        for name, text in (("before", before), ("after", after)):
            (Path(d) / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        out = _git("diff", "--no-index", "--no-color", "-U3", "before", "after", ok=(0, 1), cwd=d)
    if out is None:
        out = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                             "before", "after", lineterm="", n=3))
    i = out.find("@@")
    return clip(out[i:]) if i >= 0 else ""


def git_history(paths, limit=30):
    """Uncommitted changes and recent commits touching `paths`, with their diffs."""
    if (_git("rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return {"available": False, "uncommitted": "", "commits": []}
    parts = []
    if _git("rev-parse", "--verify", "-q", "HEAD") is not None:
        parts.append(_git("diff", "--relative", "--no-color", "-U3", "HEAD", "--", *paths) or "")
    for f in (_git("ls-files", "--others", "--exclude-standard", "--", *paths) or "").splitlines():
        parts.append(_git("diff", "--no-index", "--no-color", "-U3", "/dev/null", f, ok=(0, 1)) or "")
    commits = []
    for rec in (_git("log", "-n", str(limit), "--format=" + GIT_FMT, "--", *paths) or "").split("\x1e"):
        f = rec.strip("\n").split("\x1f")
        if len(f) < 6:
            continue
        commits.append({"sha": f[0], "short": f[1], "date": f[2], "author": f[3], "subject": f[4],
                        "checkpoint": f[5].strip(),
                        "diff": clip(_git("show", "--relative", "--no-color", "--format=", "-U3", f[0],
                                         "--", *paths) or "")})
    return {"available": True, "uncommitted": clip("".join(parts)), "commits": commits}


def _pretty(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) if obj is not None else ""


def attempt_diffs(attempts, body, trace):
    """Per attempt: what changed since the previously judged version.
    Also the change from the last judged version to the current files."""
    out, prev = [], None
    for a in attempts:
        snap = a.get("snapshot") or {}
        if prev is not None and "body" in snap:
            out.append({"code": text_diff(prev["body"], snap["body"]),
                        "trace": text_diff(_pretty(prev.get("trace")), _pretty(snap.get("trace")))})
        else:
            out.append(None)
        if "body" in snap:
            prev = snap
    current = None
    if prev is not None:
        current = {"code": text_diff(prev["body"], body),
                   "trace": text_diff(_pretty(prev.get("trace")), _pretty(trace))}
    return out, current


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
def dashboard_data(events=None):
    if events is None:
        events = load_events()
    log_error = LOG["error"]
    reg = load_registry()
    cases, cover, files = [], {}, {}
    for f in CFG["coverage_files"]:
        if (ROOT / f).is_file():
            cover[f] = {}
    for tf in all_test_files():
        res = check_file(tf, events)
        rows = timeline(tf, events, cited_files(tf))
        files[tf] = git_history([tf, trace_path(tf)])
        for cid, c in res["cases"].items():
            entry = reg["cases"].get(cid) or {}
            diffs, current_diff = attempt_diffs(entry.get("attempts") or [], c["body"], c["trace"])
            sources = []
            for x in c["trace"].get("sources") or []:
                f, rng = rel(x.get("file")), parse_lines(x.get("lines"))
                ctx = []
                if f and rng and (ROOT / f).is_file():
                    lines = (ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
                    for n in range(max(1, rng[0] - 1), min(len(lines), rng[1] + 1) + 1):
                        ctx.append({"n": n, "text": lines[n - 1], "cited": rng[0] <= n <= rng[1]})
                    for n in range(rng[0], rng[1] + 1):
                        cover.setdefault(f, {}).setdefault(n, []).append(cid)
                tag = "[%s]" % x.get("ref")
                sources.append({
                    "ref": x.get("ref"), "file": x.get("file"), "lines": str(x.get("lines")),
                    "quote": x.get("quote"), "context": ctx,
                    "quote_ok": not any(tag in y and ("QUOTE" in y or "SRC_" in y or "BAD_LINES" in y)
                                        for y in c["fails"]),
                    "read": ("none" if any(tag in y and "NO_READ" in y for y in c["fails"])
                             else "weak" if any(tag in y for y in c["flags"]) else "read")})
            cases.append({
                "id": cid, "test": c["test"], "file": tf, "line": c["line"],
                "status": case_status(entry, c["hash"]),
                "rejected": rejected_count(entry), "hash": c["hash"], "body": c["body"],
                "objective": c["trace"].get("objective"), "sources": sources,
                "inputs": c["trace"].get("inputs") or [], "oracle": c["trace"].get("oracle") or [],
                "assumptions": c["trace"].get("assumptions") or [],
                "fails": c["fails"] + res["file_errors"],
                "flags": c["flags"] + res.get("file_flags", []),
                "timeline": rows, "attempts": entry.get("attempts") or [],
                "attempt_diffs": diffs, "current_diff": current_diff})
    status_of = {c["id"]: c["status"] for c in cases}
    coverage = []
    for f in sorted(cover):
        lines = (ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
        coverage.append({"file": f, "lines": [
            {"n": i + 1, "text": t,
             "cases": [{"id": cid, "status": status_of.get(cid)}
                       for cid in sorted(set(cover[f].get(i + 1, [])))]}
            for i, t in enumerate(lines) if t.strip()]})
    return {"generated": now(), "max_attempts": MAX_ATTEMPTS, "cases": cases, "coverage": coverage,
            "files": files, "code": git_history(CODE_PATHS),
            "prod_re": _PROD_RE.pattern, "test_re": _TEST_RE.pattern,
            "config": {k: CFG[k] for k in ("framework", "test_globs", "spec_globs",
                                           "production_globs", "language")},
            "log": {"source": LOG["source"], "sessions": LOG["sessions"], "error": log_error},
            "locked": (not cases) or any(c["status"] != APPROVED for c in cases)}


def write_dashboard(events=None):
    data = json.dumps(dashboard_data(events), ensure_ascii=False).replace("</", "<\\/")
    template = (Path(__file__).resolve().parent / "dashboard_template.html").read_text(encoding="utf-8")
    out = ROOT / DASHBOARD
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template.replace("/*__TC_GATE_DATA__*/null", data), encoding="utf-8")
    return DASHBOARD


def cmd_dashboard(_args):
    path = ROOT / write_dashboard()
    print("wrote %s" % path)
    if shutil.which("wslpath"):
        win = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
        if win:
            print("open in a Windows browser: %s" % win)
    if LOG["error"]:
        print("note: session log unavailable (%s)" % LOG["error"])
    return 0


# --------------------------------------------------------------------------
# PreToolUse hook
# --------------------------------------------------------------------------
def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    return 0


GATE_CMD_RE = re.compile(r"tc_gate\.py[\"']?\s+(record|human-approve|reset)\b")


GATED_TARGET = "|".join(glob_re(g, shell=True) for g in CFG["production_globs"]) or r"(?!)"
RECORD_TARGET = re.escape(STATE_DIR) + r"(?![\w-])"


def _shell_writes(cmd, target):
    """Best-effort: does this shell command write to a path matching `target`?"""
    target = r"[\"']?(?:\./)?(?:%s)" % target
    return bool(re.search(r">{1,2}\s*" + target, cmd) or re.search(
        r"\b(?:tee|sed\s+-i|cp|mv|rm|truncate|touch)\b[^|;&\n]*?[\s\"'=]" + target, cmd))


def cmd_hook(_args):
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    tool = data.get("tool_name")
    inp = data.get("tool_input") or {}
    agent = data.get("agent_type")

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = rel(inp.get("file_path") or inp.get("notebook_path"))
        if agent == VERIFIER_AGENT:
            if p and re.fullmatch(re.escape(INBOX) + r"/[^/]+\.json", p):
                return 0
            return deny("TC gate: the %s only judges. It may write nothing except its verdict "
                        "file under %s/. Report what is wrong instead of editing %s."
                        % (VERIFIER_AGENT, INBOX, p))
        if under(p, (STATE_DIR + "/",)):
            return deny("TC gate: %s belongs to the gate. Approval records are written only by "
                        "`tc_gate.py record` (run by the %s subagent); the tool and its config are "
                        "changed only by the human. Do not edit them by hand." % (p, VERIFIER_AGENT))
        if is_prod(p):
            return _gate(p)
        return 0

    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        m = GATE_CMD_RE.search(cmd)
        if m and m.group(1) != "record":
            return deny("TC gate: `%s` is a human override. Stop and ask the user to run it "
                        "themselves (prefix the command with `!`)." % m.group(1))
        if m and agent != VERIFIER_AGENT:
            return deny("TC gate: verdicts may only be recorded by the %s subagent "
                        "(caller: %s). Spawn %s; do not approve your own test cases."
                        % (VERIFIER_AGENT, agent or "main agent", VERIFIER_AGENT))
        if agent == VERIFIER_AGENT and _shell_writes(cmd, r"(?!/dev/)[\w./-]+"):
            return deny("TC gate: the %s only judges and must not change files with shell "
                        "commands. Use read-only commands." % VERIFIER_AGENT)
        if not m and _shell_writes(cmd, RECORD_TARGET):
            return deny("TC gate: files under %s/ are written only by `tc_gate.py record` or by "
                        "the human. Do not edit them by hand." % STATE_DIR)
        if not m and _shell_writes(cmd, GATED_TARGET):
            return _gate("production code via shell")
    return 0


def _gate(target):
    rows = collect_status()
    if not rows:
        return deny("TC gate: %s is locked. No test cases exist yet - write the test case and "
                    "its trace record first (TDD), then have them verified. See the tc-approval "
                    "skill." % target)
    pending = [r for r in rows if r["status"] != APPROVED]
    if not pending:
        return 0
    lines = ["%s %s (%s)" % (r["status"], r["id"], r["test"]) for r in pending[:12]]
    return deny("TC gate: %s is locked until every test case is APPROVED by the %s subagent. "
                "Pending: %s. Follow the tc-approval skill; "
                "ESCALATED cases need the human." % (target, VERIFIER_AGENT, "; ".join(lines)))


def cmd_doctor(_args):
    """Check that everything the gate needs is in place."""
    ok = True

    def line(good, text, hint=""):
        nonlocal ok
        ok = ok and good
        print("  [%s] %s%s" % ("ok" if good else "!!", text, ("\n       -> " + hint) if hint and not good else ""))

    print("AcceptTC doctor  (project: %s)" % ROOT)
    line(CONFIG_ERROR is None, "config %s" % (CONFIG_ERROR or CONFIG_FILE))
    print("       framework=%s language=%s max_attempts=%d" % (CFG["framework"], CFG["language"], MAX_ATTEMPTS))
    print("       tests: %s\n       specs: %s\n       production (locked): %s"
          % (", ".join(CFG["test_globs"]), ", ".join(CFG["spec_globs"]), ", ".join(CFG["production_globs"])))
    line(_git("rev-parse", "--is-inside-work-tree") is not None, "git repository", "run: git init")
    load_events()
    line(LOG["error"] is None, "Entire session log (%d session(s))" % len(LOG["sessions"]),
         "%s | run: entire enable --agent claude-code" % LOG["error"])
    settings = ROOT / ".claude" / "settings.json"
    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    line("tc_gate.py" in text, "gate hook in .claude/settings.json", "re-run the AcceptTC installer")
    line("entire hooks" in text, "Entire hooks in .claude/settings.json",
         "run: entire enable --agent claude-code   (from %s)" % ROOT)
    line((ROOT / ".claude/agents/tc-verifier.md").is_file(), "verifier agent .claude/agents/tc-verifier.md",
         "re-run the AcceptTC installer")
    line((ROOT / ".claude/skills/tc-approval/SKILL.md").is_file(), "skill .claude/skills/tc-approval",
         "re-run the AcceptTC installer")
    files = all_test_files()
    cases = collect_status()
    print("       %d test file(s), %d test case(s), %d approved"
          % (len(files), len(cases), sum(1 for c in cases if c["status"] == APPROVED)))
    print("result: %s" % ("ready" if ok else "NOT ready - fix the items marked !!"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="tc_gate.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, hlp in (("check", cmd_check, "mechanical checks on a test file"),
                          ("brief", cmd_brief, "everything the verifier needs, in one output"),
                          ("evidence", cmd_evidence, "session-log evidence for a test file"),
                          ("record", cmd_record, "store the verifier's verdicts (verifier only)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("test_file")
        if name == "check":
            p.add_argument("--json", action="store_true")
        if name == "record":
            p.add_argument("--verdicts", help="verdict JSON file, or - for stdin "
                                              "(default: the verifier's verdict file)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("status", help="approval state of every test case")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("dashboard", help="write the verification web page")
    p.set_defaults(fn=cmd_dashboard)
    p = sub.add_parser("doctor", help="check the installation")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("hook", help="PreToolUse hook entry point")
    p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("human-approve", help="human override (user only)")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_human)
    p = sub.add_parser("reset", help="clear the attempt counter (user only)")
    p.add_argument("id")
    p.set_defaults(fn=cmd_human)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
