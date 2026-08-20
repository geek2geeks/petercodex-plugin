# Security Policy

PeterCodex is an orchestration wrapper around the OpenAI Codex CLI. It does not add its own network service or credential store.

## Security model

- Plan uses a read-only Codex sandbox.
- Execute uses workspace-write, never danger-full-access.
- PeterCodex never passes `--dangerously-bypass-approvals-and-sandbox`.
- Durable state is stored under `~/.petercodex/` by default, outside target repositories.
- The wrapper records session IDs and Git metadata, but never intentionally records Codex authentication tokens.
- Inherited `CODEX_THREAD_ID` is removed from delegated subprocess environments to avoid stale nested-thread correlation.

## Reporting a vulnerability

Please open a GitHub Security Advisory for this repository when possible. Do not publish credentials, tokens, private source code, or exploit details in a public issue.

## Scope warning

PeterCodex cannot make arbitrary model-generated commands safe. Operators remain responsible for repository trust, project instructions, dependencies, external tools, MCP servers, and any explicit permission escalation performed outside PeterCodex.
