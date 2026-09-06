"""Attack on the build placing the LadybugDB native binary when dependency install scripts were skipped.

@ladybugdb/core ships its binary in a platform package and copies it into itself from an install
script; under npm ignore-scripts that copy never runs. The build must place it instead.
"""

from __future__ import annotations

import subprocess
import unittest

from gitnexus.test.e2e.test_analyze_skill_generation import ENV, PACKAGE

BINARY = PACKAGE / "node_modules" / "@ladybugdb" / "core" / "lbugjs.node"


class BuildPlacesLadybugBinaryTests(unittest.TestCase):
    def test_build_places_the_binary_from_the_platform_package(self) -> None:
        marker = "LADYBUG_BINARY_NOT_PLACED"
        self.assertTrue(BINARY.exists(), f"{marker}: this install has no binary to move aside")
        aside = BINARY.with_name("lbugjs.node.aside")
        BINARY.rename(aside)
        try:
            built = subprocess.run(["npm", "run", "build", "--silent"], cwd=PACKAGE, text=True,
                                   capture_output=True, env=ENV, timeout=900)
            self.assertEqual(built.returncode, 0, f"build failed: {built.stdout[-300:]} {built.stderr[-500:]}")

            self.assertTrue(BINARY.exists(), f"{marker}: {BINARY} absent after npm run build")
        finally:
            if BINARY.exists():
                aside.unlink()
            else:
                aside.rename(BINARY)
