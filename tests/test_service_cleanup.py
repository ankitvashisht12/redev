"""Cleanup must retain permission failures for live service groups."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from redev import runner


class ServiceCleanupTests(unittest.TestCase):
    def stop_with_group_state(self, group_state, already_stopped=False):
        state = {"services": [{"pid": 1234, "identity": "fixture"}]}
        details = {"group": 1234, "identity": "fixture", "zombie": True}
        response = subprocess.CompletedProcess([], 0, "1234 " + group_state + "\n", "")
        signals = [PermissionError("denied")] if already_stopped else [None, PermissionError("denied")]
        with patch.object(runner, "process_details", return_value=details), \
                patch.object(runner, "service_alive", return_value=False), \
                patch.object(runner.os, "killpg", side_effect=signals), \
                patch.object(runner.subprocess, "run", return_value=response), \
                patch.object(runner.time, "sleep"), patch.object(runner, "atomic_json"):
            runner.stop_services(Path("unused"), state)

    def test_zombie_only_group_does_not_mask_original_service_error(self):
        self.stop_with_group_state("Z")

    def test_live_child_permission_failure_is_not_hidden(self):
        with self.assertRaises(PermissionError):
            self.stop_with_group_state("S")

    def test_repeated_cleanup_accepts_zombie_only_group(self):
        self.stop_with_group_state("Z", already_stopped=True)

    def test_initial_live_group_permission_failure_is_not_hidden(self):
        with self.assertRaises(PermissionError):
            self.stop_with_group_state("S", already_stopped=True)


if __name__ == "__main__":
    unittest.main()
