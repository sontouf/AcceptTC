---
name: tc-approval
description: Write test cases with a traceable specification basis and get each one approved by the tc-verifier subagent before writing production code. Use whenever you add or change a test case, do TDD, or a hook says production code is locked by the TC gate.
---

# Test Case Approval Gate

Every test case must be approved by the `tc-verifier` subagent before
production code is written for it. A hook enforces this: edits to production
code are denied while any test case is not `APPROVED`. The verifier reads your
session log through Entire, so how you produce a test case matters as much as
what it contains.

Project settings are in `.accepttc/config.json`: the test framework, where
tests live (`test_globs`), which files count as specification (`spec_globs`),
which files are locked production code (`production_globs`), and the language
of explanations (`language`). Read it first. Run
`python3 .accepttc/tc_gate.py doctor` to see the effective settings.
You do not need to read `.accepttc/tc_gate.py` itself or look up `entire`
commands; this skill and the tool's output tell you what to do.

A test case is one test function or block in the project's framework (for
example one GoogleTest `TEST`, one pytest `test_*` function, one JUnit `@Test`
method, one Jest `it(...)`).

## Trace record

Each test file has a trace record beside it: the same path with the extension
replaced by `.trace.json` (`tests/foo_test.cpp` -> `tests/foo_test.trace.json`).
It has one entry per test case. Use `templates/test-case.trace.json` as the
form.

- `id`: `TC-<AREA>-<NNN>`, unique in the project. `test`: the test's name as
  `check` prints it.
- `sources`: the specification passages the test case is derived from. Each has
  a `ref`, the `file` (it must match `spec_globs`), the `lines`, and a `quote`
  copied verbatim from those lines. Quote the whole sentence or rule, not a
  fragment that reads differently out of context.
- Shared sources: a passage that several test cases of the file cite is written
  once, in a `sources` list at the top level of the trace file (next to
  `cases`). A case uses it by naming its `ref` in an input or oracle entry and
  does not repeat it. Give shared refs names that say what they are
  (`REQ-10`, `FSM-T04`), and keep `S1`, `S2` for a case's own sources.
- `inputs`: every input value and stimulus step, with the `rationale` for
  choosing it and the `sources` refs that justify it. This includes set-up
  steps and helper calls: a step without a source is rejected.
- `oracle`: one entry per assertion in the test body, with the `derivation`
  that leads from the cited passages to the expected value, and its `sources`.
- Write `objective`, `rationale`, `derivation` and assumption `text` in the
  configured `language` (`ko` = Korean): the human reads them on the
  verification web page. Copy each `quote` verbatim in the language of the
  specification, and keep `assertion` and `item` close to the code.
- `assumptions`: anything the specification does not determine. Do not fill a
  gap with a plausible value. Report it to the human; an assumption is usable
  only after the human approves it and it carries `approved_by`.

## Authoring rules

- Write test files and trace records yourself, with the Write/Edit tools. The
  session log Entire exposes covers the main agent only, so a test written by a
  subagent or by shell redirection has no visible history and is rejected.
- Read the specification passages with the Read tool before writing the test,
  even if you believe you remember them. A test case whose sources were not
  read first is rejected.
- Derive expected values from the specification only. Do not read production
  code to decide an expected value, and do not change an expected value because
  a test run produced something else.
- Never edit anything under `.accepttc/`, never record a verdict yourself, and
  never run `tc_gate.py human-approve` or `reset`. Those belong to the verifier
  and the human.

## Approval loop (at most 3 attempts per test case)

Production code stays locked while any test case is not `APPROVED`, so the
fast way through is to write a whole batch of tests first and have it verified
in parallel, not one file at a time.

1. Write the test files and trace records for everything you are about to
   implement: every module of the feature, or every module whose interface is
   already fixed. Read all the specification you need before the first test,
   in as few Read calls as possible.
2. Run `python3 .accepttc/tc_gate.py check <test_file>` on each file and fix
   every `FAIL`. Also fix `FLAG ASSERT_COUNT` (one oracle entry per assertion)
   and resolve every `FLAG ASSUMPTION` with the human: these are the usual
   reasons for a rejection, and a rejection costs a whole extra round. This
   step does not count as an attempt.
3. Run `python3 .accepttc/tc_gate.py plan`. It splits the test cases that need
   a verdict into up to 4 shares and prints one request per `tc-verifier`
   subagent. Spawn all of them in a single message so they run at the same
   time, each with its line as the whole request and nothing else. Do not tell
   a verifier the tests are correct and do not summarise them: it gets what it
   needs from `tc_gate.py brief`. When `plan` prints several waves, start the
   next wave when the previous one has reported.
4. While the verifiers run, do not edit the test files or trace records they
   are judging (a change makes their verdict `STALE`). Use the time for work
   that touches neither: the next batch of tests in other files, headers,
   build files.
5. When every test case is `APPROVED`, write the production code.
6. For each `REJECTED` test case, read
   `.accepttc/verification/<TC-ID>/attempt-<N>.md`, go back to the
   specification, and rewrite the test case and its trace entry. Fix all the
   rejected cases, then repeat from step 2: `plan` only lists what still needs
   a verdict.
7. After the third rejection a test case becomes `ESCALATED`. Stop working on
   it, show the human the reports, and wait for a decision.

Changing an approved test body, its trace entry, or the fixture code of its
file makes the approval `STALE`, and the test case must be verified again.
`python3 .accepttc/tc_gate.py status` shows the state of every test case.

## Verification page for the human

Every recorded verdict regenerates `.accepttc/verification/index.html`, a
self-contained web page. It opens on a traceability graph that links the cited
specification lines, the test cases and the production code they call, and it
shows, for each test case, the cited specification
text beside each input and expected value, the session-log timeline, the
mechanical checks, every verification attempt with the code that was judged,
and the code before and after each change. Run
`python3 .accepttc/tc_gate.py dashboard` to refresh it at any time. When you
report test cases to the human, give them the path of this page.

## Commits

The page uses git to show code before and after each change, so commit locally
at these points (never push unless the human asks):
1. after a batch of test cases is `APPROVED`: the test file, its trace record,
   and `.accepttc/registry.json` with `.accepttc/verification/` (RED);
2. after the tests pass: the production code (GREEN), and again after a
   refactor if there was one.
Keep test changes and production changes in separate commits.
