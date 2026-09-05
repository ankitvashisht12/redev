import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LauncherTest(unittest.TestCase):
    def test_launcher_finds_supported_python_when_python3_is_an_old_project_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            binaries = Path(temporary)
            old_python = binaries / 'python3'
            old_python.write_text('#!/bin/sh\nexit 1\n')
            old_python.chmod(0o755)
            supported = binaries / 'python3.14'
            supported.symlink_to(sys.executable)
            launcher = Path(__file__).parents[1] / 'gh-worktree-cloud'
            result = subprocess.run([str(launcher), '--version'], env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH']}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '0.1.0')

if __name__ == '__main__':
    unittest.main()
