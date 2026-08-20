#!/usr/bin/env python3
"""PeterCodex: supervised Plan -> Execute -> Review orchestration for Codex CLI.

The wrapper intentionally uses only the Python standard library and public Codex
CLI surfaces. Durable run evidence is kept outside the target repository.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, NamedTuple

PLUGIN_VERSION = "0.1.1"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = PLUGIN_ROOT / "schemas"
DEFAULT_HOME = Path(os.environ.get("PETERCODEX_HOME", Path.home() / ".petercodex"))

EXIT_OK = 0
EXIT_DEPENDENCY = 10
EXIT_STATE = 20
EXIT_DRIFT = 21
EXIT_PLAN_UNKNOWN = 30
EXIT_PLAN_FAILED = 31
EXIT_EXECUTION_UNKNOWN = 40
EXIT_EXECUTION_FAILED = 41
EXIT_HEAD_CHANGED = 42
EXIT_REVIEW_UNKNOWN = 50
EXIT_REVIEW_FAILED = 51


class PeterCodexError(RuntimeError):
    """Expected user-facing PeterCodex error."""


class ProcessResult(NamedTuple):
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def make_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise PeterCodexError(f"Path does not exist: {path}")
    return path


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeterCodexError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PeterCodexError(f"Expected a JSON object in {path}")
    return value


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def resolve_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise PeterCodexError(f"Required executable not found on PATH: {name}")
    return executable


def build_invocation(executable: str, args: list[str]) -> list[str]:
    suffix = Path(executable).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command_line = subprocess.list2cmdline([executable, *args])
        return [comspec, "/d", "/s", "/c", command_line]
    if os.name == "nt" and suffix == ".ps1":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise PeterCodexError("PowerShell is required to launch the resolved Codex shim")
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable, *args]
    return [executable, *args]


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def run_process(
    executable: str,
    args: list[str],
    *,
    cwd: Path | None,
    timeout_seconds: int,
    sanitize_codex_thread: bool = False,
) -> ProcessResult:
    env = os.environ.copy()
    if sanitize_codex_thread:
        env.pop("CODEX_THREAD_ID", None)

    invocation = build_invocation(executable, args)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True

    process = subprocess.Popen(invocation, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return ProcessResult(process.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process)
        trailing_stdout, trailing_stderr = process.communicate()
        stdout = (exc.stdout or "") + (trailing_stdout or "")
        stderr = (exc.stderr or "") + (trailing_stderr or "")
        return ProcessResult(None, stdout, stderr, True)


_CODEX_COMMAND_CACHE: list[str] | None = None


def _candidate_codex_commands() -> list[list[str]]:
    """Return plausible Codex launch commands in preference order.

    The npm installer can transiently leave the package under an atomic staging
    directory such as ``@openai/.codex-<suffix>`` while a generated shim still
    points at ``@openai/codex``. PeterCodex treats that as a recoverable local
    packaging problem and can launch the staged public CLI entrypoint directly.
    """

    commands: list[list[str]] = []
    override = os.environ.get("PETERCODEX_CODEX")
    node = shutil.which("node")

    if override:
        override_path = Path(override).expanduser()
        if override_path.suffix.lower() == ".js":
            if node:
                commands.append([node, str(override_path)])
        else:
            commands.append([str(override_path)])

    path_candidate = shutil.which("codex")
    if path_candidate:
        commands.append([path_candidate])

    if node:
        npm_roots: list[Path] = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm_roots.append(Path(appdata) / "npm" / "node_modules" / "@openai")

        for npm_root in npm_roots:
            standard = npm_root / "codex" / "bin" / "codex.js"
            if standard.is_file():
                commands.append([node, str(standard)])

            staged = [path for path in npm_root.glob(".codex-*/bin/codex.js") if path.is_file()]
            staged.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            commands.extend([node, str(path)] for path in staged)

    deduplicated: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        key = tuple(os.path.normcase(os.path.abspath(part)) if index == 0 else part for index, part in enumerate(command))
        if key not in seen:
            seen.add(key)
            deduplicated.append(command)
    return deduplicated


def resolve_codex_command() -> list[str]:
    global _CODEX_COMMAND_CACHE
    if _CODEX_COMMAND_CACHE is not None:
        return list(_CODEX_COMMAND_CACHE)

    attempts: list[str] = []
    for command in _candidate_codex_commands():
        try:
            probe = run_process(
                command[0],
                [*command[1:], "--version"],
                cwd=None,
                timeout_seconds=20,
                sanitize_codex_thread=True,
            )
        except OSError as exc:
            attempts.append(f"{' '.join(command)} -> {exc}")
            continue

        version_text = (probe.stdout or probe.stderr).strip()
        if not probe.timed_out and probe.returncode == 0 and version_text:
            _CODEX_COMMAND_CACHE = list(command)
            return list(command)
        attempts.append(
            f"{' '.join(command)} -> returncode={probe.returncode} timed_out={probe.timed_out} {version_text[:160]}"
        )

    detail = "; ".join(attempts) if attempts else "no candidates found"
    raise PeterCodexError(
        "No working Codex CLI invocation found. "
        "Set PETERCODEX_CODEX to a working codex executable or codex.js path if needed. "
        f"Attempts: {detail}"
    )


def run_codex(args: list[str], *, cwd: Path | None, timeout_seconds: int) -> ProcessResult:
    command = resolve_codex_command()
    return run_process(
        command[0],
        [*command[1:], *args],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        sanitize_codex_thread=True,
    )


def run_git(args: list[str], *, cwd: Path, timeout_seconds: int = 20) -> subprocess.CompletedProcess[bytes]:
    executable = resolve_executable("git")
    return subprocess.run(
        [executable, *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )


def require_git_success(result: subprocess.CompletedProcess[bytes], operation: str) -> bytes:
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PeterCodexError(f"Git {operation} failed: {stderr or 'unknown error'}")
    return result.stdout


def git_changed_paths(root: Path) -> list[str]:
    commands = [
        (["diff", "--name-only"], "unstaged path lookup"),
        (["diff", "--cached", "--name-only"], "staged path lookup"),
        (["ls-files", "--others", "--exclude-standard"], "untracked path lookup"),
    ]
    paths: set[str] = set()
    for command, operation in commands:
        raw = require_git_success(run_git(command, cwd=root), operation)
        for line in raw.decode("utf-8", errors="replace").splitlines():
            value = line.strip().replace("\\", "/")
            if value:
                paths.add(value)
    return sorted(paths)


def git_snapshot(workspace: Path) -> dict[str, Any]:
    root_raw = require_git_success(run_git(["rev-parse", "--show-toplevel"], cwd=workspace), "root lookup")
    root = Path(root_raw.decode("utf-8", errors="replace").strip()).resolve()
    head_raw = require_git_success(run_git(["rev-parse", "HEAD"], cwd=root), "HEAD lookup")
    status_raw = require_git_success(
        run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root),
        "status lookup",
    )
    short_raw = require_git_success(
        run_git(["status", "--short", "--branch", "--untracked-files=all"], cwd=root),
        "status summary",
    )
    return {
        "git_root": str(root),
        "head": head_raw.decode("utf-8", errors="replace").strip(),
        "status_digest": hashlib.sha256(status_raw).hexdigest(),
        "status_short": short_raw.decode("utf-8", errors="replace").strip(),
        "changed_paths": git_changed_paths(root),
    }


def drift_details(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, bool]:
    return {
        "git_root_changed": baseline.get("git_root") != current.get("git_root"),
        "head_changed": baseline.get("head") != current.get("head"),
        "working_tree_changed": baseline.get("status_digest") != current.get("status_digest"),
    }


def has_drift(drift: dict[str, bool]) -> bool:
    return any(drift.values())


def execution_file_evidence(
    baseline: dict[str, Any], after: dict[str, Any], structured: dict[str, Any]
) -> dict[str, Any]:
    baseline_paths = baseline.get("changed_paths")
    if not isinstance(baseline_paths, list) or baseline_paths:
        return {"enforced": False, "reason": "execution baseline was not a proven-clean working tree"}

    reported_raw = structured.get("files_changed")
    if not isinstance(reported_raw, list):
        return {"enforced": True, "matches": False, "error": "files_changed is not a list"}

    reported = sorted(
        {
            str(path).strip().replace("\\", "/")
            for path in reported_raw
            if isinstance(path, str) and str(path).strip()
        }
    )
    actual_raw = after.get("changed_paths")
    actual = sorted(str(path).replace("\\", "/") for path in actual_raw) if isinstance(actual_raw, list) else []
    return {
        "enforced": True,
        "matches": reported == actual,
        "reported": reported,
        "actual": actual,
    }


def execution_result_evidence(structured: dict[str, Any]) -> dict[str, Any]:
    criteria_raw = structured.get("acceptance_criteria")
    tests_raw = structured.get("tests")
    criteria = criteria_raw if isinstance(criteria_raw, list) else []
    tests = tests_raw if isinstance(tests_raw, list) else []

    non_met = [
        item
        for item in criteria
        if not isinstance(item, dict) or item.get("status") != "met"
    ]
    failed_tests = [
        item
        for item in tests
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    return {
        "criteria_count": len(criteria),
        "all_criteria_met": bool(criteria) and not non_met,
        "non_met_criteria": non_met,
        "failed_tests": failed_tests,
        "passes": bool(criteria) and not non_met and not failed_tests,
    }


def parse_jsonl(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    invalid_lines: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            invalid_lines.append(line)
    return events, invalid_lines


def thread_id_from_events(events: Iterable[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def terminal_event(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        event_type = event.get("type")
        if event_type in {"turn.completed", "turn.failed", "error"}:
            return str(event_type)
    return None


def save_process_evidence(run_dir: Path, phase: str, result: ProcessResult) -> tuple[list[dict[str, Any]], list[str]]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{phase}.jsonl").write_text(result.stdout, encoding="utf-8", newline="\n")
    (run_dir / f"{phase}.stderr.log").write_text(result.stderr, encoding="utf-8", newline="\n")
    events, invalid_lines = parse_jsonl(result.stdout)
    return events, invalid_lines


def validate_structured_value(value: Any, required_keys: set[str], source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PeterCodexError(f"Structured result from {source} is not a JSON object")
    missing = sorted(required_keys - set(value.keys()))
    if missing:
        raise PeterCodexError(f"Structured result from {source} is missing required keys: {', '.join(missing)}")
    return value


def validate_structured_result(path: Path, required_keys: set[str]) -> dict[str, Any]:
    return validate_structured_value(load_json(path), required_keys, str(path))


def last_agent_message(events: Iterable[dict[str, Any]]) -> str | None:
    messages: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    return messages[-1] if messages else None


def parse_structured_agent_message(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            candidate = candidate[first_newline + 1:last_fence].strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        first_brace = candidate.find("{")
        last_brace = candidate.rfind("}")
        if first_brace < 0 or last_brace <= first_brace:
            raise PeterCodexError("Agent message did not contain a JSON object")
        try:
            value = json.loads(candidate[first_brace:last_brace + 1])
        except json.JSONDecodeError as exc:
            raise PeterCodexError(f"Agent message JSON fallback failed: {exc}") from exc
    if not isinstance(value, dict):
        raise PeterCodexError("Agent message structured fallback is not a JSON object")
    return value


def load_structured_result(
    path: Path,
    events: Iterable[dict[str, Any]],
    required_keys: set[str],
) -> dict[str, Any]:
    try:
        return validate_structured_result(path, required_keys)
    except PeterCodexError as file_error:
        message = last_agent_message(events)
        if not message:
            raise file_error
        value = validate_structured_value(
            parse_structured_agent_message(message),
            required_keys,
            "last agent_message fallback",
        )
        atomic_write_json(path, value)
        return value


def state_path(home: Path, run_id: str) -> Path:
    return home / "runs" / run_id / "state.json"


def load_state(home: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    path = state_path(home, run_id)
    if not path.exists():
        raise PeterCodexError(f"Unknown PeterCodex run: {run_id}")
    return path.parent, load_json(path)


def codex_provider_args(
    model: str | None,
    local_provider: str | None = None,
    provider: dict[str, str] | None = None,
) -> list[str]:
    if local_provider and provider:
        raise PeterCodexError("--local-provider cannot be combined with a custom model provider")

    args: list[str] = []
    if local_provider:
        args.extend(["-c", f"model_provider={json.dumps(local_provider)}"])
    elif provider:
        provider_id = provider.get("id", "").strip()
        base_url = provider.get("base_url", "").strip()
        api_key_env = provider.get("api_key_env", "").strip()
        wire_api = provider.get("wire_api", "responses").strip()
        if not provider_id or not provider_id.replace("_", "").isalnum():
            raise PeterCodexError("Custom provider id must contain only letters, numbers, and underscores")
        if not base_url:
            raise PeterCodexError("Custom provider requires a base URL")
        if wire_api not in {"responses", "chat"}:
            raise PeterCodexError("Custom provider wire_api must be 'responses' or 'chat'")
        if api_key_env and not os.environ.get(api_key_env):
            raise PeterCodexError(f"Custom provider API-key environment variable is not set: {api_key_env}")

        prefix = f"model_providers.{provider_id}"
        args.extend(
            [
                "-c",
                f"model_provider={json.dumps(provider_id)}",
                "-c",
                f"{prefix}.name={json.dumps(provider.get('name') or provider_id)}",
                "-c",
                f"{prefix}.base_url={json.dumps(base_url)}",
                "-c",
                f"{prefix}.wire_api={json.dumps(wire_api)}",
                "-c",
                f"{prefix}.requires_openai_auth=false",
            ]
        )
        if api_key_env:
            args.extend(["-c", f"{prefix}.env_key={json.dumps(api_key_env)}"])
    if model:
        args.extend(["-m", model])
    return args


def provider_from_plan_args(args: argparse.Namespace) -> dict[str, str] | None:
    supplied = any(
        [
            args.provider_id,
            args.provider_name,
            args.provider_base_url,
            args.provider_api_key_env,
            args.provider_wire_api,
        ]
    )
    if not supplied:
        return None
    if not args.provider_id or not args.provider_base_url:
        raise PeterCodexError("Custom provider requires --provider-id and --provider-base-url")
    return {
        "id": args.provider_id,
        "name": args.provider_name or args.provider_id,
        "base_url": args.provider_base_url,
        "api_key_env": args.provider_api_key_env or "",
        "wire_api": args.provider_wire_api or "responses",
    }


def codex_version(timeout_seconds: int = 20) -> str | None:
    try:
        result = run_codex(["--version"], cwd=None, timeout_seconds=timeout_seconds)
    except PeterCodexError:
        return None
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip() or None


def plan_prompt(objective: str) -> str:
    return f"""You are the planning phase of PeterCodex, operating under a supervising agent.

