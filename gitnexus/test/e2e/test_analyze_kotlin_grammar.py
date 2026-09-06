"""Attack on Kotlin indexing through the real CLI from an install made with npm ignore-scripts."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gitnexus.test.e2e.test_analyze_skill_generation import ENV, PACKAGE, SOURCE_ENTRY

FIXTURE = PACKAGE / "test" / "fixtures" / "lang-resolution" / "kotlin-overload-dispatch"


class AnalyzeKotlinGrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gitnexus-kotlin-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.repo = self.home / "repo"
        shutil.copytree(FIXTURE, self.repo)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*SOURCE_ENTRY, *args], cwd=PACKAGE, text=True, capture_output=True,
                              env={**ENV, "HOME": str(self.home)}, timeout=600)

    def test_analyze_indexes_kotlin_classes_without_install_scripts(self) -> None:
        marker = "KOTLIN_NOT_INDEXED"
        analyzed = self.cli("analyze", "--skip-git", str(self.repo))
        self.assertEqual(analyzed.returncode, 0, f"analyze failed: {analyzed.stdout[-300:]} {analyzed.stderr[-500:]}")

        found = self.cli("cypher", "--repo", str(self.repo),
                         "MATCH (n) WHERE n.name = 'SqlRepository' RETURN n.name AS name")

        self.assertEqual(found.returncode, 0, f"{marker}: cypher failed: {found.stdout[-300:]} {found.stderr[-300:]}")
        self.assertIn("SqlRepository", found.stdout, f"{marker}: {analyzed.stdout[-400:]} {analyzed.stderr[-400:]}")
