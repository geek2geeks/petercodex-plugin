# PeterCodex Plugin

PeterCodex turns the OpenAI Codex CLI into a supervised coding worker with an explicit **Plan → Execute → Review** lifecycle.

It is designed for agent supervisors that need stronger guarantees than a one-shot `codex exec`: durable session IDs, sandbox transitions, Git-state drift detection, structured evidence, and timeout-safe handling.

## Why PeterCodex

A robust coding-agent handoff needs more than a prompt:

- **Plan is read-only.** PeterCodex starts Codex with `sandbox=read-only` and no approval escalation.
- **Execution resumes the exact planning session.** It captures the `thread_id` emitted by `codex exec --json` and resumes that ID explicitly.
- **Execution is bounded.** The resumed turn switches to `workspace-write`, never to full-access.
- **Stale plans are blocked.** PeterCodex records Git HEAD plus a digest of staged, unstaged, and untracked state; execution refuses if the repository changed after planning unless drift is explicitly accepted.
- **Timeout is UNKNOWN.** A timeout is never silently converted into failure and never triggers an automatic retry.
- **Results are structured.** Plan and execution turns use JSON schemas and preserve the complete Codex JSONL stream plus stderr.
- **Review is independent.** `codex exec review --uncommitted` runs as a separate read-only pass.
- **No dangerous bypass.** PeterCodex never uses `--dangerously-bypass-approvals-and-sandbox`.

## Requirements

- Python 3.10+
- Git
- OpenAI Codex CLI with `exec`, `exec resume`, and `exec review`
- A working Codex authentication or compatible configured provider

Check readiness:

```bash
python plugins/petercodex-plugin/scripts/petercodex.py doctor
```

## Install from GitHub

Add this repository as a Codex marketplace:

```bash
codex plugin marketplace add geek2geeks/petercodex-plugin
codex plugin add petercodex-plugin@petercodex
```

Then start a new Codex thread so the installed skill metadata is picked up.

## Direct CLI usage

### 1. Plan

```bash
python plugins/petercodex-plugin/scripts/petercodex.py plan \
  --workspace /absolute/path/to/repo \
  --prompt "Implement the requested fix and define the validation required."
```

PeterCodex returns JSON containing a `run_id` and exact Codex `session_id`.

### 2. Execute the approved plan

```bash
python plugins/petercodex-plugin/scripts/petercodex.py execute \
  --run-id <run-id>
```

If the repository changed since planning, execution is refused. After inspecting those changes, an operator may explicitly accept drift:

```bash
python plugins/petercodex-plugin/scripts/petercodex.py execute \
  --run-id <run-id> \
  --allow-drift
```

### 3. Review

```bash
python plugins/petercodex-plugin/scripts/petercodex.py review \
  --run-id <run-id>
```

### 4. Inspect durable state

```bash
python plugins/petercodex-plugin/scripts/petercodex.py status \
  --run-id <run-id>
```

Run evidence is stored under `~/.petercodex/runs/<run-id>/` unless `PETERCODEX_HOME` or `--home` overrides it.

## State machine

```text
PLAN_RUNNING
   ├── PLANNED ──> EXECUTION_RUNNING ──> EXECUTED ──> REVIEW_RUNNING ──> REVIEWED
   ├── PLAN_BLOCKED
   ├── PLAN_FAILED
   └── PLAN_UNKNOWN

EXECUTION_RUNNING
   ├── EXECUTED
   ├── EXECUTED_WITH_HEAD_CHANGE
   ├── EXECUTION_FAILED
   └── EXECUTION_UNKNOWN
```

`*_UNKNOWN` is deliberately different from `*_FAILED`: the caller must reconcile evidence and repository state before attempting recovery.

## Evidence layout

```text
~/.petercodex/runs/<run-id>/
├── state.json
├── plan.jsonl
├── plan.stderr.log
├── plan-result.json
├── execute.jsonl
├── execute.stderr.log
├── execute-result.json
├── review.jsonl
├── review.stderr.log
└── review-result.txt
```

The target repository is not used for PeterCodex control-plane state.

## Safety properties

PeterCodex deliberately does **not**:

- resume the “latest” Codex session;
- silently tolerate Git drift;
- auto-retry timed-out execution;
- grant danger-full-access;
- bypass approvals and sandboxing;
- auto-approve MCP servers;
- authorize push, force-push, reset, rebase, branch deletion, deployment, or secret mutation.

A local Git commit can be explicitly allowed with `--allow-git-commit`; this never authorizes push or history rewriting.

## Current Codex primitives used

PeterCodex relies on public Codex CLI capabilities:

- `codex exec --json`
- `-s read-only`
- `-s workspace-write` / resumed `sandbox_mode="workspace-write"`
- `codex exec resume <SESSION_ID>`
- `codex exec review --uncommitted`
- `--output-schema`
- `--output-last-message`

OpenAI's current SDK documentation also exposes per-thread/per-turn `read_only`, `workspace_write`, and `full_access` sandbox presets. PeterCodex intentionally stays on the CLI surface to avoid adding an SDK dependency.

Official references:

- https://github.com/openai/codex
- https://github.com/openai/codex/tree/main/sdk/python

## Tests

Deterministic tests:

```bash
python -m unittest discover -s tests -v
```

Authenticated live smoke test:

```bash
python tests/live_smoke.py
```

The live smoke creates a disposable Git repository, verifies that Plan does not dirty it, executes a one-file change through the same persisted Codex session, checks the exact resulting file and Git status, and runs an independent Review.

## License

MIT