Objective:
{objective}

Rules for this turn:
- This is PLAN ONLY. Do not edit, create, rename, or delete files.
- Inspect the repository and all applicable AGENTS.md / CLAUDE.md / project instructions.
- Identify the smallest coherent implementation that satisfies the objective.
- Do not commit, push, rebase, reset, change branches, alter secrets, or deploy.
- Make assumptions explicit and identify blockers instead of silently guessing.
- Define concrete validation/tests for the implementation.
- Set ready_to_execute=false if material ambiguity or a blocker makes execution unsafe.
- Return only the structured result required by the supplied output schema.
"""


def execute_prompt(objective: str, allow_git_commit: bool) -> str:
    commit_rule = (
        "- A local Git commit is explicitly allowed after validation, but push, force-push, rebase, reset, and branch deletion remain forbidden."
        if allow_git_commit
        else "- Do not commit, push, force-push, rebase, reset, change branches, or delete branches."
    )
    return f"""You are the execution phase of PeterCodex. Resume the plan you produced in the previous turn and implement it.

Original objective:
{objective}

Execution rules:
- Follow the previously produced plan unless repository evidence proves a bounded correction is necessary.
- Stay inside the repository and respect all applicable AGENTS.md / CLAUDE.md / project instructions.
- Make the smallest coherent changes required by the objective.
- Do not modify credentials, secrets, account settings, or unrelated files.
{commit_rule}
- Run the most relevant available tests/checks after editing.
- If a required action is blocked by the sandbox or permissions, report the blocker; do not bypass the sandbox.
- Return only the structured result required by the supplied output schema.
"""


def cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "petercodex_version": PLUGIN_VERSION,
        "python": sys.version.split()[0],
        "codex_available": False,
        "codex_command": None,
        "codex_version": None,
        "codex_login_ok": False,
        "codex_login_message": None,
        "git_available": shutil.which("git") is not None,
        "home": str(args.home),
    }
    try:
        command = resolve_codex_command()
        report["codex_available"] = True
        report["codex_command"] = command
        report["codex_version"] = codex_version()
        login = run_codex(["login", "status"], cwd=None, timeout_seconds=20)
        report["codex_login_ok"] = not login.timed_out and login.returncode == 0
        report["codex_login_message"] = (login.stdout or login.stderr).strip()
    except PeterCodexError as exc:
        report["codex_login_message"] = str(exc)
    emit(report)
    return EXIT_OK if report["codex_available"] and report["codex_login_ok"] and report["git_available"] else EXIT_DEPENDENCY


def cmd_plan(args: argparse.Namespace) -> int:
    workspace = canonical_path(args.workspace)
    baseline = git_snapshot(workspace)
    provider = provider_from_plan_args(args)
    run_id = make_run_id()
    run_dir = args.home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    state_file = run_dir / "state.json"
    plan_result_path = run_dir / "plan-result.json"

    state: dict[str, Any] = {
        "format_version": 1,
        "petercodex_version": PLUGIN_VERSION,
        "run_id": run_id,
        "state": "PLAN_RUNNING",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "workspace": str(workspace),
        "objective": args.prompt,
        "objective_sha256": sha256_text(args.prompt),
        "model": args.model,
        "local_provider": args.local_provider,
        "provider": provider,
        "codex_version": codex_version(),
        "plan_baseline": baseline,
        "session_id": None,
    }
    atomic_write_json(state_file, state)

    codex_args = [
        "exec",
        "-C",
        str(workspace),
        "-s",
        "read-only",
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(plan_result_path),
        "--output-schema",
        str(SCHEMA_DIR / "plan-result.schema.json"),
    ]
    codex_args.extend(codex_provider_args(args.model, args.local_provider, provider))
    codex_args.append(plan_prompt(args.prompt))

    result = run_codex(codex_args, cwd=workspace, timeout_seconds=args.timeout_minutes * 60)
    events, invalid_lines = save_process_evidence(run_dir, "plan", result)
    session_id = thread_id_from_events(events)
    state["session_id"] = session_id
    state["plan_terminal_event"] = terminal_event(events)
    state["plan_invalid_jsonl_lines"] = len(invalid_lines)
    state["updated_at"] = utc_now()

    if result.timed_out:
        state["state"] = "PLAN_UNKNOWN"
        state["message"] = "Planning timed out. Treat the operation as ambiguous; do not blindly retry."
        atomic_write_json(state_file, state)
        emit({"run_id": run_id, "state": state["state"], "session_id": session_id, "run_dir": str(run_dir)})
        return EXIT_PLAN_UNKNOWN

    if result.returncode != 0 or state["plan_terminal_event"] in {"turn.failed", "error"}:
        state["state"] = "PLAN_FAILED"
        state["returncode"] = result.returncode
        atomic_write_json(state_file, state)
        emit({"run_id": run_id, "state": state["state"], "session_id": session_id, "run_dir": str(run_dir)})
        return EXIT_PLAN_FAILED

    if not session_id:
        state["state"] = "PLAN_FAILED"
        state["message"] = "Codex completed without a thread.started thread_id; durable resume is impossible."
        atomic_write_json(state_file, state)
        emit({"run_id": run_id, "state": state["state"], "run_dir": str(run_dir)})
        return EXIT_PLAN_FAILED

    try:
        structured = load_structured_result(
            plan_result_path,
            events,
            {"summary", "assumptions", "steps", "validation", "risks", "ready_to_execute"},
        )
    except PeterCodexError as exc:
        state["state"] = "PLAN_FAILED"
        state["message"] = str(exc)
        atomic_write_json(state_file, state)
        emit({"run_id": run_id, "state": state["state"], "session_id": session_id, "run_dir": str(run_dir)})
        return EXIT_PLAN_FAILED

    state["state"] = "PLANNED" if structured.get("ready_to_execute") is True else "PLAN_BLOCKED"
    state["plan_result"] = str(plan_result_path)
    atomic_write_json(state_file, state)
    emit(
        {
            "run_id": run_id,
            "state": state["state"],
            "session_id": session_id,
            "workspace": str(workspace),
            "run_dir": str(run_dir),
            "plan_result": structured,
        }
    )
    return EXIT_OK if state["state"] == "PLANNED" else EXIT_STATE


def cmd_execute(args: argparse.Namespace) -> int:
    run_dir, state = load_state(args.home, args.run_id)
    state_file = run_dir / "state.json"
    if state.get("state") != "PLANNED":
        raise PeterCodexError(f"Run {args.run_id} is not executable from state {state.get('state')}")
    session_id = state.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise PeterCodexError("Plan state has no durable Codex session ID")

    workspace = canonical_path(state["workspace"])
    current = git_snapshot(workspace)
    drift = drift_details(state["plan_baseline"], current)
    if has_drift(drift) and not args.allow_drift:
        emit(
            {
                "run_id": args.run_id,
                "state": state["state"],
                "error": "Repository changed after planning; execution refused.",
                "drift": drift,
                "current_status": current["status_short"],
            }
        )
        return EXIT_DRIFT

    state["state"] = "EXECUTION_RUNNING"
    state["execution_started_at"] = utc_now()
    state["execution_baseline"] = current
    state["execution_drift_accepted"] = bool(has_drift(drift) and args.allow_drift)
    state["allow_git_commit"] = bool(args.allow_git_commit)
    state["updated_at"] = utc_now()
    atomic_write_json(state_file, state)

    execute_result_path = run_dir / "execute-result.json"
    codex_args = [
        "exec",
        "resume",
        *codex_provider_args(
            str(state["model"]) if state.get("model") else None,
            str(state["local_provider"]) if state.get("local_provider") else None,
            state.get("provider") if isinstance(state.get("provider"), dict) else None,
        ),
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(execute_result_path),
        "--output-schema",
        str(SCHEMA_DIR / "execute-result.schema.json"),
    ]
    codex_args.extend(
        [
            session_id,
            execute_prompt(state["objective"], args.allow_git_commit),
        ]
    )

    result = run_codex(codex_args, cwd=workspace, timeout_seconds=args.timeout_minutes * 60)
    events, invalid_lines = save_process_evidence(run_dir, "execute", result)
    after = git_snapshot(workspace)
    state["execution_terminal_event"] = terminal_event(events)
    state["execution_invalid_jsonl_lines"] = len(invalid_lines)
    state["execution_post_snapshot"] = after
    state["execution_finished_at"] = utc_now()
    state["updated_at"] = utc_now()

    if result.timed_out:
        state["state"] = "EXECUTION_UNKNOWN"
        state["message"] = "Execution timed out. Inspect repository and saved evidence before any recovery action."
        atomic_write_json(state_file, state)
        emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir), "post_status": after["status_short"]})
        return EXIT_EXECUTION_UNKNOWN

    if result.returncode != 0 or state["execution_terminal_event"] in {"turn.failed", "error"}:
        state["state"] = "EXECUTION_FAILED"
        state["returncode"] = result.returncode
        atomic_write_json(state_file, state)
        emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir), "post_status": after["status_short"]})
        return EXIT_EXECUTION_FAILED

    try:
        structured = load_structured_result(
            execute_result_path,
            events,
            {"summary", "files_changed", "commands", "tests", "acceptance_criteria", "risks"},
        )
    except PeterCodexError as exc:
        state["state"] = "EXECUTION_FAILED"
        state["message"] = str(exc)
        atomic_write_json(state_file, state)
        emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir)})
        return EXIT_EXECUTION_FAILED

    result_evidence = execution_result_evidence(structured)
    state["execution_result_evidence"] = result_evidence
    if result_evidence.get("passes") is not True:
        state["state"] = "EXECUTION_FAILED"
        state["message"] = "Execution result did not prove all acceptance criteria and tests succeeded."
        atomic_write_json(state_file, state)
        emit(
            {
                "run_id": args.run_id,
                "state": state["state"],
                "run_dir": str(run_dir),
                "result_evidence": result_evidence,
            }
        )
        return EXIT_EXECUTION_FAILED

    file_evidence = execution_file_evidence(current, after, structured)
    state["execution_file_evidence"] = file_evidence
    if file_evidence.get("enforced") is True and file_evidence.get("matches") is not True:
        state["state"] = "EXECUTION_FAILED"
        state["message"] = "Codex-reported files_changed does not match the Git-observed execution delta."
        atomic_write_json(state_file, state)
        emit(
            {
                "run_id": args.run_id,
                "state": state["state"],
                "run_dir": str(run_dir),
                "file_evidence": file_evidence,
            }
        )
        return EXIT_EXECUTION_FAILED

    unexpected_head_change = current["head"] != after["head"] and not args.allow_git_commit
    state["state"] = "EXECUTED_WITH_HEAD_CHANGE" if unexpected_head_change else "EXECUTED"
    state["execute_result"] = str(execute_result_path)
    atomic_write_json(state_file, state)
    emit(
        {
            "run_id": args.run_id,
            "state": state["state"],
            "session_id": session_id,
            "run_dir": str(run_dir),
            "post_status": after["status_short"],
            "execute_result": structured,
        }
    )
    return EXIT_HEAD_CHANGED if unexpected_head_change else EXIT_OK


def cmd_review(args: argparse.Namespace) -> int:
    run_dir, state = load_state(args.home, args.run_id)
    state_file = run_dir / "state.json"
    current_state = state.get("state")
    reviewable_states = {"EXECUTED", "EXECUTED_WITH_HEAD_CHANGE", "REVIEWED"}
    if current_state == "REVIEW_FAILED" and state.get("pre_review_state") in reviewable_states:
        current_state = state.get("pre_review_state")
    if current_state not in reviewable_states:
        raise PeterCodexError(f"Run {args.run_id} cannot be reviewed from state {state.get('state')}")
    workspace = canonical_path(state["workspace"])
    review_result_path = run_dir / "review-result.txt"

    review_model = args.model or state.get("model")
    local_provider = state.get("local_provider")
    provider = state.get("provider") if isinstance(state.get("provider"), dict) else None
    codex_args = [
        "exec",
        "review",
        "--uncommitted",
        *codex_provider_args(
            str(review_model) if review_model else None,
            str(local_provider) if local_provider else None,
            provider,
        ),
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(review_result_path),
    ]

    previous_state = current_state
    for stale_key in ("review_terminal_event", "review_finished_at", "returncode"):
        state.pop(stale_key, None)
    state["state"] = "REVIEW_RUNNING"
    state["review_started_at"] = utc_now()
    state["updated_at"] = utc_now()
    atomic_write_json(state_file, state)

    result = run_codex(codex_args, cwd=workspace, timeout_seconds=args.timeout_minutes * 60)
    events, invalid_lines = save_process_evidence(run_dir, "review", result)
    state["review_terminal_event"] = terminal_event(events)
    state["review_invalid_jsonl_lines"] = len(invalid_lines)
    state["review_finished_at"] = utc_now()
    state["updated_at"] = utc_now()

    if result.timed_out:
        state["state"] = "REVIEW_UNKNOWN"
        state["pre_review_state"] = previous_state
        atomic_write_json(state_file, state)
        emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir)})
        return EXIT_REVIEW_UNKNOWN

    if result.returncode != 0 or state["review_terminal_event"] in {"turn.failed", "error"}:
        state["state"] = "REVIEW_FAILED"
        state["pre_review_state"] = previous_state
        state["returncode"] = result.returncode
        atomic_write_json(state_file, state)
        emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir)})
        return EXIT_REVIEW_FAILED

    state["state"] = "REVIEWED"
    state["review_result"] = str(review_result_path)
    atomic_write_json(state_file, state)
    review_text = review_result_path.read_text(encoding="utf-8", errors="replace") if review_result_path.exists() else ""
    emit({"run_id": args.run_id, "state": state["state"], "run_dir": str(run_dir), "review": review_text})
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    run_dir, state = load_state(args.home, args.run_id)
    report: dict[str, Any] = {
        "run_id": args.run_id,
        "state": state.get("state"),
        "session_id": state.get("session_id"),
        "workspace": state.get("workspace"),
        "run_dir": str(run_dir),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
    }
    try:
        current = git_snapshot(canonical_path(state["workspace"]))
        report["current_git"] = current
        if isinstance(state.get("plan_baseline"), dict):
            report["drift_from_plan"] = drift_details(state["plan_baseline"], current)
    except PeterCodexError as exc:
        report["git_error"] = str(exc)
    emit(report)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="petercodex",
        description="Supervised Plan -> Execute -> Review orchestration for OpenAI Codex CLI.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=DEFAULT_HOME,
        help="Durable PeterCodex state directory (default: ~/.petercodex or PETERCODEX_HOME).",
    )
    parser.add_argument("--version", action="version", version=f"PeterCodex {PLUGIN_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check Codex, Git, and authentication readiness.")
    doctor.set_defaults(func=cmd_doctor)

    plan = subparsers.add_parser("plan", help="Create a read-only Codex implementation plan and durable session.")
    plan.add_argument("--workspace", required=True, help="Absolute or resolvable Git workspace path.")
    plan.add_argument("--prompt", required=True, help="Coding objective and acceptance criteria.")
    plan.add_argument("--model", help="Optional Codex model override for the planning session.")
    plan.add_argument(
        "--local-provider",
        choices=["ollama", "lmstudio"],
        help="Use a built-in Codex local provider; persisted for Execute/Review.",
    )
    plan.add_argument("--provider-id", help="Custom Codex model-provider id, for example proxycli.")
    plan.add_argument("--provider-name", help="Optional display name for the custom provider.")
    plan.add_argument("--provider-base-url", help="OpenAI-compatible base URL, typically ending in /v1.")
    plan.add_argument("--provider-api-key-env", help="Environment variable containing the provider API key; the key itself is never persisted.")
    plan.add_argument(
        "--provider-wire-api",
        choices=["responses", "chat"],
        help="Custom provider protocol. Defaults to responses.",
    )
    plan.add_argument("--timeout-minutes", type=int, default=20)
    plan.set_defaults(func=cmd_plan)

    execute = subparsers.add_parser("execute", help="Resume an exact plan session in workspace-write mode.")
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--allow-drift", action="store_true", help="Explicitly accept Git drift since Plan.")
    execute.add_argument("--allow-git-commit", action="store_true", help="Allow one local commit; never authorizes push/history rewriting.")
    execute.add_argument("--timeout-minutes", type=int, default=30)
    execute.set_defaults(func=cmd_execute)

    review = subparsers.add_parser("review", help="Run an independent read-only Codex review of uncommitted changes.")
    review.add_argument("--run-id", required=True)
    review.add_argument("--model", help="Optional model override for the independent review pass.")
    review.add_argument("--timeout-minutes", type=int, default=20)
    review.set_defaults(func=cmd_review)

    status = subparsers.add_parser("status", help="Inspect durable run state and current Git drift.")
    status.add_argument("--run-id", required=True)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.home = Path(args.home).expanduser().resolve()
    try:
        return int(args.func(args))
    except PeterCodexError as exc:
        emit({"error": str(exc), "command": getattr(args, "command", None)})
        return EXIT_STATE
    except KeyboardInterrupt:
        emit({"error": "Interrupted by user", "command": getattr(args, "command", None)})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
