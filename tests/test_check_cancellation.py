"""Exercise cancellation through the CLI and real process tree, without GitHub."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from redev.state import StateStore
import test_worker


class CheckCancellationTests(unittest.TestCase):
    def test_sigterm_stops_check_process_and_provider_and_saves_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'repo'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.devcontainer').mkdir()
            pid_file = base / 'check.pid'
            code = 'from pathlib import Path; import os,time; Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid())); time.sleep(60)'
            config = {'version': 1, 'checks': {'quiet': {'argv': [sys.executable, '-c', code]}}, 'sync': {}}
            (root / '.devcontainer/devcontainer.json').write_text(json.dumps({'customizations': {'redev': config}}))
            binaries = base / 'bin'
            binaries.mkdir()
            for name in ('gh', 'ssh', 'rsync'):
                executable = binaries / name
                executable.write_text(test_worker.FAKE_PROVIDER)
                executable.chmod(0o755)
            remote = base / 'remote'
            remote.mkdir()
            environment = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                           'XDG_STATE_HOME': str(base / 'state'), 'FIXTURE_REMOTE': str(remote),
                           'FIXTURE_RSYNC': shutil.which('rsync'), 'FIXTURE_PATH': os.environ['PATH']}
            cli = Path(__file__).parents[1] / 'gh-redev'
            process = subprocess.Popen([str(cli), '--root', str(root), 'check', 'quiet', '--stop'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            check_pid = None
            try:
                deadline = time.monotonic() + 20
                while not pid_file.exists():
                    self.assertIsNone(process.poll(), 'CLI exited before check started')
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.03)
                check_pid = int(pid_file.read_text())
                process.terminate()
                output = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 130, output)
                store = StateStore(root, base / 'state/redev')
                record = json.loads(Path(store.read()['lastCheck']['resultPath']).read_text())
                self.assertEqual(record['exit'], 130)
                self.assertTrue(record['stop']['confirmed'])
                details = subprocess.run(['ps', '-p', str(check_pid), '-o', 'stat='], capture_output=True, text=True)
                self.assertTrue(details.returncode != 0 or details.stdout.strip().startswith('Z'), details.stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
                if check_pid:
                    try:
                        os.kill(check_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
