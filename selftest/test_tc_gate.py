#!/usr/bin/env python3
"""Self-test for tc_gate.py.  Run: python3 selftest/test_tc_gate.py

Builds a throw-away repo plus a fake agent session log and drives the CLI the
way the author agent, the verifier subagent and the hook do.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "accepttc" / "tc_gate.py"

SPEC = """Door Controller
- The door stays closed while idle.
- The controller has an internal tick. A door cycle lasts 3 ticks.
- If both the inner and the outer button are pressed, the inner button has priority.
"""

TEST_CPP = """#include <gtest/gtest.h>
extern "C" {
#include "controller.h"
}

class ControllerTest : public ::testing::Test {
 protected:
  void SetUp() override { rvc_init(&c); }
  rvc_t c;
};

TEST_F(ControllerTest, TurnLastsThreeTicks) {
  // braces in a string must not confuse the parser: "}{"
  rvc_front(&c, true);
  for (int i = 0; i < 3; ++i) { EXPECT_EQ(rvc_tick(&c), DIR_TURN_LEFT); }
  EXPECT_EQ(rvc_tick(&c), DIR_FORWARD);
}

TEST_F(ControllerTest, LeftHasPriority) {
  rvc_front(&c, true);
  EXPECT_EQ(rvc_tick(&c), DIR_TURN_LEFT);
}
"""


KO = ["명세의 해당 줄에서 그대로 도출됩니다."]


def case(cid, test, line, quote):
    return {
        "id": cid, "test": test, "objective": "회전 동작을 확인한다",
        "sources": [{"ref": "S1", "file": "docs/req.md", "lines": str(line), "quote": quote}],
        "inputs": [{"item": "front=true", "rationale": "장애물이 회전을 일으킨다", "sources": ["S1"]}],
        "oracle": [{"assertion": "EXPECT_EQ(...)", "derivation": "명세에 그대로 적혀 있다", "sources": ["S1"]},
                   {"assertion": "EXPECT_EQ(...)", "derivation": "명세에 그대로 적혀 있다", "sources": ["S1"]}],
        "assumptions": [],
    }


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.logs = Path(self.tmp.name) / "logs"
        for d in ("docs", "tests", "src"):
            (self.root / d).mkdir(parents=True)
        self.logs.mkdir()
        (self.root / "docs/req.md").write_text(SPEC)
        (self.root / "tests/controller_test.cpp").write_text(TEST_CPP)
        self.write_trace([
            case("TC-CTRL-001", "ControllerTest.TurnLastsThreeTicks", 3, "A door cycle lasts 3 ticks."),
            case("TC-CTRL-002", "ControllerTest.LeftHasPriority", 4, "the inner button has priority"),
        ])
        self.n = 0
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def write_trace(self, cases):
        (self.root / "tests/controller_test.trace.json").write_text(
            json.dumps({"test_file": "tests/controller_test.cpp", "cases": cases}))

    def tool(self, name, inp, agent=None):
        """Append a tool call to the fake session log."""
        self.n += 1
        line = json.dumps({
            "type": "assistant", "sessionId": "sess0001",
            "timestamp": "2026-01-01T00:%02d:00.000Z" % self.n,
            "message": {"content": [{"type": "tool_use", "id": "tu%d" % self.n,
                                     "name": name, "input": inp}]}})
        if agent:
            d = self.logs / "sess0001" / "subagents"
            d.mkdir(parents=True, exist_ok=True)
            (d / ("agent-%s.meta.json" % agent)).write_text(json.dumps({"agentType": agent}))
            with open(d / ("agent-%s.jsonl" % agent), "a") as f:
                f.write(line + "\n")
        else:
            with open(self.logs / "sess0001.jsonl", "a") as f:
                f.write(line + "\n")

    def p(self, relpath):
        return str(self.root / relpath)

    def run_gate(self, *args, stdin=""):
        env = dict(os.environ, TC_GATE_ROOT=str(self.root), TC_GATE_TRANSCRIPT_DIR=str(self.logs))
        r = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout, r.stderr

    def check(self):
        code, out, _ = self.run_gate("check", "tests/controller_test.cpp", "--json")
        return code, json.loads(out)

    def record(self, verdicts):
        return self.run_gate("record", "tests/controller_test.cpp",
                             stdin=json.dumps({"verdicts": verdicts}))

    def hook(self, tool, inp, agent=None):
        payload = {"tool_name": tool, "tool_input": inp}
        if agent:
            payload["agent_type"] = agent
        _, out, _ = self.run_gate("hook", stdin=json.dumps(payload))
        if not out.strip():
            return None
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]

    def honest_session(self):
        self.tool("Read", {"file_path": self.p("docs/req.md")})
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})

    def status(self):
        _, out, _ = self.run_gate("status", "--json")
        return {r["id"]: r["status"] for r in json.loads(out)}

    # -- mechanical checks ------------------------------------------------
    def test_honest_test_passes(self):
        self.honest_session()
        code, res = self.check()
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(res["cases"]), ["TC-CTRL-001", "TC-CTRL-002"])
        self.assertEqual(res["cases"]["TC-CTRL-001"]["asserts"], 2)

    def test_spec_never_read_is_caught(self):
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})
        code, res = self.check()
        self.assertEqual(code, 1)
        self.assertTrue(any("NO_READ_EVIDENCE" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    def test_spec_read_only_after_writing_is_caught(self):
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})
        self.tool("Read", {"file_path": self.p("docs/req.md")})
        _, res = self.check()
        self.assertTrue(any("NO_READ_EVIDENCE" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    def test_verifier_reads_are_not_evidence(self):
        self.tool("Read", {"file_path": self.p("docs/req.md")}, agent="tc-verifier")
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})
        _, res = self.check()
        self.assertTrue(any("NO_READ_EVIDENCE" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    def test_test_written_outside_the_main_agent_log_is_caught(self):
        self.tool("Read", {"file_path": self.p("docs/req.md")})
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")}, agent="tc-author")
        code, res = self.check()
        self.assertEqual(code, 1)
        self.assertTrue(any("WRITE_NOT_IN_LOG" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    def test_forked_session_events_are_not_counted_twice(self):
        self.honest_session()
        (self.logs / "sess0002.jsonl").write_text((self.logs / "sess0001.jsonl").read_text())
        _, out, _ = self.run_gate("evidence", "tests/controller_test.cpp")
        self.assertEqual(out.count(" WRITE "), 1, out)

    def test_unreadable_log_blocks_approval(self):
        self.honest_session()
        env = dict(os.environ, TC_GATE_ROOT=str(self.root), TC_GATE_ENTIRE="/nonexistent/entire")
        env.pop("TC_GATE_TRANSCRIPT_DIR", None)
        r = subprocess.run([sys.executable, str(SCRIPT), "record", "tests/controller_test.cpp"],
                           input=json.dumps({"verdicts": [{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}]}),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOG_UNAVAILABLE", r.stderr)

    def test_dashboard_shows_the_judged_snapshot(self):
        self.honest_session()
        self.record([{"id": "TC-CTRL-001", "verdict": "REJECTED", "findings": ["명세와 다릅니다 <b>bad</b>"]}])
        html = (self.root / ".accepttc/verification/index.html").read_text()
        self.assertIn("TC-CTRL-001", html)
        self.assertIn("A door cycle lasts 3 ticks.", html)
        self.assertNotIn("__TC_GATE_DATA__", html)
        self.assertNotIn("</b>", html)
        reg = json.loads((self.root / ".accepttc/registry.json").read_text())
        snap = reg["cases"]["TC-CTRL-001"]["attempts"][0]["snapshot"]
        self.assertIn("TurnLastsThreeTicks", snap["body"])
        self.assertEqual([r["kind"] for r in snap["timeline"]], ["READ", "WRITE"])

    def test_partial_read_is_only_weak_evidence(self):
        self.tool("Read", {"file_path": self.p("docs/req.md"), "offset": 1, "limit": 2})
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})
        code, res = self.check()
        self.assertEqual(code, 0)
        self.assertTrue(any("WEAK_READ_EVIDENCE" in f for f in res["cases"]["TC-CTRL-001"]["flags"]))

    def test_invented_quote_is_caught(self):
        self.honest_session()
        self.write_trace([
            case("TC-CTRL-001", "ControllerTest.TurnLastsThreeTicks", 3, "A door cycle lasts 5 ticks."),
            case("TC-CTRL-002", "ControllerTest.LeftHasPriority", 2, "the inner button has priority"),
        ])
        _, res = self.check()
        self.assertTrue(any("파일 어디에도 없음" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))
        self.assertTrue(any("파일의 다른 줄에는 있음" in f for f in res["cases"]["TC-CTRL-002"]["fails"]))

    def test_missing_trace_and_unsourced_oracle(self):
        self.honest_session()
        c = case("TC-CTRL-001", "ControllerTest.TurnLastsThreeTicks", 3, "A door cycle lasts 3 ticks.")
        c["oracle"][0]["sources"] = []
        c["assumptions"] = [{"text": "turn direction defaults to left"}]
        self.write_trace([c])
        _, res = self.check()
        self.assertTrue(any("UNSOURCED oracle[0]" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))
        self.assertTrue(any("ASSUMPTION" in f for f in res["cases"]["TC-CTRL-001"]["flags"]))
        missing = [c for c in res["cases"].values() if c["test"] == "ControllerTest.LeftHasPriority"][0]
        self.assertTrue(any("NO_TRACE" in f for f in missing["fails"]))

    def test_log_flags_fitting_the_oracle(self):
        self.tool("Read", {"file_path": self.p("docs/req.md")})
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})
        self.tool("Read", {"file_path": self.p("src/controller.c")})
        self.tool("Bash", {"command": "ctest --test-dir build"})
        self.tool("Edit", {"file_path": self.p("tests/controller_test.cpp")})
        _, res = self.check()
        flags = " ".join(res["file_flags"])
        self.assertIn("PROD_READ_BEFORE_TEST", flags)
        self.assertIn("TEST_EDITED_AFTER_TEST_RUN", flags)

    def test_dashboard_shows_before_and_after_from_git(self):
        def git(*a):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                           cwd=self.root, check=True, capture_output=True)
        self.honest_session()
        git("init", "-q")
        git("add", "tests", "docs")
        git("commit", "-q", "-m", "test: add controller tests", "-m", "Entire-Checkpoint: abc123def456")
        self.record([{"id": "TC-CTRL-001", "verdict": "REJECTED", "findings": KO}])
        f = self.root / "tests/controller_test.cpp"
        f.write_text(f.read_text().replace("i < 3", "i < 4"))
        self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}])
        (self.root / "src/controller.c").write_text("int rvc_tick(void) { return 0; }\n")
        html = (self.root / ".accepttc/verification/index.html").read_text()
        data = json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        case = [c for c in data["cases"] if c["id"] == "TC-CTRL-001"][0]
        self.assertIsNone(case["attempt_diffs"][0])
        self.assertIn("-  for (int i = 0; i < 3; ++i)", case["attempt_diffs"][1]["code"])
        self.assertIn("+  for (int i = 0; i < 4; ++i)", case["attempt_diffs"][1]["code"])
        self.assertEqual(case["current_diff"]["code"], "")
        hist = data["files"]["tests/controller_test.cpp"]
        self.assertEqual(hist["commits"][0]["subject"], "test: add controller tests")
        self.assertEqual(hist["commits"][0]["checkpoint"], "abc123def456")
        self.assertIn("i < 4", hist["uncommitted"])
        self.run_gate("dashboard")
        html = (self.root / ".accepttc/verification/index.html").read_text()
        data = json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertIn("+++ b/src/controller.c", data["code"]["uncommitted"])

    # -- verdicts, retries, staleness --------------------------------------
    def test_approval_then_edit_goes_stale(self):
        self.honest_session()
        code, out, err = self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO},
                                      {"id": "TC-CTRL-002", "verdict": "APPROVED", "findings": KO}])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.status(), {"TC-CTRL-001": "APPROVED", "TC-CTRL-002": "APPROVED"})
        self.assertTrue((self.root / ".accepttc/verification/TC-CTRL-001/attempt-1.md").is_file())
        # changing one test body invalidates only that test
        f = self.root / "tests/controller_test.cpp"
        f.write_text(f.read_text().replace("i < 3", "i < 4"))
        self.assertEqual(self.status(), {"TC-CTRL-001": "STALE", "TC-CTRL-002": "APPROVED"})
        # changing the fixture invalidates every test in the file
        f.write_text(f.read_text().replace("rvc_init(&c);", "rvc_init(&c); rvc_reset(&c);"))
        self.assertEqual(self.status(), {"TC-CTRL-001": "STALE", "TC-CTRL-002": "STALE"})

    def test_cannot_approve_when_mechanical_checks_fail(self):
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})   # spec never read
        code, _, err = self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}])
        self.assertEqual(code, 1)
        self.assertIn("cannot be APPROVED", err)
        self.assertEqual(self.status()["TC-CTRL-001"], "UNVERIFIED")

    def test_three_rejections_escalate(self):
        self.honest_session()
        f = self.root / "tests/controller_test.cpp"
        for i in range(3):
            f.write_text(f.read_text().replace("DIR_FORWARD", "DIR_FORWARD /*%d*/" % i))
            _, out, _ = self.record([{"id": "TC-CTRL-001", "verdict": "REJECTED", "findings": KO}])
        self.assertIn("ESCALATED", out)
        self.assertEqual(self.status()["TC-CTRL-001"], "ESCALATED")
        f.write_text(f.read_text().replace("DIR_FORWARD", "DIR_FORWARD /*again*/"))
        code, _, err = self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}])
        self.assertEqual(code, 1)
        self.assertIn("only the user can reset", err)
        self.assertEqual(self.status()["TC-CTRL-001"], "ESCALATED")
        self.run_gate("reset", "TC-CTRL-001")
        self.assertEqual(self.status()["TC-CTRL-001"], "UNVERIFIED")

    def test_rejudging_unchanged_content_is_not_a_new_attempt(self):
        self.honest_session()
        for _ in range(4):
            self.record([{"id": "TC-CTRL-001", "verdict": "REJECTED", "findings": KO}])
        self.assertEqual(self.status()["TC-CTRL-001"], "REJECTED")

    def test_explanations_must_be_korean(self):
        self.honest_session()
        code, out, err = self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED",
                                       "findings": ["Follows from docs/req.md:3."]}])
        self.assertEqual(code, 1)
        self.assertIn("Korean", err)
        self.assertEqual(self.status()["TC-CTRL-001"], "UNVERIFIED")
        c = case("TC-CTRL-001", "ControllerTest.TurnLastsThreeTicks", 3, "A door cycle lasts 3 ticks.")
        c["oracle"][0]["derivation"] = "stated directly in the requirement"
        self.write_trace([c])
        _, res = self.check()
        self.assertTrue(any("NOT_KOREAN oracle[0]" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    # -- other frameworks and layouts ----------------------------------------
    def parse(self, framework, relpath, source, extra=None):
        cfg = {"framework": framework, "test_globs": [relpath]}
        cfg.update(extra or {})
        (self.root / ".accepttc").mkdir(exist_ok=True)
        (self.root / ".accepttc/config.json").write_text(json.dumps(cfg))
        f = self.root / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(source)
        _, out, err = self.run_gate("check", relpath, "--json")
        cases = json.loads(out)["cases"].values()
        return {c["test"]: c["asserts"] for c in cases}, {c["test"]: c["body"] for c in cases}

    def test_pytest_cases(self):
        names, bodies = self.parse("pytest", "tests/test_ctrl.py", (
            "import pytest\n\n\ndef helper():\n    return 1\n\n\n"
            "@pytest.mark.parametrize(\n    'ticks', [1, 2, 3],\n)\n"
            "def test_turn(ticks):\n    c = make()\n    assert c.tick() == LEFT\n\n    assert c.mode == OFF\n\n\n"
            "class TestDust:\n    def test_clean_up(self):\n        with pytest.raises(ValueError):\n            run()\n\n"
            "    def helper(self):\n        assert False\n"))
        self.assertEqual(names, {"test_turn": 2, "TestDust.test_clean_up": 1})
        self.assertTrue(bodies["test_turn"].startswith("@pytest.mark.parametrize"))
        self.assertNotIn("class TestDust", bodies["test_turn"])
        self.assertNotIn("def helper", bodies["TestDust.test_clean_up"])

    def test_junit_cases(self):
        names, bodies = self.parse("junit", "src/test/java/CtrlTest.java", (
            "class CtrlTest {\n  private Ctrl c = new Ctrl();\n\n"
            "  @Test\n  @DisplayName(\"turn {3}\")\n  void turnLastsThreeTicks() throws Exception {\n"
            "    assertEquals(LEFT, c.tick()); // }\n    assertThat(c.mode()).isEqualTo(OFF);\n  }\n\n"
            "  void helper() { fail(); }\n\n"
            "  @Test public void leftHasPriority() {\n    assertTrue(c.left());\n  }\n}\n"),
            {"production_globs": ["src/main/**"]})
        self.assertEqual(names, {"CtrlTest.turnLastsThreeTicks": 2, "CtrlTest.leftHasPriority": 1})
        self.assertNotIn("helper", bodies["CtrlTest.turnLastsThreeTicks"])
        self.assertIsNone(self.hook("Write", {"file_path": self.p("src/test/java/Other.java")}))
        self.assertIsNotNone(self.hook("Write", {"file_path": self.p("src/main/java/Ctrl.java")}))

    def test_jest_go_and_unity_cases(self):
        names, bodies = self.parse("jest", "src/ctrl.test.ts", (
            "describe('ctrl', () => {\n  it('turns left (3 ticks)', async () => {\n"
            "    expect(tick()).toBe(LEFT);\n    expect(`${a})`).toBe(')');\n  });\n\n"
            "  test(\"left has priority\", () => { expect(left()).toBe(true) })\n});\n"))
        self.assertEqual(names, {"turns left (3 ticks)": 2, "left has priority": 1})
        # a test file inside a production glob is still a test, never locked production code
        self.assertIsNone(self.hook("Write", {"file_path": self.p("src/ctrl.test.ts")}))
        self.assertIsNotNone(self.hook("Write", {"file_path": self.p("src/ctrl.ts")}))
        names, _ = self.parse("go", "ctrl_test.go", (
            "package ctrl\n\nfunc helper(t *testing.T) {}\n\n"
            "func TestTurn(t *testing.T) {\n\tif tick() != Left {\n\t\tt.Fatalf(\"got %v\", 1)\n\t}\n"
            "\tassert.Equal(t, Off, mode())\n}\n"))
        self.assertEqual(names, {"TestTurn": 2})
        names, _ = self.parse("unity", "test/test_ctrl.c", (
            "void setUp(void) {}\nvoid test_turn(void)\n{\n    TEST_ASSERT_EQUAL(LEFT, tick());\n}\n"))
        self.assertEqual(names, {"test_turn": 1})

    def test_bad_config_is_reported_not_fatal(self):
        (self.root / ".accepttc").mkdir(exist_ok=True)
        (self.root / ".accepttc/config.json").write_text('{"framework": "nope"}')
        self.honest_session()
        code, res = self.check()
        self.assertEqual(code, 1)
        self.assertTrue(any("CONFIG_ERROR" in e for e in res["file_errors"]))
        self.assertIn("locked", self.hook("Write", {"file_path": self.p("src/a.c")}))

    def test_language_any_does_not_require_korean(self):
        (self.root / ".accepttc").mkdir(exist_ok=True)
        (self.root / ".accepttc/config.json").write_text('{"language": "en"}')
        self.honest_session()
        code, _, err = self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED",
                                     "findings": ["Follows from docs/req.md:3."]}])
        self.assertEqual(code, 0, err)

    def test_verifier_records_through_a_verdict_file(self):
        self.honest_session()
        inbox = self.root / ".accepttc/inbox/controller_test.cpp.json"
        # only the verifier may write the verdict file, and it may write nothing else
        self.assertIsNone(self.hook("Write", {"file_path": str(inbox)}, agent="tc-verifier"))
        self.assertIn("by hand", self.hook("Write", {"file_path": str(inbox)}))
        self.assertIn("only judges", self.hook("Edit", {"file_path": self.p("tests/controller_test.cpp")},
                                               agent="tc-verifier"))
        self.assertIn("only judges", self.hook("Write", {"file_path": self.p(".accepttc/registry.json")},
                                               agent="tc-verifier"))
        self.assertIn("only judges", self.hook("Bash", {"command": "sed -i s/3/4/ tests/controller_test.cpp"},
                                               agent="tc-verifier"))
        for cmd in ("entire session info abc --transcript | grep -n Write",
                    "ls docs artifacts 2>/dev/null; cat -n tests/controller_test.cpp 2>&1 | head"):
            self.assertIsNone(self.hook("Bash", {"command": cmd}, agent="tc-verifier"), cmd)
        self.assertIsNone(self.hook("Bash", {"command": "python3 .accepttc/tc_gate.py record tests/controller_test.cpp"},
                                    agent="tc-verifier"))
        # record with nothing to read explains where the file goes
        env = dict(os.environ, TC_GATE_ROOT=str(self.root), TC_GATE_TRANSCRIPT_DIR=str(self.logs))
        run = lambda: subprocess.run([sys.executable, str(SCRIPT), "record", "tests/controller_test.cpp"],  # noqa: E731
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
        r = run()
        self.assertEqual(r.returncode, 2)
        self.assertIn(".accepttc/inbox/controller_test.cpp.json", r.stderr)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(json.dumps({"verdicts": [{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}]}))
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.status()["TC-CTRL-001"], "APPROVED")
        self.assertFalse(inbox.exists())
        # a refused verdict file stays so it can be corrected
        inbox.write_text(json.dumps({"verdicts": [{"id": "TC-CTRL-002", "verdict": "APPROVED",
                                                   "findings": ["english only"]}]}))
        self.assertEqual(run().returncode, 1)
        self.assertTrue(inbox.exists())

    def test_brief_gives_the_verifier_everything_in_one_output(self):
        self.honest_session()
        with open(self.logs / "sess0001.jsonl", "a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "u1", "sessionId": "sess0001",
                                "timestamp": "2026-01-01T00:01:30.000Z",
                                "message": {"content": [{"type": "text", "text": "req 3 says three ticks"}]}}) + "\n")
        self.record([{"id": "TC-CTRL-001", "verdict": "REJECTED", "findings": ["틱 수가 명세와 다릅니다."]},
                     {"id": "TC-CTRL-002", "verdict": "APPROVED", "findings": KO}])
        f = self.root / "tests/controller_test.cpp"
        f.write_text(f.read_text().replace("DIR_FORWARD", "DIR_STOP"))
        code, out, _ = self.run_gate("brief", "tests/controller_test.cpp")
        self.assertEqual(code, 0)
        self.assertIn("judge: TC-CTRL-001", out)
        self.assertIn("skip:  TC-CTRL-002 (APPROVED)", out)
        self.assertIn(".accepttc/inbox/controller_test.cpp.json", out)
        self.assertIn("*   3| - The controller has an internal tick. A door cycle lasts 3 ticks.", out)
        self.assertIn("req 3 says three ticks", out)                 # the author's own words
        self.assertIn("틱 수가 명세와 다릅니다.", out)                   # previous findings
        self.assertIn("-  EXPECT_EQ(rvc_tick(&c), DIR_FORWARD);", out)  # what changed since
        self.assertIn("void SetUp() override", out)                  # fixture
        self.assertNotIn("LeftHasPriority) {", out)                  # approved case is not repeated
        self.assertLess(len(out), 6000)

    def test_gate_files_are_off_limits_to_agents(self):
        for f in (".accepttc/config.json", ".accepttc/tc_gate.py", ".accepttc/registry.json"):
            self.assertIn("by hand", self.hook("Edit", {"file_path": self.p(f)}))
        self.assertIsNotNone(self.hook("Bash", {"command": "rm -rf .accepttc"}))
        self.assertIsNotNone(self.hook("Bash", {"command": "sed -i s/src/x/ .accepttc/config.json"}))
        self.assertIsNone(self.hook("Bash", {"command": "python3 .accepttc/tc_gate.py doctor"}))

    def test_project_in_a_subdirectory_of_the_git_repo(self):
        def git(*a):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                           cwd=self.tmp.name, check=True, capture_output=True)
        self.honest_session()
        git("init", "-q")
        git("add", "repo")
        git("commit", "-q", "-m", "init")
        f = self.root / "tests/controller_test.cpp"
        f.write_text(f.read_text().replace("i < 3", "i < 4"))
        self.run_gate("dashboard")
        html = (self.root / ".accepttc/verification/index.html").read_text()
        data = json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertIn("+++ b/tests/controller_test.cpp", data["files"]["tests/controller_test.cpp"]["uncommitted"])
        self.assertNotIn("b/repo/", data["code"]["uncommitted"])

    # -- hook ---------------------------------------------------------------
    def test_hook_locks_production_code_until_approved(self):
        self.honest_session()
        src = {"file_path": self.p("src/controller.c")}
        self.assertIn("locked", self.hook("Write", src))
        for cmd in ("cat > src/controller.c <<EOF\nEOF", "sed -i s/a/b/ src/controller.c",
                    "cp /x/y.c ./src/controller.c"):
            self.assertIn("locked", self.hook("Bash", {"command": cmd}) or "", cmd)
        for cmd in ("gcc -c src/controller.c -o build/c.o 2>&1 | tail", "cat src/controller.c > /dev/null",
                    "cmake --build build 2>&1"):
            self.assertIsNone(self.hook("Bash", {"command": cmd}), cmd)
        self.assertIsNone(self.hook("Write", {"file_path": self.p("tests/other_test.cpp")}))
        self.assertIsNone(self.hook("Write", {"file_path": self.p("include/controller.h")}))
        self.record([{"id": "TC-CTRL-001", "verdict": "APPROVED", "findings": KO}])
        self.assertIn("TC-CTRL-002", self.hook("Edit", src))
        self.record([{"id": "TC-CTRL-002", "verdict": "APPROVED", "findings": KO}])
        self.assertIsNone(self.hook("Edit", src))

    def test_hook_locks_when_no_tests_exist(self):
        (self.root / "tests/controller_test.cpp").unlink()
        self.assertIn("No test cases exist", self.hook("Write", {"file_path": self.p("src/a.c")}))

    def test_hook_only_verifier_may_record(self):
        cmd = {"command": "python3 .accepttc/tc_gate.py record tests/controller_test.cpp <<'J'\n{}\nJ"}
        self.assertIn("do not approve your own", self.hook("Bash", cmd))
        self.assertIn("do not approve your own", self.hook("Bash", cmd, agent="general-purpose"))
        self.assertIsNone(self.hook("Bash", cmd, agent="tc-verifier"))
        for sub in ("human-approve TC-CTRL-001 --by x --reason y", "reset TC-CTRL-001"):
            self.assertIn("human override", self.hook(
                "Bash", {"command": "python3 .accepttc/tc_gate.py " + sub}, agent="tc-verifier"))
        self.assertIsNone(self.hook("Bash", {"command": "python3 .accepttc/tc_gate.py status"}))

    def test_hook_protects_approval_records(self):
        self.assertIn("by hand", self.hook("Write", {"file_path": self.p(".accepttc/registry.json")}))
        self.assertIn("by hand", self.hook(
            "Edit", {"file_path": self.p(".accepttc/verification/TC-CTRL-001/attempt-1.md")}))
        self.assertIsNotNone(self.hook("Bash", {"command": "echo '{}' > .accepttc/registry.json"}))
        self.assertIsNone(self.hook("Bash", {"command": "cat .accepttc/registry.json"}))
        self.assertIsNone(self.hook("Bash", {
            "command": "python3 - <<'E'\nprint('.accepttc/verification') if 2 > 1 else None\nE"}))

    # -- parallel verification ----------------------------------------------
    def many_cases(self, n):
        """A test file with n test cases and its trace record."""
        body = "#include <gtest/gtest.h>\n" + "".join(
            "TEST(Door, Case%d) {\n  EXPECT_EQ(door_ticks(), 3);\n}\n\n" % i for i in range(n))
        (self.root / "tests/door_test.cpp").write_text(body)
        cases = []
        for i in range(n):
            c = case("TC-DOOR-%03d" % (i + 1), "Door.Case%d" % i, 3, "A door cycle lasts 3 ticks.")
            c["oracle"] = c["oracle"][:1]
            cases.append(c)
        (self.root / "tests/door_test.trace.json").write_text(
            json.dumps({"test_file": "tests/door_test.cpp", "cases": cases}))
        self.tool("Read", {"file_path": self.p("docs/req.md")})
        self.tool("Write", {"file_path": self.p("tests/door_test.cpp")})
        self.tool("Write", {"file_path": self.p("tests/controller_test.cpp")})

    def test_plan_splits_pending_cases_over_verifiers(self):
        self.many_cases(8)
        code, out, _ = self.run_gate("plan")
        self.assertEqual(code, 0)
        jobs = [l.strip() for l in out.splitlines() if l.startswith("  tests/")]
        self.assertEqual(len(jobs), 4, out)                     # two files, split up to 4 verifiers
        ids = " ".join(jobs)
        for i in range(1, 9):
            self.assertEqual(ids.count("TC-DOOR-%03d" % i), 1, out)   # every case in exactly one job
        self.assertTrue(any(j.startswith("tests/controller_test.cpp ") and "--cases" not in j for j in jobs), out)
        _, out, _ = self.run_gate("plan", "--workers", "1")
        self.assertIn("wave 2", out)
        self.assertNotIn("--cases", out)

    def test_verifiers_sharing_a_file_record_separately(self):
        self.many_cases(4)
        code, out, _ = self.run_gate("brief", "tests/door_test.cpp", "--cases", "TC-DOOR-001", "TC-DOOR-002")
        self.assertEqual(code, 0, out)
        head = out.splitlines()
        self.assertIn(".accepttc/inbox/door_test.cpp.TC-DOOR-001.json", head[1])
        self.assertIn("--verdicts .accepttc/inbox/door_test.cpp.TC-DOOR-001.json", head[1])
        self.assertEqual(head[2], "judge: TC-DOOR-001, TC-DOOR-002")
        self.assertIn("not yours", out)
        self.assertIn("== TC-DOOR-002", out)
        self.assertNotIn("== TC-DOOR-003", out)
        # two verifiers record their shares at the same moment: no verdict is lost
        inbox = self.root / ".accepttc/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, TC_GATE_ROOT=str(self.root), TC_GATE_TRANSCRIPT_DIR=str(self.logs))
        procs = []
        for share in (["TC-DOOR-001", "TC-DOOR-002"], ["TC-DOOR-003", "TC-DOOR-004"]):
            f = inbox / ("door_test.cpp.%s.json" % share[0])
            f.write_text(json.dumps({"verdicts": [{"id": i, "verdict": "APPROVED", "findings": KO} for i in share]}))
            procs.append(subprocess.Popen(
                [sys.executable, str(SCRIPT), "record", "tests/door_test.cpp", "--verdicts",
                 str(f.relative_to(self.root))], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate()
            self.assertEqual(p.returncode, 0)
        st = self.status()
        self.assertEqual([st["TC-DOOR-%03d" % i] for i in range(1, 5)], ["APPROVED"] * 4)
        self.assertEqual(list(inbox.glob("*.json")), [])
        _, out, _ = self.run_gate("plan")
        self.assertNotIn("door_test", out)                      # nothing left to judge there

    def test_shared_sources_are_written_once(self):
        self.honest_session()
        c1 = case("TC-CTRL-001", "ControllerTest.TurnLastsThreeTicks", 3, "x")
        c2 = case("TC-CTRL-002", "ControllerTest.LeftHasPriority", 4, "x")
        for c in (c1, c2):
            c["sources"] = []
            for it in c["inputs"] + c["oracle"]:
                it["sources"] = ["CYCLE"]
        c2["oracle"][0]["sources"] = ["CYCLE", "PRIO"]
        shared = [{"ref": "CYCLE", "file": "docs/req.md", "lines": "3", "quote": "A door cycle lasts 3 ticks."},
                  {"ref": "PRIO", "file": "docs/req.md", "lines": "4", "quote": "the inner button has priority"}]
        path = self.root / "tests/controller_test.trace.json"
        path.write_text(json.dumps({"test_file": "tests/controller_test.cpp", "sources": shared, "cases": [c1, c2]}))
        _, res = self.check()
        self.assertEqual(res["cases"]["TC-CTRL-001"]["fails"], [])
        self.assertEqual([s["ref"] for s in res["cases"]["TC-CTRL-001"]["trace"]["sources"]], ["CYCLE"])
        self.assertEqual([s["ref"] for s in res["cases"]["TC-CTRL-002"]["trace"]["sources"]], ["CYCLE", "PRIO"])
        before = {k: v["hash"] for k, v in res["cases"].items()}
        # changing a shared source changes only the cases that use it
        shared[1]["quote"] = "If both the inner and the outer button are pressed, the inner button has priority."
        path.write_text(json.dumps({"test_file": "tests/controller_test.cpp", "sources": shared, "cases": [c1, c2]}))
        _, res = self.check()
        self.assertEqual(res["cases"]["TC-CTRL-001"]["hash"], before["TC-CTRL-001"])
        self.assertNotEqual(res["cases"]["TC-CTRL-002"]["hash"], before["TC-CTRL-002"])
        # a ref that is defined nowhere is still an error
        c1["oracle"][0]["sources"] = ["NOPE"]
        path.write_text(json.dumps({"test_file": "tests/controller_test.cpp", "sources": shared, "cases": [c1, c2]}))
        _, res = self.check()
        self.assertTrue(any("BAD_REF" in f for f in res["cases"]["TC-CTRL-001"]["fails"]))

    # -- more frameworks ----------------------------------------------------
    def test_cunit_and_cmocka_cases(self):
        names, _ = self.parse("cunit", "tests/test_door.c", (
            "#include <CUnit/CUnit.h>\nstatic int init_suite(void) { return 0; }\n"
            "void test_cycle(void)\n{\n    CU_ASSERT_EQUAL(door_ticks(), 3);\n    CU_ASSERT(door_closed());\n}\n"
            "static void testPriority(void) { CU_ASSERT_TRUE(inner_first()); }\n"
            "int main(void) { CU_add_test(s, \"cycle\", test_cycle); return 0; }\n"))
        self.assertEqual(names, {"test_cycle": 2, "testPriority": 1})
        names, _ = self.parse("cmocka", "tests/test_door.c", (
            "#include <cmocka.h>\nstatic int setup(void **state) { return 0; }\n"
            "static void cycle_lasts_three_ticks(void **state)\n{\n    (void)state;\n"
            "    assert_int_equal(door_ticks(), 3);\n    assert_true(door_closed());\n}\n"
            "static void helper(int x) { (void)x; }\n"
            "int main(void) { const struct CMUnitTest t[] = { cmocka_unit_test(cycle_lasts_three_ticks) };\n"
            "  return cmocka_run_group_tests(t, setup, NULL); }\n"))
        self.assertEqual(names, {"cycle_lasts_three_ticks": 2})

    # -- traceability graph -------------------------------------------------
    def test_graph_links_tests_to_the_code_they_call(self):
        self.honest_session()
        (self.root / "src/controller.c").write_text(
            "#include \"controller.h\"\n/* not_a_function(void) { } */\n"
            "static int clamp(int v) { return v < 0 ? 0 : v; }\n"
            "void rvc_init(rvc_t *c)\n{\n    c->n = clamp(0);\n}\n"
            "void rvc_front(rvc_t *c, bool on) { c->front = on; }\n"
            "dir_t rvc_tick(rvc_t *c) {\n    if (c->front) { return DIR_TURN_LEFT; }\n    return DIR_FORWARD;\n}\n")
        (self.root / "src/unused.c").write_text("int spare(void) { return 1; }\n")
        (self.root / "src/notes.md").write_text("rvc_tick(c) { not code }\n")
        self.assertEqual(self.run_gate("dashboard")[0], 0)
        html = (self.root / ".accepttc/verification/index.html").read_text()
        data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
        code = {f["file"]: [s["name"] for s in f["symbols"]] for f in data["graph"]["code"]}
        self.assertEqual(code, {"src/controller.c": ["clamp", "rvc_init", "rvc_front", "rvc_tick"],
                                "src/unused.c": ["spare"]})
        files = [f["file"] for f in data["graph"]["code"]]
        calls = {cid: sorted((files[i], name) for i, name in pairs) for cid, pairs in data["graph"]["calls"].items()}
        # rvc_init is reached through the fixture's SetUp(), not from the test body
        self.assertEqual(calls["TC-CTRL-001"], [("src/controller.c", "rvc_front"), ("src/controller.c", "rvc_init"),
                                                ("src/controller.c", "rvc_tick")])
        self.assertEqual(calls["TC-CTRL-002"], calls["TC-CTRL-001"])
        self.assertIn("src/unused.c", data["tree"])
        self.assertEqual(data["config"]["framework"], "gtest")
        self.assertIn("cmocka", data["frameworks"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
