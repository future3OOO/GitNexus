"""Attacks on the detect-changes CLI through the real entry on real analyzed repositories.

The MCP detect_changes tool had no CLI counterpart, so nothing outside a Claude
session could map dirty hunks to symbols. These tests drive the same entry the
dist CLI is built from against a temp repo indexed by the built CLI under an
isolated HOME (the repository registry lives there), read the CLI's JSON, and
compare it with the MCP server's answer for the same call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gitnexus.test.e2e.test_mcp_cypher_isolation import McpClient

PACKAGE = Path(__file__).resolve().parents[2]
ENTRY = ["node", "--import", "tsx", str(PACKAGE / "src" / "cli" / "index.ts")]
# analyze forks parser workers that bypass the tsx loader, so indexing the
# fixture goes through the built CLI; the command under test runs from source.
DIST_ENTRY = ["node", str(PACKAGE / "dist" / "cli" / "index.js")]
BASE = "def value():\n    return 1\n\n\ndef other():\n    return value() + 1\n"
EDITED = BASE.replace("return 1", "return 2")


def normalized(payload: dict) -> dict:
    """The payload with its two list fields in a stable order; the backend promises their members, not their order."""
    return {**payload,
            "changed_symbols": sorted(payload["changed_symbols"], key=lambda symbol: symbol["id"]),
            "affected_processes": sorted(payload["affected_processes"], key=lambda process: process["id"])}


class DetectChangesCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gitnexus-detect-changes-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "harness@example.invalid")
        self.git("config", "user.name", "Harness")
        (self.repo / "app.py").write_text(BASE, encoding="utf-8")
        (self.repo / "lib.py").write_text("def helper():\n    return 'a'\n", encoding="utf-8")
        self.git("add", "app.py", "lib.py")
        self.git("commit", "-q", "-m", "base")
        analyzed = self.cli("analyze", "--skip-agents-md", str(self.repo), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def cli(self, *args: str, entry: list[str] = ENTRY) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*entry, *args], cwd=str(PACKAGE), text=True, capture_output=True, check=False,
                              env={**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})

    def detect(self, *args: str) -> dict:
        result = self.cli("detect-changes", *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def names(payload: dict) -> set[str]:
        return {symbol.get("name") for symbol in payload.get("changed_symbols", [])}

    def assert_no_changes(self, payload: dict, marker: str) -> None:
        self.assertEqual(payload["changed_symbols"], [], marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload["summary"]["changed_count"], 0, marker + ": " + json.dumps(payload)[:600])
        # A no-change summary carries no changed_files at all; zero files is the only admissible reading.
        self.assertEqual(payload["summary"].get("changed_files", 0), 0, marker + ": " + json.dumps(payload)[:600])

    def assert_refused_as_json(self, result: subprocess.CompletedProcess[str], marker: str) -> None:
        self.assertEqual(result.returncode, 1, marker + ": " + result.stdout + result.stderr)
        self.assertTrue(result.stdout.lstrip().startswith("{"), marker + ": " + result.stdout + result.stderr)
        self.assertIn("error", json.loads(result.stdout), marker)

    def test_a_dirty_hunk_maps_to_its_symbol(self) -> None:
        marker = "DETECT_CHANGES_CLI_MISSING"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        result = self.cli("detect-changes", "-r", str(self.repo), "--scope", "unstaged")
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("value", self.names(payload), marker + ": " + result.stdout[:600])
        self.assertEqual(payload["summary"]["changed_files"], 1, marker)

    def test_a_hunk_inside_one_function_reports_only_that_function(self) -> None:
        marker = "SIBLING_SYMBOL_REPORTED_CHANGED"
        (self.repo / "app.py").write_text(BASE.replace("return value() + 1", "return value() + 2"), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"other"}, marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload["summary"]["changed_count"], 1, marker + ": " + json.dumps(payload["summary"]))

    def test_a_deleted_file_reports_its_symbols_as_deleted(self) -> None:
        marker = "DELETED_FILE_SYMBOLS_LOST"
        self.git("rm", "-q", "lib.py")

        payload = self.detect("-r", str(self.repo), "--scope", "staged")

        deleted = {s["name"]: s["change_type"] for s in payload["changed_symbols"]}
        self.assertEqual(deleted.get("helper"), "Deleted", marker + ": " + json.dumps(payload)[:600])

    def test_an_unknown_repo_is_refused_as_json(self) -> None:
        self.assert_refused_as_json(self.cli("detect-changes", "-r", str(self.tmp / "nowhere")),
                                    "DETECT_CHANGES_BAD_REPO_NOT_REFUSED")

    def test_staged_scope_reports_only_staged_hunks(self) -> None:
        marker = "STAGED_SCOPE_NOT_ISOLATED"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        self.git("add", "app.py")
        staged = self.detect("-r", str(self.repo), "-s", "staged")
        self.assertIn("value", self.names(staged), marker + ": " + json.dumps(staged)[:600])
        self.assert_no_changes(self.detect("-r", str(self.repo), "--scope", "unstaged"), marker)

    def test_all_scope_reports_staged_and_unstaged_files(self) -> None:
        marker = "ALL_SCOPE_MISSES_A_SIDE"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        self.git("add", "app.py")
        (self.repo / "lib.py").write_text("def helper():\n    return 'b'\n", encoding="utf-8")
        payload = self.detect("-r", str(self.repo), "--scope", "all")
        self.assertEqual(payload["summary"]["changed_files"], 2, marker + ": " + json.dumps(payload)[:600])
        self.assertTrue({"value", "helper"} <= self.names(payload), marker + ": " + json.dumps(payload)[:600])

    def test_compare_scope_reads_a_committed_difference(self) -> None:
        marker = "COMPARE_SCOPE_IGNORES_BASE_REF"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        self.git("commit", "-q", "-am", "edit")
        payload = self.detect("-r", str(self.repo), "--scope", "compare", "--base-ref", "HEAD~1")
        self.assertIn("value", self.names(payload), marker + ": " + json.dumps(payload)[:600])
        self.assert_no_changes(self.detect("-r", str(self.repo), "--scope", "unstaged"), marker)

    def test_an_invalid_scope_is_refused_before_anything_runs(self) -> None:
        marker = "INVALID_SCOPE_ACCEPTED"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        result = self.cli("detect-changes", "-r", str(self.repo), "--scope", "alll")
        self.assertNotEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "", marker + ": " + result.stdout)
        for choice in ("unstaged", "staged", "all", "compare"):
            self.assertIn(choice, result.stderr, marker + ": " + result.stderr)

    def test_default_scope_and_long_repo_flag_match_explicit_unstaged(self) -> None:
        marker = "DEFAULT_SCOPE_DRIFTS"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        explicit = self.detect("-r", str(self.repo), "-s", "unstaged")
        self.assertEqual(normalized(self.detect("--repo", str(self.repo))), normalized(explicit), marker)

    def test_omitted_repo_resolves_the_only_registered_repo(self) -> None:
        marker = "OMITTED_REPO_NOT_RESOLVED"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        result = self.cli("detect-changes")
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        self.assertIn("value", self.names(json.loads(result.stdout)), marker + ": " + result.stdout[:600])

    def test_a_registered_name_selects_the_repo(self) -> None:
        marker = "NAME_SELECTOR_NOT_RESOLVED"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        registry = json.loads((self.home / ".gitnexus" / "registry.json").read_text(encoding="utf-8"))
        (name,) = [entry["name"] for entry in registry if entry["path"] == str(self.repo)]
        self.assertNotEqual(name, str(self.repo), marker + ": the registry names the repo by its path only")
        by_name = self.detect("-r", name)
        self.assertIn("value", self.names(by_name), marker + ": " + json.dumps(by_name)[:600])
        self.assertEqual(normalized(by_name), normalized(self.detect("-r", str(self.repo))), marker)

    def test_an_unknown_repo_name_is_refused_as_json(self) -> None:
        self.assert_refused_as_json(self.cli("detect-changes", "-r", "no-such-repo"), "UNKNOWN_NAME_NOT_REFUSED")

    def test_a_registered_repo_without_its_index_is_refused(self) -> None:
        marker = "MISSING_INDEX_NOT_REFUSED"
        shutil.rmtree(self.repo / ".gitnexus")
        result = self.cli("detect-changes", "-r", str(self.repo))
        self.assertEqual(result.returncode, 1, marker + ": " + result.stdout + result.stderr)

    def test_a_missing_index_is_refused_as_json(self) -> None:
        # The registered repository's index is gone, so the backend has nothing
        # to start with; the refusal must still be the command's JSON answer.
        shutil.rmtree(self.repo / ".gitnexus")
        self.assert_refused_as_json(self.cli("detect-changes", "-r", str(self.repo)), "MISSING_INDEX_NOT_JSON")

    def test_an_edit_inside_a_call_chain_names_its_affected_process(self) -> None:
        marker = "AFFECTED_PROCESS_NOT_TRACED"
        (self.repo / "entry.py").write_text("from step import step\n\ndef entry():\n    return step(1)\n", encoding="utf-8")
        (self.repo / "step.py").write_text("from leaf import leaf\n\ndef step(x):\n    return leaf(x) + 1\n", encoding="utf-8")
        (self.repo / "leaf.py").write_text("def leaf(x):\n    return x * 2\n", encoding="utf-8")
        self.git("add", "entry.py", "step.py", "leaf.py")
        self.git("commit", "-q", "-m", "chain")
        analyzed = self.cli("analyze", "--force", "--skip-agents-md", str(self.repo), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)
        (self.repo / "leaf.py").write_text("def leaf(x):\n    return x * 3\n", encoding="utf-8")
        payload = self.detect("-r", str(self.repo))
        self.assertIn("leaf", self.names(payload), marker + ": " + json.dumps(payload)[:600])
        self.assertIn("Entry → Leaf", [process.get("name") for process in payload["affected_processes"]],
                      marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload["summary"]["affected_count"], 1, marker)

    def test_cli_payload_equals_the_mcp_payload(self) -> None:
        marker = "CLI_MCP_PAYLOAD_DIFFERS"
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        cli = self.detect("-r", str(self.repo), "--scope", "unstaged")
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)
        mcp = client.call("detect_changes", {"repo": str(self.repo), "scope": "unstaged"})
        self.assertIsNotNone(mcp, marker + ": the MCP server gave no answer")
        self.assertEqual(normalized(cli), normalized(mcp), marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
