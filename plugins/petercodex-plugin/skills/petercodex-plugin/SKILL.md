---
name: petercodex-plugin
description: Use OpenAI Codex as a supervised coding worker with durable plan, execute, and review phases. Trigger when the user asks to delegate coding to Codex or PeterCodex.
compatibility: Python 3.10+ and OpenAI Codex CLI with exec, exec resume, and exec review support.
---

# PeterCodex Plugin

Delegate coding work to Codex while keeping the invoking agent responsible for scope, safety, and verification.

PeterCodex deliberately separates **Plan**, **Execute**, and **Review**. It persists the exact Codex session ID plus a Git snapshot so execution can resume the planning context without silently operating on a changed repository.

## Core safety model

- Use the bundled `scripts/petercodex.py`; do not improvise `codex exec` flags when the wrapper can express the operation.
- Plan runs in `read-only` with `approval_policy="never"`.
- Execute resumes the exact plan session and switches to `workspace-write` with `approval_policy="never"`.
- Review is an independent read-only `codex exec review --uncommitted` pass.
- Never use `--dangerously-bypass-approvals-and-sandbox` through PeterCodex.
- Do not use `--last` to resume a session. PeterCodex always persists and resumes an explicit session ID.
- A timeout is `UNKNOWN`, not `FAILED`. Inspect PeterCodex status and repository state before any new execution attempt.
- PeterCodex removes inherited `CODEX_THREAD_ID` from delegated subprocesses so a nested Codex run cannot be correlated to the supervising thread by a stale environment value.
- Plan evidence and state live outside the target repository under `~/.petercodex/runs/` by default, so planning does not dirty the workspace.

## Preflight

Run:

```bash
python scripts/petercodex.py doctor
```

Require:

- Codex CLI is available.
- `codex login status` succeeds.
- Git is available for repository-bound work.

Resolve the exact repository path before delegation. Read applicable `AGENTS.md`, `CLAUDE.md`, task specifications, and project-specific constraints yourself, then include material requirements in the objective passed to PeterCodex.

## Plan mode

Use Plan when the user wants analysis, architecture, an implementation plan, or when implementation should not begin until a plan is reviewed.

```bash
python scripts/petercodex.py plan \
  --workspace /absolute/path/to/repo \
  --prompt "Implement the requested authentication fix and define the tests required."
```

Plan mode:

1. Captures Git HEAD and a digest of the complete porcelain status, including untracked files.
2. Starts a new Codex session in `read-only`.
3. Requires a structured plan response.
4. Persists the exact `thread_id` returned by `codex exec --json`.
5. Writes all evidence under a unique run directory.
6. Returns a machine-readable summary containing `run_id`, `session_id`, result paths, and state.

Do not treat a Plan result as approval to execute unless the user or supervising workflow has authorized implementation.

## Execute mode

Execute only an identified PeterCodex run:

```bash
python scripts/petercodex.py execute \
  --run-id <run-id>
```

Before resuming Codex, PeterCodex compares the current repository to the Plan snapshot. By default it refuses execution if HEAD or working-tree status changed after planning. This prevents an approved plan from being applied to silently changed code.

When drift is known and explicitly accepted, the supervising agent may use:

```bash
python scripts/petercodex.py execute \
  --run-id <run-id> \
  --allow-drift
```

Use `--allow-drift` only after inspecting the intervening changes and deciding that the original plan remains valid.

Execute resumes the exact persisted Codex session, applies `sandbox_mode="workspace-write"`, and asks Codex to implement and validate the already-planned work. The execution contract prohibits commit, push, destructive Git history changes, secret changes, and unrelated edits unless an explicit PeterCodex option permits a narrower exception.

If the requested task explicitly includes creating a local commit, pass `--allow-git-commit`. This never authorizes push, force-push, reset, rebase, or branch deletion.

## Review mode

After execution, prefer an independent review pass:

```bash
python scripts/petercodex.py review \
  --run-id <run-id>
```

Review uses `codex exec review --uncommitted` with a read-only sandbox and stores JSONL plus the final review message in the run evidence directory.

Treat review findings as evidence for the supervising agent. Independently inspect the most important findings before reporting them as verified defects.

## Status and reconciliation

Inspect durable state with:

```bash
python scripts/petercodex.py status --run-id <run-id>
```

Important states:

- `PLANNED`: a complete plan exists and the run has not executed.
- `EXECUTED`: execution completed without an unexpected HEAD change.
- `EXECUTED_WITH_HEAD_CHANGE`: Codex or another process changed HEAD unexpectedly; inspect history before proceeding.
- `PLAN_UNKNOWN`: planning timed out after a session may already have started.
- `EXECUTION_UNKNOWN`: execution timed out or its terminal state could not be proven.
- `REVIEWED`: independent review completed.

Never blindly rerun an `UNKNOWN` operation. First inspect:

- the run's `state.json`;
- current `git status` and `git log`;
- the saved JSONL/stderr evidence;
- whether the expected files/tests already changed or ran.

If another execution is needed after an ambiguous timeout, make that a deliberate recovery decision rather than a retry.

## Model selection

Plan accepts `--model <model>` when the user explicitly requests a model. The persisted session is then resumed for execution. Omit the model when no project/user requirement exists so Codex can use the configured default.

PeterCodex supports both Codex built-in local providers (`--local-provider ollama|lmstudio`) and per-run OpenAI-compatible providers. For ProxyCLI/LiteLLM-style gateways, pass `--provider-id`, `--provider-base-url`, `--provider-api-key-env`, and `--provider-wire-api responses|chat`. Persist only the environment-variable name; never write API-key values into the repository, run state, prompts, or evidence.

Execute and Review reuse the provider/model stored by Plan. Do not silently substitute a requested model or provider. If a provider returns fenced JSON instead of populating Codex's structured-output file, PeterCodex may normalize only the final agent message after validating that it is a JSON object with all required keys; all Git/acceptance evidence gates still apply.

## Delegation prompt contract

A good objective tells Codex:

- the exact outcome required;
- acceptance criteria;
- tests or checks that must pass;
- important repository instructions already discovered by the supervisor;
- scope exclusions.

The wrapper adds baseline constraints automatically. The supervising agent should still make the task-specific acceptance criteria explicit.

## Supervisor verification

After Execute:

1. Read PeterCodex's structured execution result.
2. Inspect `git status`, relevant diff, and unexpected untracked files.
3. Run or re-run the most important tests independently when practical.
4. Optionally run PeterCodex Review.
5. Report separately:
   - what Codex claimed;
   - what the supervisor independently verified;
   - remaining risks or blockers.

## Failure handling

- Missing Codex CLI: stop and report the dependency.
- Authentication failure: run `codex login` outside PeterCodex, then repeat only the operation that never started.
- Git drift before execution: stop unless the drift is explicitly reviewed and accepted.
- Timeout: mark `UNKNOWN`; do not infer failure.
- Nonzero Codex exit with terminal JSONL error: preserve evidence and report the failure.
- Invalid structured result: do not treat the turn as successful even if Codex exited zero.
- Unexpected HEAD change during execution: treat it as a safety exception and inspect Git history.
