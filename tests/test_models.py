"""Quality profiles: picked from the hardware, saved per machine, safe while downloading."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import aisettings as AI
from mnemo import models as M


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "ai.json"
        self.patch = mock.patch.object(M, "SETTINGS_FILE", self.tmp)
        self.patch.start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MNEMO_PROFILE", None)
        os.environ.pop("MNEMO_ROUTER_MODEL", None)

    def tearDown(self):
        self.patch.stop()
        self.env.stop()
        M.apply(M.LEGACY)

    def test_auto_profile_follows_graphics_memory(self):
        self.assertEqual(M.auto_profile(0), "fast")
        self.assertEqual(M.auto_profile(4), "fast")
        self.assertEqual(M.auto_profile(6), "balanced")
        self.assertEqual(M.auto_profile(24), "balanced")     # "Thorough" is a choice, not a default

    def test_choice_is_saved_and_read_back(self):
        self.assertEqual(M.chosen_profile(), "auto")
        M.save_profile("best")
        self.assertEqual(M.chosen_profile(), "best")
        M.save_profile("nonsense")
        self.assertEqual(M.chosen_profile(), "auto")

    def test_missing_brain_falls_back_to_one_that_is_here(self):
        M.save_profile("fast")
        self.assertEqual(M.resolve(installed={"nomic-embed-text:latest"}).name, "legacy")
        fast, balanced = M.PROFILES["fast"].brain, M.PROFILES["balanced"].brain
        self.assertEqual(M.resolve(installed={fast}).name, "fast")
        # Fast's brain still downloading: use the one that's here meanwhile.
        self.assertEqual(M.resolve(installed={balanced}).name, "balanced")

    def test_environment_wins(self):
        os.environ["MNEMO_PROFILE"] = "fast"
        M.save_profile("best")
        self.assertEqual(M.chosen_profile(), "fast")
        os.environ["MNEMO_ROUTER_MODEL"] = "my-model:1b"
        M.apply(M.PROFILES["fast"])
        self.assertEqual(M.ROUTER_MODEL, "my-model:1b")

    def test_models_in_use_cannot_be_removed(self):
        M.apply(M.PROFILES["balanced"])
        brain = M.PROFILES["balanced"].brain
        out = AI.remove(brain)
        self.assertFalse(out["ok"])
        self.assertIn("in use", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
