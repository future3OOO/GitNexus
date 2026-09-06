"""The repository's own format and lint gates, run exactly as CI runs them.

CI enforces these on the default branch, but a pull request targeting a feature
branch never reaches that workflow, so an unformatted file or a new lint error
can land on main unseen — which is how three files and one error arrived there.
These attacks run the same two commands from the repository root, so the same
breakage fails locally in the same suite as everything else.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ESLINT_ERROR = re.compile(r"^\s+\d+:\d+\s+error\s", re.MULTILINE)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=str(ROOT), text=True, capture_output=True, check=False,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


class RepoQualityGateTests(unittest.TestCase):
    def test_every_tracked_source_file_is_formatted(self) -> None:
        marker = "FORMAT_GATE_STILL_FAILS"
        result = run("npx", "prettier", "--check", ".")

        self.assertEqual(result.returncode, 0,
                         marker + ": prettier names these files\n" + (result.stdout + result.stderr)[-1200:])

    def test_the_lint_gate_reports_no_error(self) -> None:
        marker = "LINT_ERROR_STILL_PRESENT"
        result = run("npx", "eslint", ".")

        # Warnings are the repository's standing state; an error is what fails CI.
        errors = ESLINT_ERROR.findall(result.stdout + result.stderr)
        self.assertEqual(errors, [], marker + ": " + "\n".join(
            line for line in (result.stdout + result.stderr).splitlines() if ESLINT_ERROR.match(line)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
