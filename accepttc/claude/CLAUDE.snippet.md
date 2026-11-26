<!-- accepttc:start -->
## Test Case Approval Gate (AcceptTC)
- Give every test case a trace record that cites the specification text for its inputs and expected values.
- Do not write production code for a test case until the `tc-verifier` subagent has approved it. A hook blocks production-code edits until then.
- Follow the `tc-approval` skill for the procedure. Settings: `.accepttc/config.json`. State of every test case: `python3 .accepttc/tc_gate.py status`.
<!-- accepttc:end -->
