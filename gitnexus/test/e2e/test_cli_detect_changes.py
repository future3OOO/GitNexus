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
import time
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
    ordered = {**payload,
               "changed_symbols": sorted(payload["changed_symbols"], key=lambda symbol: symbol["id"]),
               "affected_processes": sorted(payload["affected_processes"], key=lambda process: process["id"])}
    if "impacted_tests" in payload:
        ordered["impacted_tests"] = sorted(payload["impacted_tests"], key=lambda test: test["id"])
    return ordered


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

    def commit_and_analyze(self, path: str, content: str) -> None:
        (self.repo / path).write_text(content, encoding="utf-8")
        self.git("add", path)
        self.git("commit", "-q", "-m", f"add {path}")
        analyzed = self.cli("analyze", "--force", "--skip-agents-md", str(self.repo), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)

    def test_a_non_ascii_path_reports_its_symbol(self) -> None:
        marker = "QUOTED_PATH_DROPPED"
        self.commit_and_analyze("café.py", "def accent():\n    return 1\n")
        (self.repo / "café.py").write_text("def accent():\n    return 2\n", encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"accent"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_deleted_body_line_that_diffs_as_dev_null_keeps_the_file_modified(self) -> None:
        marker = "BODY_LINE_READ_AS_METADATA"
        content = 'def value():\n    return 1\n\n\ndef note():\n    text = """\n-- /dev/null\n"""\n    return text\n'
        self.commit_and_analyze("doc.py", content)
        (self.repo / "doc.py").write_text(content.replace("-- /dev/null\n", ""), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        changed = {s["name"]: s["change_type"] for s in payload["changed_symbols"] if s["filePath"] == "doc.py"}
        self.assertEqual(changed, {"note": "Modified"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_deleted_binary_attributed_file_reports_its_symbols_as_deleted(self) -> None:
        marker = "BINARY_DELETION_SYMBOLS_LOST"
        (self.repo / ".gitattributes").write_text("lib.py -diff\n", encoding="utf-8")
        self.git("add", ".gitattributes")
        self.git("commit", "-q", "-m", "attributes")
        self.git("rm", "-q", "lib.py")

        payload = self.detect("-r", str(self.repo), "--scope", "staged")

        deleted = {s["name"]: s["change_type"] for s in payload["changed_symbols"]}
        self.assertEqual(deleted.get("helper"), "Deleted", marker + ": " + json.dumps(payload)[:600])

    def test_deleting_a_blank_separator_line_reports_no_symbol(self) -> None:
        marker = "SEPARATOR_DELETION_REPORTED_NEIGHBOUR"
        (self.repo / "app.py").write_text(BASE.replace("\n\n\n", "\n\n", 1), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual((payload["changed_symbols"], payload["summary"]["changed_count"]), ([], 0),
                         marker + ": " + json.dumps(payload)[:600])

    def test_a_patch_above_one_mebibyte_is_analysed(self) -> None:
        marker = "LARGE_PATCH_FAILED"
        padding = "".join(f"    pad_{index} = {index}\n" for index in range(60000))
        (self.repo / "lib.py").write_text("def helper():\n" + padding + "    return 'a'\n", encoding="utf-8")

        result = self.cli("detect-changes", "-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(result.returncode, 0, marker + ": " + (result.stdout + result.stderr)[:600])
        payload = json.loads(result.stdout)
        self.assertNotIn("error", payload, marker + ": " + result.stdout[:600])
        self.assertEqual(self.names(payload), {"helper"}, marker + ": " + result.stdout[:600])

    def test_hunks_are_mapped_on_the_lines_the_index_was_built_from(self) -> None:
        marker = "SHIFTED_HUNK_MISMAPPED"
        shifted = "import os\nimport sys\nimport json\n" + BASE.replace("return value() + 1", "return value() + 2")
        (self.repo / "app.py").write_text(shifted, encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"other"}, marker + ": " + json.dumps(payload)[:600])

    def test_an_insertion_inside_a_function_reports_that_function(self) -> None:
        marker = "INSERTION_INSIDE_FUNCTION_MISSED"
        (self.repo / "app.py").write_text(BASE.replace("def value():\n", "def value():\n    x = 1\n"), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"value"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_quoted_header_does_not_attach_its_hunks_to_the_previous_file(self) -> None:
        marker = "QUOTED_HEADER_HUNKS_MISATTRIBUTED"
        quoted = 'q"uote.py'
        self.commit_and_analyze(quoted, "def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")
        (self.repo / quoted).write_text("def one():\n    return 1\n\n\ndef two():\n    return 3\n", encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"value"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_prefixless_diff_configuration_still_maps_hunks(self) -> None:
        marker = "PREFIX_CONFIGURATION_BROKE_HEADERS"
        self.git("config", "diff.noprefix", "true")
        self.git("config", "diff.mnemonicPrefix", "true")
        (self.repo / "app.py").write_text(EDITED, encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"value"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_staged_rename_reports_the_old_file_symbols_as_deleted(self) -> None:
        marker = "RENAME_HID_THE_DELETION"
        self.git("mv", "lib.py", "lib2.py")

        payload = self.detect("-r", str(self.repo), "--scope", "staged")

        changed = {(s["filePath"], s["name"]): s["change_type"] for s in payload["changed_symbols"]}
        self.assertEqual(changed.get(("lib.py", "helper")), "Deleted", marker + ": " + json.dumps(payload)[:600])

    def test_an_indexed_new_file_reports_its_symbols_as_added(self) -> None:
        marker = "NEW_FILE_NOT_CLASSIFIED_ADDED"
        (self.repo / "fresh.py").write_text("def fresh():\n    return 1\n", encoding="utf-8")
        self.git("add", "fresh.py")
        analyzed = self.cli("analyze", "--force", "--skip-agents-md", str(self.repo), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)

        payload = self.detect("-r", str(self.repo), "--scope", "staged")

        changed = {s["name"]: s["change_type"] for s in payload["changed_symbols"]}
        self.assertEqual(changed.get("fresh"), "Added", marker + ": " + json.dumps(payload)[:600])

    def test_deleting_the_only_separator_line_reports_no_symbol(self) -> None:
        marker = "ZERO_COUNT_DELETION_WIDENED"
        content = "def value():\n    return 1\n\ndef other():\n    return value() + 1\n"
        self.commit_and_analyze("tight.py", content)
        (self.repo / "tight.py").write_text(content.replace("\n\ndef other", "\ndef other"), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        tight = [s["name"] for s in payload["changed_symbols"] if s["filePath"] == "tight.py"]
        self.assertEqual(tight, [], marker + ": " + json.dumps(payload)[:600])

    def test_editing_a_markdown_heading_line_reports_that_section(self) -> None:
        marker = "MARKDOWN_HEADING_EDIT_MAPPED_TO_PREVIOUS_SECTION"
        # Sibling headings: a nested heading would also lie inside its parent section.
        content = "# Intro\n\nHello.\n\n# Setup\n\nSteps.\n"
        self.commit_and_analyze("guide.md", content)
        (self.repo / "guide.md").write_text(content.replace("# Setup\n", "# Setup   \n"), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        self.assertEqual(self.names(payload), {"Setup"}, marker + ": " + json.dumps(payload)[:600])

    def test_editing_a_cobol_paragraph_line_reports_that_paragraph(self) -> None:
        marker = "COBOL_PARAGRAPH_EDIT_MAPPED_TO_PREVIOUS_PARAGRAPH"
        content = (
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID. DEMO.\n"
            "       PROCEDURE DIVISION.\n"
            "       PARA-A.\n"
            "           DISPLAY 'A'.\n"
            "       PARA-B.\n"
            "           DISPLAY 'B'.\n"
            "           STOP RUN.\n"
        )
        self.commit_and_analyze("prog.cbl", content)
        (self.repo / "prog.cbl").write_text(content.replace("       PARA-B.\n", "       PARA-B.   \n"), encoding="utf-8")

        payload = self.detect("-r", str(self.repo), "--scope", "unstaged")

        # The program Module spans the whole file, like a class around an edited method.
        cobol = {s["name"] for s in payload["changed_symbols"] if s["filePath"] == "prog.cbl"}
        self.assertEqual(cobol, {"DEMO", "PARA-B"}, marker + ": " + json.dumps(payload)[:600])

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


SERVICE = (
    "class Service:\n"
    "    def compute(self, x):\n"
    "        return x + 1\n"
    "\n"
    "    def other(self):\n"
    "        return 0\n"
    "\n"
    "\n"
    "def helper():\n"
    "    return Service().compute(1)\n"
)
TEST_SVC = (
    "from svc import Service, helper\n"
    "\n"
    "\n"
    "def test_compute():\n"
    "    assert Service().compute(1) == 2\n"
    "\n"
    "\n"
    "def test_helper():\n"
    "    assert helper() == 2\n"
)
TEST_OTHER = "from svc import Service\n\n\ndef test_other():\n    assert Service().other() == 0\n"
TEST_UNRELATED = "def test_nothing():\n    assert True\n"
GUIDE = "# Intro\n\nHello.\n\n## Setup\n\nSteps.\n"
COBOL = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. DEMO.\n"
    "       PROCEDURE DIVISION.\n"
    "       PARA-A.\n"
    "           DISPLAY 'A'.\n"
    "       PARA-B.\n"
    "           DISPLAY 'B'.\n"
    "           STOP RUN.\n"
)


def write_working_tree(repo: Path) -> str:
    """The tree id of repo's working tree (tracked and untracked, ignored excluded), written into its object store."""
    index = repo.parent / (repo.name + "-candidate-index")
    excludes = repo.parent / (repo.name + "-candidate-excludes")
    excludes.write_text(".gitnexus/\n", encoding="utf-8")
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    git = ["git", "-C", str(repo), "-c", f"core.excludesFile={excludes}"]
    try:
        subprocess.run([*git, "read-tree", "HEAD"], check=True, capture_output=True, env=env)
        subprocess.run([*git, "add", "-A", "--", "."], check=True, capture_output=True, env=env)
        return subprocess.run([*git, "write-tree"], check=True, capture_output=True, text=True, env=env).stdout.strip()
    finally:
        index.unlink(missing_ok=True)
        excludes.unlink(missing_ok=True)


class WorktreeDetectChangesTests(unittest.TestCase):
    """Attacks on --worktree: a cache clone is indexed, the source checkout is edited.

    The two checkouts share no Git directory. Every attack drives the source CLI
    entry against the dist-analyzed cache index and reads the CLI's JSON.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gitnexus-detect-worktree-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q")
        self.git(self.source, "config", "user.email", "harness@example.invalid")
        self.git(self.source, "config", "user.name", "Harness")
        # The index directory is ignored up front, as in a real repository, so analyze never edits .gitignore.
        (self.source / ".gitignore").write_text(".gitnexus/\n", encoding="utf-8")
        (self.source / "svc.py").write_text(SERVICE, encoding="utf-8")
        (self.source / "tests").mkdir()
        (self.source / "tests" / "test_svc.py").write_text(TEST_SVC, encoding="utf-8")
        (self.source / "tests" / "test_other.py").write_text(TEST_OTHER, encoding="utf-8")
        (self.source / "tests" / "test_unrelated.py").write_text(TEST_UNRELATED, encoding="utf-8")
        self.git(self.source, "add", "-A")
        self.git(self.source, "commit", "-q", "-m", "base")
        # Not named "cache": the walker's default ignore list drops a directory of that name.
        self.cache = self.tmp / "indexed"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.source), str(self.cache)], check=True,
                       capture_output=True)
        self.analyze_cache("--force")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args: str, entry: list[str] = ENTRY, cwd: Path = PACKAGE,
            timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*entry, *args], cwd=str(cwd), text=True, capture_output=True, check=False,
                              timeout=timeout,
                              env={**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})

    def analyze_cache(self, *flags: str) -> None:
        analyzed = self.cli("analyze", *flags, "--skip-agents-md", str(self.cache), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, analyzed.stdout + analyzed.stderr)

    def commit_and_sync(self, files: dict[str, str]) -> None:
        """Commit files in the source, fast-forward the cache clone to the same commit, and re-index it."""
        for name, content in files.items():
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text(content, encoding="utf-8")
        self.git(self.source, "add", "-A")
        self.git(self.source, "commit", "-q", "-m", "sync " + " ".join(files))
        self.git(self.cache, "fetch", "-q", "origin", "HEAD")
        self.git(self.cache, "reset", "-q", "--hard", "FETCH_HEAD")
        self.analyze_cache("--force")

    def meta(self) -> dict:
        return json.loads((self.cache / ".gitnexus" / "meta.json").read_text(encoding="utf-8"))

    def detect_worktree(self, *args: str, worktree: Path | None = None,
                        timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return self.cli("detect-changes", "-r", str(self.cache), "--worktree", str(worktree or self.source), *args,
                        timeout=timeout)

    def payload(self, result: subprocess.CompletedProcess[str], marker: str) -> dict:
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        self.assertTrue(result.stdout.lstrip().startswith("{"), marker + ": " + result.stdout + result.stderr)
        return json.loads(result.stdout)

    def incoming(self, payload: dict, marker: str) -> dict[str, int]:
        """Each changed symbol's incoming_edges, keyed by name."""
        counts = {}
        for symbol in payload["changed_symbols"]:
            self.assertIn("incoming_edges", symbol, marker + ": " + json.dumps(symbol))
            counts[symbol["name"]] = symbol["incoming_edges"]
        return counts

    def cypher_rows(self, query: str, marker: str) -> list[str]:
        result = self.cli("cypher", query, "-r", str(self.cache))
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        return json.loads(result.stdout)["markdown"].splitlines()[2:]

    def refusal(self, result: subprocess.CompletedProcess[str], marker: str) -> dict:
        self.assertEqual(result.returncode, 1, marker + ": " + result.stdout + result.stderr)
        self.assertTrue(result.stdout.lstrip().startswith("{"), marker + ": " + result.stdout + result.stderr)
        body = json.loads(result.stdout)
        self.assertIn("error", body, marker + ": " + result.stdout[:600])
        self.assertEqual(body.get("analysis", {}).get("status"), "unavailable", marker + ": " + result.stdout[:600])
        return body

    @staticmethod
    def names(payload: dict) -> set[str]:
        return {symbol["name"] for symbol in payload.get("changed_symbols", [])}

    def impacted_paths(self, payload: dict, marker: str) -> set[str]:
        self.assertIn("impacted_tests", payload, marker + ": " + json.dumps(payload)[:600])
        return {test["filePath"] for test in payload["impacted_tests"]}

    def edit_compute(self) -> None:
        (self.source / "svc.py").write_text(SERVICE.replace("return x + 1", "return x + 2"), encoding="utf-8")

    def test_an_edit_in_the_other_checkout_is_attributed_through_worktree(self) -> None:
        marker = "WORKTREE_EDIT_NOT_ATTRIBUTED"
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload["summary"]["changed_files"], 1, marker + ": " + json.dumps(payload["summary"]))

    def test_a_dirty_snapshot_supplies_the_hunk_coordinates(self) -> None:
        marker = "SNAPSHOT_TREE_NOT_THE_BASELINE"
        # The cache is indexed with an uncommitted method inserted before compute, so the
        # indexed coordinates differ from HEAD's; HEAD-based old-side lines would land on `extra`.
        inserted = SERVICE.replace("    def compute(self, x):", "    def extra(self):\n        return 9\n\n    def compute(self, x):")
        (self.cache / "svc.py").write_text(inserted, encoding="utf-8")
        self.analyze_cache("--force")
        self.assertNotEqual(self.meta().get("indexedTree"), self.git(self.cache, "rev-parse", "HEAD^{tree}"), marker)
        # The source stages a divergent version (other edited) and keeps the snapshot's insertion
        # plus the compute edit in its working tree; only the working tree is the candidate.
        (self.source / "svc.py").write_text(inserted.replace("return 0", "return 1"), encoding="utf-8")
        self.git(self.source, "add", "svc.py")
        (self.source / "svc.py").write_text(inserted.replace("return x + 1", "return x + 2"), encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])

    def test_impacted_tests_equal_the_union_of_per_symbol_impact(self) -> None:
        marker = "IMPACTED_TESTS_DIFFER_FROM_IMPACT"
        self.commit_and_sync({
            "chain.py": "from svc import helper\n\n\ndef wrapper():\n    return helper()\n",
            "tests/test_chain.py": "from chain import wrapper\n\n\ndef test_wrapper():\n    assert wrapper() == 2\n",
            "guide.md": GUIDE,
            "prog.cbl": COBOL,
        })
        self.edit_compute()
        (self.source / "tests" / "test_svc.py").write_text(TEST_SVC.replace("assert helper() == 2", "assert helper() == 3"), encoding="utf-8")
        (self.source / "guide.md").write_text(GUIDE.replace("## Setup\n", "## Setup   \n"), encoding="utf-8")
        (self.source / "prog.cbl").write_text(COBOL.replace("       PARA-B.\n", "       PARA-B.   \n"), encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        seeds = {symbol["id"] for symbol in payload["changed_symbols"]}
        self.assertTrue({"Class:svc.py:Service", "Function:svc.py:Service.compute", "Function:tests/test_svc.py:test_helper"} <= seeds,
                        marker + ": " + json.dumps(sorted(seeds)))
        self.assertTrue(any(symbol["filePath"] == "guide.md" for symbol in payload["changed_symbols"]), marker + ": " + json.dumps(sorted(seeds)))
        self.assertTrue(any(symbol["filePath"] == "prog.cbl" for symbol in payload["changed_symbols"]), marker + ": " + json.dumps(sorted(seeds)))
        expected: set[str] = set()
        for seed in sorted(seeds):
            impact = self.cli("impact", "--uid", seed, "--include-tests", "-r", str(self.cache))
            self.assertEqual(impact.returncode, 0, marker + ": " + impact.stdout + impact.stderr)
            for items in json.loads(impact.stdout).get("byDepth", {}).values():
                expected |= {item["id"] for item in items if item["filePath"].startswith("tests/")}
        expected -= seeds
        self.assertIn("Function:tests/test_chain.py:test_wrapper", expected, marker + ": the depth-3 test must be in the oracle")
        self.assertIn("impacted_tests", payload, marker + ": " + json.dumps(payload)[:600])
        self.assertEqual({test["id"] for test in payload["impacted_tests"]}, expected, marker)

    def test_impact_on_a_class_lists_its_callers_beside_its_importers(self) -> None:
        marker = "CLASS_CALLERS_DROPPED_BY_FRONTIER_QUERY"
        # The class walk's frontier is exactly the class plus its owning File; the batched
        # frontier query must still return the CALLS edges into the class.
        impact = self.cli("impact", "--uid", "Class:svc.py:Service", "--include-tests", "-r", str(self.cache))
        self.assertEqual(impact.returncode, 0, marker + ": " + impact.stdout + impact.stderr)

        direct = {(item["id"], item["relationType"]) for item in json.loads(impact.stdout).get("byDepth", {}).get("1", [])}

        self.assertIn(("File:tests/test_svc.py", "IMPORTS"), direct, marker + ": " + json.dumps(sorted(direct)))
        self.assertIn(("Function:tests/test_svc.py:test_compute", "CALLS"), direct, marker + ": " + json.dumps(sorted(direct)))

    def test_a_high_fanout_test_set_is_not_truncated(self) -> None:
        marker = "IMPACTED_TESTS_TRUNCATED"
        many = "from svc import Service\n\n\n" + "".join(
            f"def test_many_{index}():\n    assert Service().compute({index}) == {index + 1}\n\n\n" for index in range(120))
        self.commit_and_sync({"tests/test_many.py": many})
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        reported = {test["name"] for test in payload.get("impacted_tests", []) if test["name"].startswith("test_many_")}
        self.assertEqual(len(reported), 120, marker + f": {len(reported)} of 120 reported")

    def test_repeated_calls_return_the_same_payload(self) -> None:
        marker = "REPEATED_CALL_UNSTABLE"
        self.edit_compute()

        first = self.payload(self.detect_worktree(), marker)
        second = self.payload(self.detect_worktree(), marker)

        self.assertIn("impacted_tests", first, marker + ": " + json.dumps(first)[:600])
        self.assertEqual(normalized(first), normalized(second), marker)

    def test_impacted_tests_carry_the_reachable_tests(self) -> None:
        marker = "IMPACTED_TESTS_MISSING"
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        self.assertIn("impacted_tests", payload, marker + ": " + json.dumps(payload)[:600])
        by_name = {test["name"]: test for test in payload["impacted_tests"]}
        self.assertTrue({"test_compute", "test_helper"} <= set(by_name), marker + ": " + json.dumps(payload["impacted_tests"])[:600])
        self.assertEqual(by_name["test_compute"]["filePath"], "tests/test_svc.py", marker)
        self.assertEqual(by_name["test_helper"]["id"], "Function:tests/test_svc.py:test_helper", marker)

    def test_a_test_reached_only_through_the_class_container_is_included(self) -> None:
        marker = "CONTAINER_REACHED_TEST_MISSING"
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        self.assertIn("tests/test_other.py", self.impacted_paths(payload, marker),
                      marker + ": " + json.dumps(payload.get("impacted_tests"))[:600])

    def test_an_unrelated_test_in_the_same_directory_is_not_reported(self) -> None:
        marker = "UNRELATED_TEST_REPORTED"
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        paths = self.impacted_paths(payload, marker)
        self.assertIn("tests/test_svc.py", paths, marker + ": " + json.dumps(payload.get("impacted_tests"))[:600])
        self.assertNotIn("tests/test_unrelated.py", paths, marker + ": " + json.dumps(payload.get("impacted_tests"))[:600])

    def test_a_same_checkout_run_carries_the_new_fields(self) -> None:
        marker = "SAME_CHECKOUT_FIELDS_MISSING"
        (self.cache / "svc.py").write_text(SERVICE.replace("return x + 1", "return x + 2"), encoding="utf-8")

        payload = self.payload(self.cli("detect-changes", "-r", str(self.cache), "--scope", "unstaged"), marker)

        self.assertIn("test_compute", {test["name"] for test in payload.get("impacted_tests", [])},
                      marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload.get("analysis", {}).get("status"), "complete", marker + ": " + json.dumps(payload.get("analysis")))
        self.assertEqual(payload["analysis"]["gaps"], [], marker)

    def write_meta(self, **changes: object) -> dict:
        meta = {**self.meta(), **changes}
        for key, value in changes.items():
            if value is None:
                meta.pop(key, None)
        (self.cache / ".gitnexus" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def test_a_worktree_whose_head_does_not_descend_from_the_snapshot_is_refused(self) -> None:
        marker = "DIVERGENT_ANCESTRY_ACCEPTED"
        # The cache is indexed at commit B; the source keeps B on a branch but moves HEAD back to A.
        self.commit_and_sync({"tests/test_unrelated.py": TEST_UNRELATED + "\n\ndef test_more():\n    assert True\n"})
        indexed_commit = self.meta()["lastCommit"]
        self.git(self.source, "branch", "keep", indexed_commit)
        self.git(self.source, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.git(self.source, "rev-parse", "keep"), indexed_commit, marker)

        body = self.refusal(self.detect_worktree(), marker)

        self.assertIn("ancestor", body["error"], marker + ": " + body["error"])

    def test_a_worktree_of_unrelated_history_is_refused_naming_the_unknown_commit(self) -> None:
        marker = "UNKNOWN_COMMIT_ACCEPTED"
        other = self.tmp / "other"
        other.mkdir()
        self.git(other, "init", "-q")
        self.git(other, "config", "user.email", "harness@example.invalid")
        self.git(other, "config", "user.name", "Harness")
        (other / "svc.py").write_text(SERVICE, encoding="utf-8")
        self.git(other, "add", "svc.py")
        self.git(other, "commit", "-q", "-m", "unrelated")

        body = self.refusal(self.detect_worktree(worktree=other), marker)

        self.assertIn(self.meta()["lastCommit"][:12], body["error"], marker + ": " + body["error"])
        self.assertNotIn("not an ancestor", body["error"], marker + ": an unknown commit is not a divergence: " + body["error"])

    def test_an_index_without_a_source_commit_is_refused(self) -> None:
        marker = "MISSING_SOURCE_COMMIT_ACCEPTED"
        self.write_meta(lastCommit="")
        self.edit_compute()

        body = self.refusal(self.detect_worktree(), marker)

        self.assertIn("lastCommit", body["error"], marker + ": " + body["error"])

    def test_an_index_without_snapshot_metadata_is_refused(self) -> None:
        marker = "MISSING_SNAPSHOT_META_ACCEPTED"
        self.write_meta(indexedTree=None)
        self.edit_compute()

        body = self.refusal(self.detect_worktree(), marker)

        self.assertIn("indexedTree", body["error"], marker + ": " + body["error"])

    def test_a_snapshot_tree_no_object_store_holds_is_refused(self) -> None:
        marker = "UNKNOWN_SNAPSHOT_TREE_ACCEPTED"
        missing = "1" * 40
        self.write_meta(indexedTree=missing)
        self.edit_compute()

        body = self.refusal(self.detect_worktree(), marker)

        self.assertIn(missing, body["error"], marker + ": " + body["error"])

    def test_an_edit_committed_after_the_snapshot_is_still_attributed(self) -> None:
        marker = "HEAD_USED_AS_BASELINE"
        self.edit_compute()
        self.git(self.source, "commit", "-q", "-am", "after snapshot")

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])

    def test_lines_inserted_above_the_edit_keep_the_attribution(self) -> None:
        marker = "SHIFTED_WORKTREE_HUNK_MISMAPPED"
        shifted = "import os\nimport sys\nimport json\n" + SERVICE.replace("return Service().compute(1)", "return Service().compute(2)")
        (self.source / "svc.py").write_text(shifted, encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.names(payload), {"helper"}, marker + ": " + json.dumps(payload)[:600])

    def test_an_untracked_overlay_present_in_both_trees_is_not_reported(self) -> None:
        marker = "OVERLAY_FILE_REPORTED_DELETED"
        # The same untracked file exists in both clones, but only the cache's analyze wrote
        # the snapshot tree object: the source's object store never held it.
        (self.source / "notes.py").write_text("def note():\n    return 1\n", encoding="utf-8")
        shutil.copy(self.source / "notes.py", self.cache / "notes.py")
        self.analyze_cache("--force")
        snapshot = self.meta().get("indexedTree")
        self.assertIsNotNone(snapshot, marker + ": " + json.dumps(self.meta()))
        absent = subprocess.run(["git", "-C", str(self.source), "cat-file", "-e", f"{snapshot}^{{tree}}"], capture_output=True)
        self.assertNotEqual(absent.returncode, 0, marker + ": the source object store must not hold the snapshot tree")
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload["analysis"]["status"], "complete", marker + ": " + json.dumps(payload["analysis"]))

    def test_a_clean_worktree_is_a_complete_empty_result(self) -> None:
        marker = "NO_CHANGE_RESULT_LACKS_ANALYSIS"

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(payload["changed_symbols"], [], marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload.get("impacted_tests"), [], marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(payload.get("analysis", {}).get("status"), "complete", marker + ": " + json.dumps(payload.get("analysis")))
        self.assertEqual(payload["analysis"]["gaps"], [], marker)

    def test_an_edit_to_a_tracked_file_without_symbols_is_an_explicit_gap(self) -> None:
        marker = "UNINDEXED_CHANGE_SILENT"
        self.commit_and_sync({"data.txt": "alpha\n"})
        (self.source / "data.txt").write_text("beta\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload.get("analysis", {})
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        gaps = {gap["path"]: gap.get("reason", "") for gap in analysis.get("gaps", [])}
        self.assertIn("data.txt", gaps, marker + ": " + json.dumps(analysis))
        self.assertIn("indexed", gaps["data.txt"], marker + ": " + json.dumps(analysis))

    def test_a_same_checkout_untracked_file_is_an_explicit_gap(self) -> None:
        marker = "SAME_CHECKOUT_UNTRACKED_SILENT"
        (self.cache / "fresh.py").write_text("def fresh():\n    return 1\n", encoding="utf-8")

        payload = self.payload(self.cli("detect-changes", "-r", str(self.cache), "--scope", "unstaged"), marker)

        analysis = payload.get("analysis", {})
        self.assertEqual(payload["changed_symbols"], [], marker + ": " + json.dumps(payload)[:600])
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        gaps = {gap["path"]: gap.get("reason", "") for gap in analysis.get("gaps", [])}
        self.assertIn("untracked", gaps.get("fresh.py", ""), marker + ": " + json.dumps(analysis))

    def test_the_staged_scope_does_not_report_untracked_files(self) -> None:
        marker = "STAGED_SCOPE_REPORTED_UNTRACKED_GAP"
        # The staged scope answers about the Git index, which cannot hold an untracked file.
        (self.cache / "svc.py").write_text(SERVICE.replace("return x + 1", "return x + 2"), encoding="utf-8")
        self.git(self.cache, "add", "svc.py")
        (self.cache / "notes.txt").write_text("scratch\n", encoding="utf-8")

        staged = self.payload(self.cli("detect-changes", "-r", str(self.cache), "--scope", "staged"), marker)
        unstaged = self.payload(self.cli("detect-changes", "-r", str(self.cache), "--scope", "unstaged"), marker)

        self.assertIn("compute", self.names(staged), marker + ": " + json.dumps(staged)[:600])
        self.assertEqual(staged["analysis"]["gaps"], [], marker + ": " + json.dumps(staged["analysis"]))
        self.assertEqual(staged["analysis"]["status"], "complete", marker + ": " + json.dumps(staged["analysis"]))
        self.assertIn("notes.txt", [gap["path"] for gap in unstaged["analysis"]["gaps"]],
                      marker + ": the working-tree scope still owns the untracked gap: " + json.dumps(unstaged["analysis"]))

    def test_a_new_untracked_file_is_an_explicit_gap(self) -> None:
        marker = "UNTRACKED_CHANGE_SILENT"
        self.edit_compute()
        (self.source / "fresh.py").write_text("def fresh():\n    return 1\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload.get("analysis", {})
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        gaps = {gap["path"]: gap.get("reason", "") for gap in analysis.get("gaps", [])}
        self.assertIn("fresh.py", gaps, marker + ": " + json.dumps(analysis))
        self.assertTrue(gaps["fresh.py"], marker + ": a gap carries its reason: " + json.dumps(analysis))
        self.assertIn("tests/test_svc.py", self.impacted_paths(payload, marker), marker)

    def test_a_failing_symbol_lookup_is_an_explicit_reason(self) -> None:
        marker = "QUERY_FAILURE_SILENT"
        # A truncated store leaves the repository registered and the metadata intact, so the
        # run reaches the graph and its queries fail: the real failure path, not a substitute.
        self.edit_compute()
        store = self.cache / ".gitnexus" / "lbug"
        store.write_bytes(store.read_bytes()[:4096])

        result = self.detect_worktree()

        self.assertTrue(result.stdout.lstrip().startswith("{"), marker + ": " + result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        analysis = payload.get("analysis", {})
        self.assertIn(analysis.get("status"), {"partial", "unavailable"},
                      marker + ": a failed lookup must never read complete: " + json.dumps(payload)[:600])
        self.assertTrue(analysis.get("reasons") or payload.get("error"),
                        marker + ": the failure must be named: " + json.dumps(payload)[:600])
        self.assertNotEqual((payload.get("changed_symbols"), analysis.get("status")), ([], "complete"), marker)

    def test_the_candidate_capture_leaves_the_worktree_staging_state_untouched(self) -> None:
        marker = "CANDIDATE_CAPTURE_MUTATED_THE_WORKTREE"
        # The producer writes a tree from the worktree; the checkout's own index and staged
        # content must survive, because a caller may be mid-commit when the trigger fires.
        (self.source / "svc.py").write_text(SERVICE.replace("return 0", "return 5"), encoding="utf-8")
        self.git(self.source, "add", "svc.py")
        self.edit_compute()
        staged_before = self.git(self.source, "diff", "--staged")
        status_before = self.git(self.source, "status", "--porcelain")
        self.assertTrue(status_before.startswith("MM"), marker + ": the fixture must hold staged and unstaged edits together")

        self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.git(self.source, "diff", "--staged"), staged_before, marker + ": staged content changed")
        self.assertEqual(self.git(self.source, "status", "--porcelain"), status_before, marker + ": working tree state changed")

    def test_a_change_outside_every_indexed_range_is_an_explicit_gap(self) -> None:
        marker = "UNATTRIBUTED_CHANGE_READ_AS_COMPLETE"
        # Content appended past the last indexed symbol: the file holds indexed symbols, but
        # the changed lines touch none of them, so nothing the graph knows explains the change.
        self.commit_and_sync({"tail.py": "def only():\n    return 1\n\n\n\n\n"})
        (self.source / "tail.py").write_text("def only():\n    return 1\n\n\n\n\ndef appended():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload.get("analysis", {})
        tail = [s["name"] for s in payload["changed_symbols"] if s["filePath"] == "tail.py"]
        self.assertEqual(tail, [], marker + ": nothing indexed was touched: " + json.dumps(payload)[:600])
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        gaps = {gap["path"]: gap.get("reason", "") for gap in analysis.get("gaps", [])}
        self.assertIn("tail.py", gaps, marker + ": " + json.dumps(analysis))
        self.assertIn("outside every indexed symbol range", gaps["tail.py"], marker + ": " + json.dumps(analysis))

    def test_a_file_ignored_only_by_the_global_excludes_is_not_captured(self) -> None:
        marker = "GLOBAL_EXCLUDES_OVERRIDDEN"
        # The checkout's own ignore configuration decides what is content; overriding
        # core.excludesFile would capture whatever the user ignores only there.
        excludes = self.tmp / "globalignore"
        excludes.write_text("secret.txt\n", encoding="utf-8")
        self.git(self.source, "config", "core.excludesFile", str(excludes))
        (self.source / "secret.txt").write_text("private\n", encoding="utf-8")
        self.assertEqual(self.git(self.source, "status", "--porcelain"), "", marker + ": git must consider the file ignored")
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        gaps = [gap["path"] for gap in payload["analysis"].get("gaps", [])]
        self.assertNotIn("secret.txt", gaps, marker + ": " + json.dumps(payload["analysis"]))
        self.assertEqual(payload["analysis"]["status"], "complete", marker + ": " + json.dumps(payload["analysis"]))
        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])

    def test_an_indexed_path_holding_the_list_delimiter_is_read(self) -> None:
        marker = "DELIMITED_REPO_PATH_SPLIT"
        # The alternate-object-directory list is delimiter separated; an indexed clone whose
        # path holds one must still be readable from the worktree.
        delimited = self.tmp / "indexed:one"
        shutil.copytree(self.cache, delimited)
        analyzed = self.cli("analyze", "--force", "--skip-agents-md", str(delimited), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, marker + ": " + analyzed.stdout + analyzed.stderr)
        self.edit_compute()

        result = self.cli("detect-changes", "-r", str(delimited), "--worktree", str(self.source))

        payload = self.payload(result, marker)
        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": " + json.dumps(payload)[:600])

    def test_a_test_reached_through_a_cobol_program_container_is_included(self) -> None:
        marker = "COBOL_CONTAINER_REACHED_TEST_MISSING"
        # Editing a paragraph reports the paragraph and its program Module; the test program
        # calls that Module, so it is reachable only through the container.
        caller = (
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID. TESTDEMO.\n"
            "       PROCEDURE DIVISION.\n"
            "       CHECK-IT.\n"
            "           CALL 'DEMO'.\n"
            "           STOP RUN.\n"
        )
        self.commit_and_sync({"prog.cbl": COBOL, "tests/test_prog.cbl": caller})
        (self.source / "prog.cbl").write_text(COBOL.replace("           DISPLAY 'B'.\n", "           DISPLAY 'C'.\n"), encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        changed = {s["id"] for s in payload["changed_symbols"] if s["filePath"] == "prog.cbl"}
        self.assertIn("Module:prog.cbl:DEMO", changed, marker + ": the program Module must be a reported container: " + json.dumps(sorted(changed)))
        self.assertIn("Module:tests/test_prog.cbl:TESTDEMO", {t["id"] for t in payload.get("impacted_tests", [])},
                      marker + ": " + json.dumps(payload.get("impacted_tests"))[:600])

    def test_a_markdown_section_reaches_no_test_in_this_graph(self) -> None:
        marker = "MARKDOWN_SECTION_REACHABILITY_UNMEASURED"
        # The contract names parent Markdown sections as containers. This records what the
        # graph actually holds for them: only downward CONTAINS edges, so nothing upstream of
        # a section can be a test, and an empty impacted-test set there is correct rather
        # than a missed edge. A future processor that adds an upstream edge fails this.
        self.commit_and_sync({"guide.md": GUIDE, "tests/test_guide.md": "# Test Guide\n\nSee [Setup](guide.md#setup).\n"})
        (self.source / "guide.md").write_text(GUIDE.replace("## Setup\n", "## Setup   \n"), encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)
        edges = self.cli("cypher", "MATCH (a)-[r:CodeRelation]->(b) WHERE b.filePath = 'guide.md' RETURN a.id AS caller, r.type AS relType",
                         "-r", str(self.cache))

        self.assertEqual(edges.returncode, 0, marker + ": " + edges.stdout + edges.stderr)
        changed = {s["id"] for s in payload["changed_symbols"] if s["filePath"] == "guide.md"}
        self.assertTrue(changed, marker + ": the edited section must be reported: " + json.dumps(payload)[:600])
        self.assertNotIn("tests/", " ".join(t["filePath"] for t in payload.get("impacted_tests", [])),
                         marker + ": a Markdown-reachable test would change this contract: " + json.dumps(payload.get("impacted_tests")))
        self.assertNotIn("tests/test_guide.md", edges.stdout,
                         marker + ": the graph gained an upstream edge into a Markdown file; the container promise is now testable positively")

    def test_a_clean_filter_on_an_untracked_file_makes_the_snapshot_unrecordable(self) -> None:
        marker = "UNTRACKED_CLEAN_FILTER_IGNORED_BY_GUARD"
        # The capture stores untracked files too, so a filter matching one rewrites bytes the
        # graph never read even though the file appears in no tracked listing.
        self.git(self.cache, "config", "filter.strip.clean", "sed '/DROP-ME/d'")
        (self.cache / ".gitattributes").write_text("scratch.py filter=strip\n", encoding="utf-8")
        self.git(self.cache, "add", ".gitattributes")
        self.git(self.cache, "commit", "-q", "-m", "attributes")
        (self.cache / "scratch.py").write_text("# DROP-ME\ndef scratch():\n    return 1\n", encoding="utf-8")
        self.assertNotEqual(self.git(self.cache, "status", "--porcelain"), "", marker + ": the file must be untracked and visible")

        self.analyze_cache("--force")

        self.assertNotIn("indexedTree", self.meta(), marker + ": " + json.dumps(self.meta()))

    def test_a_filter_attribute_without_a_clean_command_still_records_a_snapshot(self) -> None:
        marker = "DECLARED_FILTER_WITHOUT_CLEAN_REFUSED"
        # git rewrites nothing for a driver it has no clean command for, and `filter=-`
        # disables filtering outright, so neither may make snapshots unavailable.
        (self.cache / ".gitattributes").write_text("*.py filter=nodriver\nsvc.py filter=-\n", encoding="utf-8")
        self.git(self.cache, "add", ".gitattributes")
        self.git(self.cache, "commit", "-q", "-m", "declared but unconfigured")

        self.analyze_cache("--force")

        self.assertEqual(self.meta().get("indexedTree"), write_working_tree(self.cache),
                         marker + ": " + json.dumps(self.meta()))

    def test_a_driver_named_like_a_check_attr_sentinel_still_blocks_the_snapshot(self) -> None:
        marker = "SENTINEL_NAMED_DRIVER_SKIPPED"
        # check-attr prints `unspecified` for a path with no filter attribute and `unset` for
        # one that disables it, so a driver actually named `unspecified` is reported with the
        # same string. It rewrites content all the same and must block the snapshot.
        self.git(self.cache, "config", "filter.unspecified.clean", "sed '/DROP-ME/d'")
        (self.cache / ".gitattributes").write_text("svc.py filter=unspecified\n", encoding="utf-8")
        (self.cache / "svc.py").write_text("# DROP-ME\n" + SERVICE, encoding="utf-8")
        self.git(self.cache, "add", "-A")
        self.git(self.cache, "commit", "-q", "-m", "sentinel-named driver")

        self.analyze_cache("--force")

        self.assertNotIn("indexedTree", self.meta(), marker + ": " + json.dumps(self.meta()))

    def test_a_clean_filter_makes_the_snapshot_unrecordable(self) -> None:
        marker = "CLEAN_FILTER_SNAPSHOT_TRUSTED"
        # A clean filter rewrites content between the working file and the stored blob, so a
        # captured tree would describe bytes the graph never read. Recording it would map
        # hunks onto lines that were never indexed, so nothing is recorded and the later read
        # refuses for missing metadata rather than mis-attributing.
        self.git(self.cache, "config", "filter.strip.clean", "sed '/DROP-ME/d'")
        (self.cache / ".gitattributes").write_text("*.py filter=strip\n", encoding="utf-8")
        (self.cache / "svc.py").write_text("# DROP-ME\n" + SERVICE, encoding="utf-8")
        self.git(self.cache, "add", "-A")
        self.git(self.cache, "commit", "-q", "-m", "filtered")
        self.analyze_cache("--force")

        self.assertNotIn("indexedTree", self.meta(), marker + ": " + json.dumps(self.meta()))
        body = self.refusal(self.detect_worktree(), marker)
        self.assertIn("indexedTree", body["error"], marker + ": " + body["error"])

    def test_an_index_without_a_recorded_snapshot_gains_one_from_a_plain_analyze(self) -> None:
        marker = "UPGRADE_NEVER_RECORDS_SNAPSHOT"
        # An index built before this feature carries no indexedTree. A plain analyze at the
        # same HEAD must record one, or worktree mode stays unavailable on every such index.
        self.write_meta(indexedTree=None)
        self.assertNotIn("indexedTree", self.meta(), marker)

        self.analyze_cache()

        recorded = self.meta().get("indexedTree")
        self.assertEqual(recorded, write_working_tree(self.cache), marker + ": " + json.dumps(self.meta()))
        # An index that already carries it is still left alone.
        before = self.meta()
        self.analyze_cache()
        self.assertEqual(self.meta(), before, marker + ": a second plain analyze must not rewrite meta.json")

    def test_an_invalid_worktree_value_is_refused_as_json(self) -> None:
        marker = "EMPTY_WORKTREE_RESOLVED_TO_CWD"
        # An empty value must not resolve to the process working directory, and a non-string
        # value that only the MCP transport can carry must not crash the server.
        self.edit_compute()
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)

        # Run from a directory that IS a checkout root: that is the state where an empty
        # value resolves to a real checkout and would be accepted instead of refused. The
        # built entry runs there without a loader the fixture cannot resolve.
        empty = self.refusal(
            self.cli("detect-changes", "-r", str(self.cache), "--worktree", "", entry=DIST_ENTRY, cwd=self.source), marker)
        wrong_type = client.call("detect_changes", {"repo": str(self.cache), "worktree": None})

        self.assertIn("worktree", empty["error"], marker + ": " + empty["error"])
        self.assertIsNotNone(wrong_type, marker + ": the MCP server died on a non-string worktree")
        self.assertIn("error", wrong_type, marker + ": " + json.dumps(wrong_type)[:400])
        self.assertTrue(client.alive(), marker + ": the MCP server must survive a non-string worktree")

    def test_a_path_git_quotes_is_reported_as_a_gap(self) -> None:
        marker = "QUOTED_PATH_SILENTLY_DROPPED"
        # Git quotes a header holding a double quote, and the parser drops it. Under a
        # completeness claim that omission has to be visible.
        quoted = 'q"uote.py'
        self.commit_and_sync({quoted: "def one():\n    return 1\n"})
        (self.source / quoted).write_text("def one():\n    return 2\n", encoding="utf-8")
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload.get("analysis", {})
        self.assertEqual(self.names(payload), {"Service", "compute"}, marker + ": the ordinary edit must still be attributed: " + json.dumps(payload)[:600])
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        paths = [gap["path"] for gap in analysis.get("gaps", [])]
        self.assertIn(quoted, paths, marker + ": the gap must name the file itself: " + json.dumps(analysis))

    def test_the_worktree_capture_does_not_copy_an_unignored_index_database(self) -> None:
        marker = "INDEX_DATABASE_COPIED_INTO_OBJECT_STORE"
        # detect-changes captures the edited checkout's tree, and nothing in that path
        # recreates the ignore rule, so a checkout whose rule was removed after indexing is
        # the reachable case: its database must not be written into the object store.
        analyzed = self.cli("analyze", "--force", "--skip-agents-md", str(self.source), entry=DIST_ENTRY)
        self.assertEqual(analyzed.returncode, 0, marker + ": " + analyzed.stdout + analyzed.stderr)
        self.assertGreater((self.source / ".gitnexus" / "lbug").stat().st_size, 4 * 1024 * 1024, marker)
        (self.source / ".gitignore").write_text("nothing-here\n", encoding="utf-8")
        self.assertNotEqual(self.git(self.source, "status", "--porcelain"), "", marker + ": the database must now be visible to git")
        self.edit_compute()
        before = self.git(self.source, "count-objects", "-v")

        self.payload(self.detect_worktree(), marker)

        after = self.git(self.source, "count-objects", "-v")

        def size(report: str) -> int:
            return int(dict(line.split(": ") for line in report.splitlines())["size"])

        # Tight enough to discriminate: the add-then-drop capture writes about 250 KiB here.
        self.assertLess(size(after) - size(before), 64,
                        marker + f": the object store grew by {size(after) - size(before)} KiB\n{before}\n{after}")

    def test_a_committed_index_directory_is_not_in_the_captured_tree(self) -> None:
        marker = "TRACKED_INDEX_DIRECTORY_CAPTURED"
        # read-tree seeds whatever HEAD holds, so a committed .gitnexus survives the add's
        # exclusion and would land in the snapshot unless it is dropped from the tree too.
        (self.cache / ".gitignore").write_text("nothing-here\n", encoding="utf-8")
        (self.cache / ".gitnexus").mkdir(exist_ok=True)
        (self.cache / ".gitnexus" / "state").write_text("committed\n", encoding="utf-8")
        self.git(self.cache, "add", "-A", "-f")
        self.git(self.cache, "commit", "-q", "-m", "commit the index directory")
        self.assertIn(".gitnexus/state", self.git(self.cache, "ls-files"), marker + ": the fixture must commit it")

        self.analyze_cache("--force")

        tree = self.meta().get("indexedTree")
        self.assertIsNotNone(tree, marker + ": " + json.dumps(self.meta()))
        listed = self.git(self.cache, "ls-tree", "-r", "--name-only", tree)
        self.assertNotIn(".gitnexus/", listed, marker + ": the snapshot still carries the index directory")
        self.assertIn("svc.py", listed, marker + ": the snapshot must still carry the source")

    def test_a_quoted_path_with_escapes_is_decoded_in_the_gap(self) -> None:
        marker = "QUOTED_GAP_PATH_IS_A_RAW_HEADER"
        # Git quotes any name holding a control character, and escapes it C-style over bytes.
        tabbed = "ta\tb.py"
        self.commit_and_sync({tabbed: "def one():\n    return 1\n"})
        (self.source / tabbed).write_text("def one():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        paths = [gap["path"] for gap in payload["analysis"].get("gaps", [])]
        self.assertIn(tabbed, paths, marker + ": " + json.dumps(payload["analysis"]))

    def test_an_astral_character_survives_the_quoted_gap_path(self) -> None:
        marker = "ASTRAL_QUOTED_GAP_PATH_CORRUPTED"
        # With core.quotePath=false git emits non-ASCII literally but still quotes a name
        # holding a control character, so the decoder meets a whole astral character.
        astral = "ta\tb\U0001f600.py"
        self.commit_and_sync({astral: "def one():\n    return 1\n"})
        (self.source / astral).write_text("def one():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        paths = [gap["path"] for gap in payload["analysis"].get("gaps", [])]
        self.assertIn(astral, paths, marker + ": " + json.dumps(payload["analysis"]))

    def test_the_capture_does_not_copy_an_unignored_index_database(self) -> None:
        marker = "INDEX_DATABASE_COPIED_INTO_OBJECT_STORE"
        # With no ignore rule for .gitnexus, a capture that adds everything would write the
        # whole index database into the object store and then drop it from the tree.
        (self.source / ".gitignore").unlink()
        self.git(self.source, "rm", "-q", "--cached", ".gitignore")
        self.git(self.source, "commit", "-q", "-m", "unignore")
        # The first analyze creates the index database; the second one's capture is the one
        # that can copy it, because the capture runs before the pipeline rebuilds it.
        first = self.cli("analyze", "--force", "--skip-agents-md", str(self.source), entry=DIST_ENTRY)
        self.assertEqual(first.returncode, 0, marker + ": " + first.stdout + first.stderr)
        self.assertGreater((self.source / ".gitnexus" / "lbug").stat().st_size, 4 * 1024 * 1024,
                           marker + ": the fixture needs an index database large enough to measure")
        before = self.git(self.source, "count-objects", "-v")

        second = self.cli("analyze", "--force", "--skip-agents-md", str(self.source), entry=DIST_ENTRY)
        self.assertEqual(second.returncode, 0, marker + ": " + second.stdout + second.stderr)

        after = self.git(self.source, "count-objects", "-v")
        def size(report: str) -> int:
            return int(dict(line.split(": ") for line in report.splitlines())["size"])

        self.assertLess(size(after) - size(before), 1024,
                        marker + f": the object store grew by {size(after) - size(before)} KiB\n{before}\n{after}")
        self.assertIn("indexedTree", json.loads((self.source / ".gitnexus" / "meta.json").read_text(encoding="utf-8")),
                      marker + ": the snapshot must still be recorded")

    def test_incoming_edges_is_an_exact_direct_edge_count(self) -> None:
        marker = "INCOMING_EDGES_NOT_AN_EXACT_COUNT"
        # Three direct callers and one that reaches target only through a direct caller: the
        # count is edges into the symbol, so the transitive one must not raise it.
        self.commit_and_sync({
            "target.py": "def target():\n    return 1\n",
            "callers.py": ("from target import target\n\n\ndef one():\n    return target()\n\n\n"
                           "def two():\n    return target()\n\n\ndef three():\n    return target()\n\n\n"
                           "def transitive():\n    return one()\n"),
        })
        (self.source / "target.py").write_text("def target():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        counts = self.incoming(payload, marker)
        self.assertEqual(counts.get("target"), 3, marker + ": " + json.dumps(payload["changed_symbols"]))
        reached = {item["name"] for item in payload.get("impacted_tests", [])}
        self.assertNotIn("transitive", reached, marker + ": a transitive caller is not a test either")

    def test_an_unfollowed_relation_kind_is_not_counted(self) -> None:
        marker = "UNFOLLOWED_RELATION_COUNTED"
        # The walk follows CALLS/IMPORTS/EXTENDS/IMPLEMENTS and the override kinds. A file's
        # CONTAINS/DEFINES edge into a symbol is an incoming edge git's graph records but the
        # walk does not follow, so it must not raise the count.
        self.commit_and_sync({"lonely.py": "def lonely():\n    return 1\n"})
        (self.source / "lonely.py").write_text("def lonely():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)
        raw = self.cypher_rows(
            "MATCH (a)-[r:CodeRelation]->(n) WHERE n.id = 'Function:lonely.py:lonely' RETURN a.id AS caller, r.type AS relType",
            marker)

        self.assertTrue(any("DEFINES" in row or "CONTAINS" in row for row in raw),
                        marker + ": the fixture needs an unfollowed incoming edge: " + "\n".join(raw))
        self.assertEqual(self.incoming(payload, marker).get("lonely"), 0,
                         marker + ": " + json.dumps(payload["changed_symbols"]))

    def test_a_test_caller_shows_in_the_count_and_in_impacted_tests(self) -> None:
        marker = "INCOMING_EDGES_MISSING_FOR_TEST_CALLER"
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)

        self.assertGreaterEqual(self.incoming(payload, marker).get("compute", 0), 1,
                                marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertIn("test_compute", {item["name"] for item in payload["impacted_tests"]},
                      marker + ": " + json.dumps(payload["impacted_tests"])[:400])

    def test_a_production_only_caller_is_not_read_as_uncovered(self) -> None:
        marker = "CALLED_SYMBOL_READ_AS_UNCOVERED"
        # This is the case the slice exists for: callers known, none of them a test.
        self.commit_and_sync({
            "engine.py": "def engine():\n    return 1\n",
            "driver.py": "from engine import engine\n\n\ndef driver():\n    return engine()\n",
        })
        (self.source / "engine.py").write_text("def engine():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        counts = self.incoming(payload, marker)
        self.assertGreaterEqual(counts.get("engine", 0), 1, marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertEqual(payload["impacted_tests"], [], marker + ": " + json.dumps(payload["impacted_tests"]))
        self.assertEqual(payload["analysis"].get("uncovered_symbols"), 0,
                         marker + ": a symbol with a caller is not uncovered: " + json.dumps(payload["analysis"]))

    def test_an_incomplete_walk_publishes_null_rather_than_zero(self) -> None:
        marker = "UNMEASURED_COVERAGE_PUBLISHED_AS_ZERO"
        # Zero already means "no caller is known", so publishing it for a symbol nobody
        # measured states a fact that was never checked.
        #
        # The only way this suite can reach an incomplete walk is a name holding an
        # apostrophe, which breaks the depth-1 query's own escaping at local-backend.ts:2533
        # while the symbol lookup still succeeds. That is a real defect and a legitimate fix
        # target, so this attack is deliberately coupled to it: the alternatives are a
        # substituted collaborator, which the mock ban forbids, or no coverage at all.
        # Whoever fixes the escaping will see the first assertion below fail with this
        # message rather than watch the attack pass vacuously — at that point the walk can no
        # longer be made to fail through the supported Interface, and this attack should be
        # deleted with the proof gap recorded, not repaired with a fake driver.
        self.commit_and_sync({"o'ne.py": "def apostrophe():\n    return 1\n"})
        (self.source / "o'ne.py").write_text("def apostrophe():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload["analysis"]
        self.assertEqual(analysis["status"], "partial",
                         marker + ": the apostrophe no longer fails the depth-1 query, so this attack has lost its"
                         " only real driver; delete it and record the proof gap. " + json.dumps(analysis))
        self.assertIsNone(analysis["uncovered_symbols"],
                          marker + ": an unmeasured count is not zero: " + json.dumps(analysis))
        for symbol in payload["changed_symbols"]:
            self.assertIsNone(symbol["incoming_edges"],
                              marker + ": an unmeasured count is not zero: " + json.dumps(symbol))
        self.assertTrue(any("incoming_edges" in reason for reason in analysis["reasons"]),
                        marker + ": the reason must name the fields: " + json.dumps(analysis))

    def test_the_reading_rule_matches_a_path_rule_blind_spot(self) -> None:
        marker = "DESCRIPTION_PROMISES_A_FALSE_INFERENCE"
        # isTestFilePath classifies by path, so a root-level test_ file is not a test to this
        # tool. Its call still counts, impacted_tests stays empty and the file is unchanged,
        # so a description promising the test caller is in changed_symbols would be false.
        self.commit_and_sync({
            "engine.py": "def engine():\n    return 1\n",
            "test_svc.py": "from engine import engine\n\n\ndef test_engine():\n    assert engine() == 1\n",
        })
        (self.source / "engine.py").write_text("def engine():\n    return 2\n", encoding="utf-8")
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)

        payload = self.payload(self.detect_worktree(), marker)
        listed = client.request("tools/list", {})

        self.assertGreaterEqual(self.incoming(payload, marker).get("engine", 0), 1,
                                marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertEqual(payload["impacted_tests"], [], marker + ": " + json.dumps(payload["impacted_tests"]))
        self.assertNotIn("test_engine", self.names(payload),
                         marker + ": the caller is unchanged, so it is not a changed symbol either")
        self.assertIsNotNone(listed, marker + ": the MCP server gave no tool list")
        (tool,) = [t for t in listed["result"]["tools"] if t["name"] == "detect_changes"]
        self.assertNotIn("are themselves in changed_symbols", tool["description"],
                         marker + ": that inference is false for a caller the path rule does not classify")
        self.assertIn("not classified as a test by the path rule", tool["description"],
                      marker + ": " + tool["description"][-400:])

    def test_a_symbol_with_no_callers_is_counted_as_uncovered(self) -> None:
        marker = "UNCOVERED_SYMBOL_NOT_COUNTED"
        self.commit_and_sync({"orphan.py": "def orphan():\n    return 1\n"})
        (self.source / "orphan.py").write_text("def orphan():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.incoming(payload, marker).get("orphan"), 0, marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertEqual(payload["analysis"].get("uncovered_symbols"), 1, marker + ": " + json.dumps(payload["analysis"]))

    def test_the_uncovered_count_is_exact(self) -> None:
        marker = "UNCOVERED_COUNT_NOT_EXACT"
        # Two covered and two uncovered in one change set, then an all-covered change set.
        self.commit_and_sync({
            "mixed.py": ("def covered_one():\n    return 1\n\n\ndef covered_two():\n    return 2\n\n\n"
                         "def bare_one():\n    return 3\n\n\ndef bare_two():\n    return 4\n"),
            "users.py": ("from mixed import covered_one, covered_two\n\n\n"
                         "def use():\n    return covered_one() + covered_two()\n"),
        })
        (self.source / "mixed.py").write_text(
            "def covered_one():\n    return 9\n\n\ndef covered_two():\n    return 9\n\n\n"
            "def bare_one():\n    return 9\n\n\ndef bare_two():\n    return 9\n", encoding="utf-8")

        mixed = self.payload(self.detect_worktree(), marker)
        # The all-covered case edits only symbols that have callers: compute and its class.
        self.git(self.source, "checkout", "--", "mixed.py")
        self.edit_compute()
        covered = self.payload(self.detect_worktree(), marker)

        self.assertEqual(mixed["analysis"].get("uncovered_symbols"), 2,
                         marker + ": " + json.dumps({"symbols": mixed["changed_symbols"], "analysis": mixed["analysis"]})[:900])
        self.assertEqual(covered["analysis"].get("uncovered_symbols"), 0,
                         marker + ": " + json.dumps({"symbols": covered["changed_symbols"], "analysis": covered["analysis"]})[:900])

    def test_a_caller_shared_by_two_changed_symbols_counts_for_both(self) -> None:
        marker = "SHARED_CALLER_COUNTED_ONCE"
        # The walk deduplicates nodes, so a tally taken from its results would drop the
        # second attribution; the count is per edge.
        self.commit_and_sync({
            "pair.py": "def left():\n    return 1\n\n\ndef right():\n    return 2\n",
            "both.py": "from pair import left, right\n\n\ndef both():\n    return left() + right()\n",
        })
        (self.source / "pair.py").write_text("def left():\n    return 9\n\n\ndef right():\n    return 9\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        counts = self.incoming(payload, marker)
        self.assertGreaterEqual(counts.get("left", 0), 1, marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertGreaterEqual(counts.get("right", 0), 1, marker + ": " + json.dumps(payload["changed_symbols"]))

    def test_a_class_count_excludes_its_expanded_seeds(self) -> None:
        marker = "CLASS_EXPANSION_INFLATED_THE_COUNT"
        # A Class seed expands to its constructors and owning File; edges into those are not
        # edges into the class, so the count must match the class's own incoming edges.
        self.edit_compute()

        payload = self.payload(self.detect_worktree(), marker)
        followed = "'CALLS', 'IMPORTS', 'EXTENDS', 'IMPLEMENTS', 'METHOD_OVERRIDES', 'OVERRIDES', 'METHOD_IMPLEMENTS'"
        raw = self.cypher_rows(
            f"MATCH (a)-[r:CodeRelation]->(n) WHERE n.id = 'Class:svc.py:Service' AND r.type IN [{followed}] "
            "RETURN a.id AS caller, r.type AS relType", marker)

        self.assertEqual(self.incoming(payload, marker).get("Service"), len(raw),
                         marker + ": class count must equal its own followed incoming edges\n" + "\n".join(raw))

    def test_the_count_mixes_test_and_production_callers_exactly(self) -> None:
        marker = "MIXED_CALLER_COUNT_NOT_EXACT"
        # Two production callers and one test caller: the count is every followed edge, so it
        # is exactly three, and only the test one reaches impacted_tests.
        self.commit_and_sync({
            "mix.py": "def mixed():\n    return 1\n",
            "prod.py": ("from mix import mixed\n\n\ndef p_one():\n    return mixed()\n\n\n"
                        "def p_two():\n    return mixed()\n"),
            "tests/test_mix.py": "from mix import mixed\n\n\ndef test_mixed():\n    assert mixed() == 1\n",
        })
        (self.source / "mix.py").write_text("def mixed():\n    return 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        self.assertEqual(self.incoming(payload, marker).get("mixed"), 3,
                         marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertEqual({item["name"] for item in payload["impacted_tests"]}, {"test_mixed"},
                         marker + ": " + json.dumps(payload["impacted_tests"]))

    def test_a_changed_test_caller_is_a_changed_symbol_not_an_impacted_test(self) -> None:
        marker = "CHANGED_TEST_CALLER_MISREAD"
        # Editing a function and its only test caller together: the test is a seed, so it is
        # reported in changed_symbols and never in impacted_tests. A consumer must read the
        # empty set against changed_symbols, which is what the tool description now says.
        self.commit_and_sync({
            "solo.py": "def solo():\n    return 1\n",
            "tests/test_solo.py": "from solo import solo\n\n\ndef test_solo():\n    assert solo() == 1\n",
        })
        (self.source / "solo.py").write_text("def solo():\n    return 2\n", encoding="utf-8")
        (self.source / "tests" / "test_solo.py").write_text(
            "from solo import solo\n\n\ndef test_solo():\n    assert solo() == 2\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        counts = self.incoming(payload, marker)
        self.assertGreaterEqual(counts.get("solo", 0), 1, marker + ": " + json.dumps(payload["changed_symbols"]))
        self.assertEqual(payload["impacted_tests"], [],
                         marker + ": a changed test is a seed, never a result: " + json.dumps(payload["impacted_tests"]))
        self.assertIn("test_solo", self.names(payload),
                      marker + ": the changed test must be visible in changed_symbols: " + json.dumps(payload["changed_symbols"]))

    def test_a_cleared_change_set_still_carries_the_coverage_fields(self) -> None:
        marker = "EMPTY_RESULT_LACKS_COVERAGE_FIELDS"
        # One MCP session, an edit and then the edit cleared: the second answer must still
        # carry the fields, with nothing uncovered because nothing changed.
        self.edit_compute()
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)
        first = client.call("detect_changes", {"repo": str(self.cache), "worktree": str(self.source)})
        self.assertIsNotNone(first, marker + ": the MCP server gave no answer")
        self.git(self.source, "checkout", "--", "svc.py")

        second = client.call("detect_changes", {"repo": str(self.cache), "worktree": str(self.source)})

        self.assertIsNotNone(second, marker + ": the MCP server gave no second answer")
        self.assertEqual(second["changed_symbols"], [], marker + ": " + json.dumps(second)[:400])
        self.assertEqual(second.get("impacted_tests"), [], marker + ": " + json.dumps(second)[:400])
        self.assertEqual(second.get("analysis", {}).get("uncovered_symbols"), 0,
                         marker + ": " + json.dumps(second.get("analysis")))

    def test_scope_with_worktree_is_refused(self) -> None:
        marker = "SCOPE_SILENTLY_IGNORED_WITH_WORKTREE"
        self.edit_compute()

        self.refusal(self.detect_worktree("--scope", "staged"), marker)

    def test_base_ref_with_worktree_is_refused(self) -> None:
        marker = "BASE_REF_SILENTLY_IGNORED_WITH_WORKTREE"
        self.edit_compute()

        self.refusal(self.detect_worktree("--base-ref", "HEAD"), marker)

    def test_a_worktree_path_that_is_not_a_checkout_root_is_refused(self) -> None:
        marker = "BAD_WORKTREE_PATH_ACCEPTED"
        self.edit_compute()

        inside = self.refusal(self.detect_worktree(worktree=self.source / "tests"), marker)
        missing = self.refusal(self.detect_worktree(worktree=self.tmp / "nowhere"), marker)

        self.assertIn(str(self.source / "tests"), inside["error"], marker + ": " + inside["error"])
        self.assertIn(str(self.tmp / "nowhere"), missing["error"], marker + ": " + missing["error"])

    def test_the_mcp_worktree_input_matches_the_cli(self) -> None:
        marker = "MCP_WORKTREE_INPUT_MISSING"
        self.edit_compute()
        cli = self.payload(self.detect_worktree(), marker)
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)

        mcp = client.call("detect_changes", {"repo": str(self.cache), "worktree": str(self.source)})

        self.assertIsNotNone(mcp, marker + ": the MCP server gave no answer")
        self.assertEqual(normalized(mcp), normalized(cli), marker)

    def test_the_mcp_payload_carries_the_coverage_fields(self) -> None:
        marker = "MCP_PAYLOAD_LACKS_NEW_FIELDS"
        # Equality alone would pass with both sides missing the fields, so the MCP response
        # is asserted directly before the payloads are compared.
        self.edit_compute()
        cli = self.payload(self.detect_worktree(), marker)
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)

        mcp = client.call("detect_changes", {"repo": str(self.cache), "worktree": str(self.source)})

        self.assertIsNotNone(mcp, marker + ": the MCP server gave no answer")
        self.assertIn("uncovered_symbols", mcp.get("analysis", {}), marker + ": " + json.dumps(mcp.get("analysis")))
        for symbol in mcp["changed_symbols"]:
            self.assertIn("incoming_edges", symbol, marker + ": " + json.dumps(symbol))
        by_name = {s["name"]: s["incoming_edges"] for s in mcp["changed_symbols"]}
        self.assertGreaterEqual(by_name.get("compute", 0), 1, marker + ": " + json.dumps(mcp["changed_symbols"]))
        self.assertEqual(normalized(mcp), normalized(cli), marker)

    def test_the_impact_payload_carries_no_coverage_fields(self) -> None:
        marker = "IMPACT_PAYLOAD_CHANGED_S3"
        # The traversal is shared with impact, which must not gain the field at any level.
        impact = self.cli("impact", "--uid", "Function:svc.py:Service.compute", "--include-tests", "-r", str(self.cache))

        self.assertEqual(impact.returncode, 0, marker + ": " + impact.stdout + impact.stderr)
        self.assertNotIn("incoming_edges", impact.stdout, marker + ": " + impact.stdout[:600])
        by_depth = json.loads(impact.stdout).get("byDepth", {})
        reached = {item["id"] for items in by_depth.values() for item in items}
        self.assertIn("Function:tests/test_svc.py:test_compute", reached, marker + ": " + json.dumps(sorted(reached)))
        self.assertIn("Function:svc.py:helper", reached, marker + ": " + json.dumps(sorted(reached)))

    def test_the_mcp_schema_advertises_the_worktree_input(self) -> None:
        marker = "MCP_SCHEMA_LACKS_WORKTREE"
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)

        listed = client.request("tools/list", {})

        self.assertIsNotNone(listed, marker + ": the MCP server gave no answer")
        (tool,) = [tool for tool in listed["result"]["tools"] if tool["name"] == "detect_changes"]
        properties = tool["inputSchema"]["properties"]
        self.assertEqual(properties.get("worktree", {}).get("type"), "string", marker + ": " + json.dumps(properties))
        self.assertNotIn("default", properties["scope"], marker + ": a schema default for scope conflicts with worktree")

    def test_analyze_records_the_indexed_tree(self) -> None:
        marker = "INDEXED_TREE_NOT_RECORDED"
        head_tree = self.git(self.cache, "rev-parse", "HEAD^{tree}")
        self.assertEqual(self.meta().get("indexedTree"), head_tree, marker + ": " + json.dumps(self.meta()))
        self.assertEqual(self.meta()["lastCommit"], self.git(self.cache, "rev-parse", "HEAD"), marker)
        (self.cache / "overlay.py").write_text("def overlay():\n    return 1\n", encoding="utf-8")
        self.analyze_cache("--force")

        recorded = self.meta().get("indexedTree")

        self.assertNotEqual(recorded, head_tree, marker + ": the overlay tree must differ from HEAD's tree")
        self.assertEqual(recorded, write_working_tree(self.cache), marker + ": " + json.dumps(self.meta()))

    def test_analyze_without_force_at_the_same_head_keeps_the_recorded_snapshot(self) -> None:
        marker = "NOFORCE_REANALYZE_MOVED_THE_SNAPSHOT"
        before = self.meta()
        self.assertIn("indexedTree", before, marker + ": " + json.dumps(before))
        (self.cache / "overlay.py").write_text("def overlay():\n    return 1\n", encoding="utf-8")
        self.analyze_cache()
        self.assertEqual(self.meta(), before, marker + ": a no-force analyze at the same HEAD must not rewrite meta.json")
        (self.source / "overlay.py").write_text("def overlay():\n    return 1\n", encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        analysis = payload.get("analysis", {})
        self.assertEqual(analysis.get("status"), "partial", marker + ": " + json.dumps(analysis))
        self.assertIn("overlay.py", [gap["path"] for gap in analysis.get("gaps", [])], marker + ": " + json.dumps(analysis))

    def test_every_seed_in_a_wide_change_set_carries_its_own_count(self) -> None:
        marker = "WIDE_CHANGE_SET_LOSES_COUNTS"
        # Forty changed symbols in one file: each is a seed of the single walk and each must
        # come back with its own count. This does not measure cost — a wall-clock ratio would
        # measure machine load, since per-call overhead dominates 38 extra seeds — so the
        # promise that no query is added per symbol stays a recorded gap on the issue.
        many = "".join(f"def wide_{index}():\n    return {index}\n\n\n" for index in range(40))
        self.commit_and_sync({"wide.py": many})
        (self.source / "wide.py").write_text(many.replace("    return ", "    return 100 + "), encoding="utf-8")

        payload = self.payload(self.detect_worktree(), marker)

        counts = self.incoming(payload, marker)
        self.assertEqual(len(payload["changed_symbols"]), 40, marker + ": " + json.dumps(payload["summary"]))
        self.assertEqual(len(counts), 40, marker + ": every seed carries its own count")
        self.assertEqual(payload["analysis"]["uncovered_symbols"], 40,
                         marker + ": none of them has a caller: " + json.dumps(payload["analysis"]))

    def test_the_cli_completes_while_an_mcp_server_holds_the_index(self) -> None:
        marker = "CLI_STALLED_UNDER_MCP_LOCK"
        self.edit_compute()
        client = McpClient(DIST_ENTRY, self.home)
        self.addCleanup(client.close)
        opened = client.call("detect_changes", {"repo": str(self.cache)})
        self.assertIsNotNone(opened, marker + ": the MCP server could not open the index")
        self.assertIn("summary", opened, marker + ": the MCP server did not answer from the index: " + json.dumps(opened)[:300])

        started = time.monotonic()
        try:
            result = self.detect_worktree(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail(marker + ": the CLI did not return within 30s while the MCP server held the index")
        elapsed = time.monotonic() - started

        self.assertTrue(result.stdout.lstrip().startswith("{"), marker + ": " + result.stdout + result.stderr)
        body = json.loads(result.stdout)
        if result.returncode == 0:
            self.assertIn("compute", self.names(body), marker + ": " + result.stdout[:600])
        else:
            # The permitted fail-fast shape: a gap notice, never a hang or an empty success.
            self.assertEqual(result.returncode, 1, marker + ": " + result.stdout + result.stderr)
            self.assertEqual(body.get("analysis", {}).get("status"), "unavailable", marker + ": " + result.stdout[:600])
        # The 30s subprocess timeout above is the hang guard; a wall-clock bound here would
        # only add flake on a loaded runner, since the outcome assertions already prove the
        # contract. The duration is printed as the measurement the contract asks for.
        print(f"cli-under-mcp-lock elapsed={elapsed:.3f}s rc={result.returncode}")

    def test_status_reads_the_index_with_the_snapshot_field(self) -> None:
        marker = "STATUS_BROKEN_BY_META_FIELD"
        self.assertIn("indexedTree", self.meta(), marker + ": " + json.dumps(self.meta()))

        # status reads process.cwd(); the built entry runs from the indexed checkout itself.
        status = self.cli("status", entry=DIST_ENTRY, cwd=self.cache)

        self.assertEqual(status.returncode, 0, marker + ": " + status.stdout + status.stderr)
        self.assertIn(self.meta()["lastCommit"][:7], status.stdout, marker + ": " + status.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
