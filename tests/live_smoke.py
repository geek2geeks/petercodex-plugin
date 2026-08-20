from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "petercodex-plugin"
SCRIPT = PLUGIN_ROOT / "scripts" / "petercodex.py"


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def parse_json(stdout: str) -> dict:
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Expected JSON object from PeterCodex")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Live authenticated PeterCodex Plan -> Execute -> Review smoke test.")
    parser.add_argument("--skip-review", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--local-provider", choices=["ollama", "lmstudio"])
    args = parser.parse_args()

    if not shutil.which("git"):
        raise RuntimeError("git is required")
    if not shutil.which("codex"):
        raise RuntimeError("codex is required")

    with tempfile.TemporaryDirectory(prefix="petercodex-live-smoke-") as temp:
        temp_root = Path(temp)
        workspace = temp_root / "repo"
        state_home = temp_root / "state"
        workspace.mkdir()

        run(["git", "init", "-b", "main"], cwd=workspace)
        run(["git", "config", "user.name", "PeterCodex Smoke"], cwd=workspace)
        run(["git", "config", "user.email", "petercodex-smoke@example.invalid"], cwd=workspace)
        (workspace / "README.md").write_text("# PeterCodex smoke fixture\n", encoding="utf-8")
        run(["git", "add", "README.md"], cwd=workspace)
        run(["git", "commit", "-m", "init smoke fixture"], cwd=workspace)

        objective = (
            "Create a file named petercodex-smoke.txt containing exactly PETERCODEX_EXECUTION_OK followed by one newline. "
            "Do not modify README.md or any other repository file. Validate the exact file content."
        )

        plan_cmd = [
            sys.executable,
            str(SCRIPT),
            "--home",
            str(state_home),
            "plan",
            "--workspace",
            str(workspace),
            "--prompt",
            objective,
            "--timeout-minutes",
            "10",
        ]
        if args.model:
            plan_cmd.extend(["--model", args.model])
        if args.local_provider:
            plan_cmd.extend(["--local-provider", args.local_provider])
        plan = parse_json(run(plan_cmd, timeout=900).stdout)
        if plan.get("state") != "PLANNED":
            raise RuntimeError(f"Unexpected plan state: {plan}")

        status_before = run(["git", "status", "--porcelain"], cwd=workspace).stdout
        if status_before.strip():
            raise RuntimeError(f"Plan dirtied the repository: {status_before}")

        run_id = plan["run_id"]
        execute = parse_json(
            run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--home",
                    str(state_home),
                    "execute",
                    "--run-id",
                    run_id,
                    "--timeout-minutes",
                    "15",
                ],
                timeout=1200,
            ).stdout
        )
        if execute.get("state") != "EXECUTED":
            raise RuntimeError(f"Unexpected execute state: {execute}")

        target = workspace / "petercodex-smoke.txt"
        if not target.exists():
            raise RuntimeError("Execution did not create petercodex-smoke.txt")
        if target.read_text(encoding="utf-8") != "PETERCODEX_EXECUTION_OK\n":
            raise RuntimeError(f"Unexpected smoke file content: {target.read_text(encoding='utf-8')!r}")
        if (workspace / "README.md").read_text(encoding="utf-8") != "# PeterCodex smoke fixture\n":
            raise RuntimeError("Execution modified README.md unexpectedly")

        changed = run(["git", "status", "--porcelain"], cwd=workspace).stdout.splitlines()
        if changed != ["?? petercodex-smoke.txt"]:
            raise RuntimeError(f"Unexpected repository changes: {changed}")

        review_state = None
        if not args.skip_review:
            review = parse_json(
                run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--home",
                        str(state_home),
                        "review",
                        "--run-id",
                        run_id,
                        "--timeout-minutes",
                        "10",
                    ],
                    timeout=900,
                ).stdout
            )
            review_state = review.get("state")
            if review_state != "REVIEWED":
                raise RuntimeError(f"Unexpected review state: {review}")

        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run_id": run_id,
                    "plan_state": plan["state"],
                    "execute_state": execute["state"],
                    "review_state": review_state,
                    "verified_file": "petercodex-smoke.txt",
                    "verified_content": "PETERCODEX_EXECUTION_OK\\n",
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
