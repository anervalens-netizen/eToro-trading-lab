# ExecPlan rules

Use an ExecPlan for substantial, multi-session, multi-component, or high-risk work. Keep one canonical active plan per objective under `docs/exec-plans/active/` and archive it only after every acceptance criterion has independent PASS evidence, relevant regression gates pass, and required deployment proof exists.

Task states: `BACKLOG -> READY -> BUILDING -> VERIFYING -> PASS`; any state may become `BLOCKED`. Independent auditor verdicts are `PASS`, `FAIL`, or `BLOCKED`. Never store credentials, private data, or sensitive runtime payloads in plans or evidence.
