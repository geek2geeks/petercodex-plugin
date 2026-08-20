from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "petercodex-plugin"
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "petercodex.py"

spec = importlib.util.spec_from_file_location("petercodex", SCRIPT_PATH)
assert spec and spec.loader
petercodex = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = petercodex
spec.loader.exec_module(petercodex)


class PeterCodexUnitTests(unittest.TestCase):
    def test_jsonl_parser_extracts_thread_and_terminal_event(self) -> None:
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"abc-123"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
                '{"type":"turn.completed","usage":{"input_tokens":1}}',
            ]
        )
        events, invalid = petercodex.parse_jsonl(stdout)
        self.assertEqual([], invalid)
        self.assertEqual("abc-123", petercodex.thread_id_from_events(events))
        self.assertEqual("turn.completed", petercodex.terminal_event(events))

    def test_jsonl_parser_preserves_invalid_lines_as_evidence(self) -> None:
        events, invalid = petercodex.parse_jsonl(
            '{"type":"thread.started","thread_id":"abc"}\nnot-json\n'
        )
        self.assertEqual(1, len(events))
        self.assertEqual(["not-json"], invalid)

    def test_local_provider_args_are_resume_safe(self) -> None:
        self.assertEqual(
            ["-c", 'model_provider="ollama"', "-m", "qwen3.5:9b"],
            petercodex.codex_provider_args("qwen3.5:9b", "ollama"),
        )
        self.assertEqual([], petercodex.codex_provider_args(None, None))

    def test_custom_provider_args_use_env_key_without_secret_value(self) -> None:
        provider = {
            "id": "proxycli",
            "name": "ProxyCLI",
            "base_url": "http://127.0.0.1:8317/v1",
            "api_key_env": "PETERCODEX_PROXYCLI_API_KEY",
            "wire_api": "responses",
        }
        with mock.patch.dict("os.environ", {"PETERCODEX_PROXYCLI_API_KEY": "secret-test-value"}, clear=False):
            args = petercodex.codex_provider_args("antigravity-gemini-3.7-flash-high", None, provider)
        joined = " ".join(args)
        self.assertIn('model_provider="proxycli"', joined)
        self.assertIn('model_providers.proxycli.base_url="http://127.0.0.1:8317/v1"', joined)
        self.assertIn('model_providers.proxycli.env_key="PETERCODEX_PROXYCLI_API_KEY"', joined)
        self.assertIn('model_providers.proxycli.wire_api="responses"', joined)
        self.assertNotIn("secret-test-value", joined)

    def test_custom_provider_rejects_missing_api_key_env(self) -> None:
        provider = {
            "id": "proxycli",
            "name": "ProxyCLI",
            "base_url": "http://127.0.0.1:8317/v1",
            "api_key_env": "PETERCODEX_MISSING_KEY",
            "wire_api": "responses",
        }
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(petercodex.PeterCodexError):
                petercodex.codex_provider_args("model", None, provider)

    def test_execution_file_evidence_matches_clean_git_delta(self) -> None:
        evidence = petercodex.execution_file_evidence(
            {"changed_paths": []},
            {"changed_paths": ["src/a.ts", "tests/a.test.ts"]},
            {"files_changed": ["tests/a.test.ts", "src\\a.ts"]},
        )
        self.assertTrue(evidence["enforced"])
        self.assertTrue(evidence["matches"])

    def test_execution_file_evidence_rejects_hallucinated_files(self) -> None:
        evidence = petercodex.execution_file_evidence(
            {"changed_paths": []},
            {"changed_paths": []},
            {"files_changed": ["test/usecases.test.ts"]},
        )
        self.assertTrue(evidence["enforced"])
        self.assertFalse(evidence["matches"])
        self.assertEqual([], evidence["actual"])

    def test_execution_result_requires_acceptance_evidence(self) -> None:
        good = {
            "acceptance_criteria": [{"criterion": "file exists", "status": "met", "evidence": "checked"}],
            "tests": [{"name": "smoke", "status": "passed", "details": "ok"}],
        }
        bad = {"acceptance_criteria": [], "tests": []}
        self.assertTrue(petercodex.execution_result_evidence(good)["passes"])
        self.assertFalse(petercodex.execution_result_evidence(bad)["passes"])

    def test_structured_fallback_accepts_fenced_json_agent_message(self) -> None:
        required = {"summary", "ready_to_execute"}
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": '```json\n{"summary":"ok","ready_to_execute":true}\n```',
                },
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result = petercodex.load_structured_result(path, events, required)
            self.assertEqual({"summary": "ok", "ready_to_execute": True}, result)
            self.assertEqual(result, json.loads(path.read_text(encoding="utf-8")))

    def test_structured_fallback_rejects_missing_required_keys(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"summary":"ok"}'},
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            with self.assertRaises(petercodex.PeterCodexError):
                petercodex.load_structured_result(path, events, {"summary", "ready_to_execute"})

    def test_drift_detection_is_orthogonal(self) -> None:
        baseline = {"git_root": "/repo", "head": "aaa", "status_digest": "111"}
        same = dict(baseline)
        changed_head = {**baseline, "head": "bbb"}
        changed_tree = {**baseline, "status_digest": "222"}
        self.assertFalse(petercodex.has_drift(petercodex.drift_details(baseline, same)))
        self.assertTrue(petercodex.drift_details(baseline, changed_head)["head_changed"])
        self.assertTrue(petercodex.drift_details(baseline, changed_tree)["working_tree_changed"])

    def test_atomic_state_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            expected = {"run_id": "run-1", "state": "PLANNED", "session_id": "abc"}
            petercodex.atomic_write_json(path, expected)
            self.assertEqual(expected, petercodex.load_json(path))

    def test_distribution_manifest_and_marketplace_match(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual("petercodex-plugin", manifest["name"])
        self.assertEqual("0.1.1", manifest["version"])
        self.assertEqual("petercodex", marketplace["name"])
        self.assertEqual("petercodex-plugin", marketplace["plugins"][0]["name"])
        self.assertEqual("./plugins/petercodex-plugin", marketplace["plugins"][0]["source"]["path"])

    def test_schemas_are_valid_json_with_required_fields(self) -> None:
        plan = json.loads((PLUGIN_ROOT / "schemas" / "plan-result.schema.json").read_text(encoding="utf-8"))
        execute = json.loads((PLUGIN_ROOT / "schemas" / "execute-result.schema.json").read_text(encoding="utf-8"))
        self.assertIn("ready_to_execute", plan["required"])
        self.assertEqual(1, plan["properties"]["validation"]["minItems"])
        self.assertIn("acceptance_criteria", execute["required"])
        self.assertEqual(1, execute["properties"]["acceptance_criteria"]["minItems"])
        self.assertFalse(plan["additionalProperties"])
        self.assertFalse(execute["additionalProperties"])

    def test_skill_frontmatter_exists(self) -> None:
        text = (PLUGIN_ROOT / "skills" / "petercodex-plugin" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: petercodex-plugin\n"))
        self.assertIn("Plan", text)
        self.assertIn("Execute", text)
        self.assertIn("Review", text)

    def test_codex_resolver_falls_back_after_broken_shim(self) -> None:
        petercodex._CODEX_COMMAND_CACHE = None
        broken = petercodex.ProcessResult(1, "", "broken shim", False)
        working = petercodex.ProcessResult(0, "codex-cli 1.2.3\n", "", False)
        with mock.patch.object(
            petercodex,
            "_candidate_codex_commands",
            return_value=[["broken-codex"], ["node", "staged-codex.js"]],
        ), mock.patch.object(petercodex, "run_process", side_effect=[broken, working]):
            command = petercodex.resolve_codex_command()
        self.assertEqual(["node", "staged-codex.js"], command)
        petercodex._CODEX_COMMAND_CACHE = None


if __name__ == "__main__":
    unittest.main()
