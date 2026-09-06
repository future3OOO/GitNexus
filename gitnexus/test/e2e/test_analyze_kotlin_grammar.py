"""Attack on Kotlin indexing through the real CLI, with the grammar loaded from its shipped prebuild.

node-gyp-build prefers a compiled build/ over prebuilds/, so the attack first pins down that this
install has no compiled binding and does have the prebuild for this platform; only then does the
indexing result say anything about the prebuild.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gitnexus.test.e2e.test_analyze_skill_generation import ENV, PACKAGE, SOURCE_ENTRY

FIXTURE = PACKAGE / "test" / "fixtures" / "lang-resolution" / "kotlin-overload-dispatch"
GRAMMAR = PACKAGE / "node_modules" / "tree-sitter-kotlin"
ARCH = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
OS = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32"}.get(platform.system())
PLATFORM = f"{OS}-{ARCH}"
# The platforms the pinned fork commit publishes a prebuild for; a missing binary on one of these is a regression.
PUBLISHED = {"linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64"}
PREBUILD = GRAMMAR / "prebuilds" / PLATFORM


class AnalyzeKotlinGrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gitnexus-kotlin-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.repo = self.home / "repo"
        shutil.copytree(FIXTURE, self.repo)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*SOURCE_ENTRY, *args], cwd=PACKAGE, text=True, capture_output=True,
                              env={**ENV, "HOME": str(self.home)}, timeout=600)

    @unittest.skipUnless(PLATFORM in PUBLISHED, "the grammar ships no prebuild for this platform")
    def test_analyze_indexes_kotlin_classes_from_the_prebuilt_grammar(self) -> None:
        marker = "KOTLIN_NOT_INDEXED"
        self.assertFalse((GRAMMAR / "build").exists(), f"{marker}: a compiled build/ would shadow the prebuild")
        self.assertTrue(list(PREBUILD.glob("*.node")), f"{marker}: no prebuild under {PREBUILD}")
        analyzed = self.cli("analyze", "--skip-git", str(self.repo))
        self.assertEqual(analyzed.returncode, 0, f"analyze failed: {analyzed.stdout[-300:]} {analyzed.stderr[-500:]}")

        found = self.cli("cypher", "--repo", str(self.repo),
                         "MATCH (n) WHERE n.name = 'SqlRepository' RETURN n.name AS name")

        self.assertEqual(found.returncode, 0, f"{marker}: cypher failed: {found.stdout[-300:]} {found.stderr[-300:]}")
        self.assertIn("SqlRepository", found.stdout, f"{marker}: {analyzed.stdout[-400:]} {analyzed.stderr[-400:]}")
