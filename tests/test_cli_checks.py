"""The CLI keeps runner flags after -- separate from its own options."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from redev.__main__ import main
from redev.transport import GitHubTransport


class CheckCliTests(unittest.TestCase):
    def test_cli_passes_named_batch_and_lifecycle_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            import os
            previous = Path.cwd()
            try:
                with patch('redev.__main__.Application') as application:
                    application.return_value.check.return_value = 23
                    code = main(['--root', str(root), 'check', 'types', 'unit', '--stop', '--codespace', 'existing', '--branch', 'feature', '--machine', 'large'])
                self.assertEqual(code, 23)
                application.return_value.check.assert_called_once_with(['types', 'unit'], stop=True, arguments=[], codespace='existing', replace=False, branch='feature', machine='large')
            finally:
                os.chdir(previous)

    def test_cli_passes_literal_flags_after_separator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            import os
            previous = Path.cwd()
            try:
                with patch('redev.__main__.Application') as application:
                    application.return_value.check.return_value = 0
                    code = main(['--root', str(root), 'check', 'unit', '--stop', '--', '--test-name', 'two words', '--stop'])
                self.assertEqual(code, 0)
                self.assertEqual(application.return_value.check.call_args.kwargs['arguments'], ['--test-name', 'two words', '--stop'])
            finally:
                os.chdir(previous)


class TransportLogTests(unittest.TestCase):
    def test_remote_output_is_streamed_and_saved_without_losing_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            transport = GitHubTransport(directory)
            transport.log_path = directory / 'output.log'
            transport.runner = 'unused'
            transport.base = 'unused'
            transport.ssh_args = lambda command: [sys.executable, '-c', 'import sys; print("normal"); print("error",file=sys.stderr); raise SystemExit(23)']
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = transport.run({'checks': ['types']})
            self.assertEqual(code, 23)
            self.assertIn('normal', output.getvalue())
            self.assertIn('error', transport.log_path.read_text())
            self.assertEqual(transport.log_path.stat().st_mode & 0o777, 0o600)

class QuietCommandTests(unittest.TestCase):
    def test_activity_output_ends_when_command_finishes(self):
        from redev import runner
        import os
        import time
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            lock = os.open(source / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with patch('redev.runner.ACTIVITY_INTERVAL', 0.03), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(runner.command([sys.executable, '-c', 'import time; time.sleep(0.12)'], source, os.environ.copy(), lock), 0)
                    count_at_exit = output.getvalue().count('still running')
                    time.sleep(0.06)
                    self.assertEqual(output.getvalue().count('still running'), count_at_exit)
                self.assertGreater(count_at_exit, 0)
            finally:
                os.close(lock)


if __name__ == '__main__':
    unittest.main()
