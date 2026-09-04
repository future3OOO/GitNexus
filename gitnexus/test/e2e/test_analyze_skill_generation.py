"""Attacks on `gitnexus analyze`'s side effects in the analyzed checkout, through the real CLI."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
# The loader as a file URL, as test/integration/skills-e2e.test.ts does: analyze spawns a worker that
# inherits execArgv from another cwd, so a bare "tsx" specifier would not resolve there.
TSX_LOADER = (PACKAGE / "node_modules" / "tsx" / "dist" / "loader.mjs").as_uri()
SOURCE_ENTRY = ["node", "--import", TSX_LOADER, str(PACKAGE / "src" / "cli" / "index.ts")]
# analyze re-execs itself for a bigger heap without the loader flag unless NODE_OPTIONS already carries one.
ENV = {**os.environ, "NODE_OPTIONS": "--max-old-space-size=8192"}
FIXTURE = PACKAGE / "test" / "fixtures" / "mini-repo"
COPIES = ".claude/skills/gitnexus"


class AnalyzeSkillGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gitnexus-analyze-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.repo = self.home / "repo"
        shutil.copytree(FIXTURE, self.repo, ignore=shutil.ignore_patterns(".gitnexus", ".claude"))

    def analyze(self, *flags: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([*SOURCE_ENTRY, "analyze", "--skip-git", *flags, str(self.repo)], cwd=PACKAGE, text=True,
                                capture_output=True, env={**ENV, "HOME": str(self.home)}, timeout=600)
        self.assertEqual(result.returncode, 0, f"analyze failed: {result.stdout[-300:]} {result.stderr[-500:]}")
        return result

    def copies(self) -> list[str]:
        root = self.repo / COPIES
        return sorted(str(p.relative_to(self.repo)) for p in root.rglob("SKILL.md")) if root.exists() else []

    def test_analyze_writes_no_skill_copies_into_the_checkout(self) -> None:
        marker = "ANALYZE_STILL_GENERATES_SKILLS"
        self.analyze()
        self.assertEqual(self.copies(), [], marker + f" (default analyze): {self.copies()}")
        self.analyze("--force")
        self.assertEqual(self.copies(), [], marker + f" (forced re-analyze): {self.copies()}")

    def test_analyze_leaves_installed_skills_alone(self) -> None:
        marker = "ANALYZE_OVERWROTE_INSTALLED_SKILL"
        mine = self.repo / COPIES / "gitnexus-guide" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("# my own guide\n", encoding="utf-8")
        self.analyze()
        self.assertEqual(mine.read_text(encoding="utf-8"), "# my own guide\n", marker)
        self.assertEqual(self.copies(), ["%s/gitnexus-guide/SKILL.md" % COPIES], marker + f": {self.copies()}")

    def test_default_analyze_upserts_context_files(self) -> None:
        marker = "CONTEXT_FILES_POINT_AT_REPO_COPIES"
        self.analyze()
        for name in ("AGENTS.md", "CLAUDE.md"):
            text = (self.repo / name).read_text(encoding="utf-8")
            self.assertIn("# GitNexus", text, f"{name} carries no GitNexus section")
            self.assertNotIn(COPIES + "/", text, marker + f" ({name})")
            self.assertIn("gitnexus setup", text, marker + f" ({name} does not say how skills are installed)")

    def test_setup_installs_skills_and_analyze_leaves_them(self) -> None:
        marker = "SETUP_SKILLS_BROKEN"
        (self.home / ".claude").mkdir()
        setup = subprocess.run([*SOURCE_ENTRY, "setup"], cwd=PACKAGE, text=True, capture_output=True,
                               env={**ENV, "HOME": str(self.home)}, timeout=300)
        self.assertEqual(setup.returncode, 0, marker + f": setup failed {setup.stderr[-300:]}")
        installed = self.home / ".claude" / "skills"
        canonical = (PACKAGE / "skills" / "gitnexus-guide.md").read_text(encoding="utf-8")
        self.assertEqual(len(list(installed.rglob("SKILL.md"))), 7, marker + " (setup installed fewer than 7 skills)")
        self.assertEqual((installed / "gitnexus-guide" / "SKILL.md").read_text(encoding="utf-8"), canonical, marker + " (installed content differs)")
        self.analyze()
        self.assertEqual(len(list(installed.rglob("SKILL.md"))), 7, marker + " (analyze changed the installed skills)")
        self.assertEqual((installed / "gitnexus-guide" / "SKILL.md").read_text(encoding="utf-8"), canonical, marker + " (analyze rewrote an installed skill)")


if __name__ == "__main__":
    unittest.main()
