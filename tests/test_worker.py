import json
import socket
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


FAKE_PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
base = pathlib.Path(os.environ['FIXTURE_REMOTE'])
state_path = base / 'provider.json'
state = json.loads(state_path.read_text()) if state_path.exists() else []
def remote_path(value):
    return value.replace('/workspaces/.redev', str(base / 'worktrees'))
if name == 'gh':
    if args[:2] == ['api','user']:
        print('fixture-user')
    elif args[:2] == ['repo','view']:
        print('example/project')
    elif args[:2] == ['api','repos/example/project/codespaces/machines']:
        print(json.dumps({'machines':[{'name':'fixture-machine','cpus':2,'memory_in_bytes':8589934592}]}))
    elif args[:2] == ['codespace','list']:
        print(json.dumps(state))
    elif args[:2] == ['codespace','create']:
        state=[{'name':'fixture-space','displayName':args[args.index('--display-name')+1], 'state':'Available','repository':{'nameWithOwner':'example/project'}}]
        state_path.write_text(json.dumps(state)); print('fixture-space')
    elif args[:2] == ['codespace','ssh'] and '--config' in args:
        print('Host cs.fixture\n  User fixture\n  Hostname fixture')
    elif args[:2] == ['codespace','cp']:
        target=pathlib.Path(remote_path(args[-1].removeprefix('remote:')))
        target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(args[-2],target)
    elif args[:3] == ['codespace','ports','forward']:
        import socket, signal, time
        listeners=[]
        for value in args:
            if ':' in value:
                remote,local=value.split(':')
                if remote != local: sys.exit('fixture needs equal port mapping')
                listener=socket.socket(); listener.bind(('127.0.0.1',int(local))); listener.listen(); listeners.append(listener)
        signal.signal(signal.SIGTERM, lambda *unused: sys.exit(0))
        while True: time.sleep(1)
    elif args[:2] == ['codespace','stop']:
        state[0]['state']='Shutdown'; state_path.write_text(json.dumps(state))
    else:
        sys.exit('unsupported gh boundary: '+repr(args))
elif name == 'ssh':
    sys.exit(subprocess.run(['bash','-c',remote_path(args[-1])]).returncode)
elif name == 'rsync':
    source=remote_path(args[-2].removeprefix('cs.fixture:'))
    target=remote_path(args[-1].removeprefix('cs.fixture:'))
    # Exercise the real rsync filesystem behavior with local transport endpoints.
    real_args=[os.environ['FIXTURE_RSYNC'],'-rtp','--checksum']
    for arg in args:
        if arg.startswith('--copy-dest='): real_args.append(remote_path(arg))
    sys.exit(subprocess.run(real_args+[source,target], env={**os.environ, 'PATH': os.environ['FIXTURE_PATH']}).returncode)
'''


class WorkerIntegrationTest(unittest.TestCase):
    def test_background_watcher_syncs_edits_and_deletions_then_stops(self):
        self.exercise_workflow({})

    def test_local_forwarding_binds_selected_port_and_stops_with_worker(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.exercise_workflow({'web': port})
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))

    def exercise_workflow(self, ports):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'repo'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.devcontainer').mkdir()
            config = {'version': 1, 'setup': 'true', 'checks': {'types': 'cat live.txt'}, 'services': [], 'ports': ports, 'sync': {}}
            (root / '.devcontainer/devcontainer.json').write_text(json.dumps({'customizations': {'redev': config}}))
            (root / 'live.txt').write_text('before')
            (root / 'delete.txt').write_text('remove me')
            binaries = base / 'bin'
            binaries.mkdir()
            for name in ('gh', 'ssh', 'rsync'):
                path = binaries / name
                path.write_text(FAKE_PROVIDER)
                path.chmod(0o755)
            remote = base / 'remote'
            remote.mkdir()
            env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                   'XDG_STATE_HOME': str(base / 'state'), 'FIXTURE_REMOTE': str(remote), 'FIXTURE_RSYNC': shutil.which('rsync'), 'FIXTURE_PATH': os.environ['PATH']}
            cli = Path(__file__).parents[1] / 'gh-redev'
            def invoke(*args):
                return subprocess.run([str(cli), '--root', str(root), *args], env=env, capture_output=True, text=True, timeout=90)
            try:
                response = invoke('up')
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                state = json.loads(invoke('status', '--json').stdout)
                self.assertTrue(state['workerRunning'])
                self.assertEqual(state['ports'], ports)
                for port in ports.values():
                    with socket.create_connection(('127.0.0.1', port), timeout=1):
                        pass
                source = remote / 'worktrees' / state['id'] / 'source'
                (root / 'live.txt').write_text('after')
                (root / 'delete.txt').unlink()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if (source / 'live.txt').read_text() == 'after' and not (source / 'delete.txt').exists():
                        break
                    time.sleep(0.1)
                self.assertEqual((source / 'live.txt').read_text(), 'after')
                self.assertFalse((source / 'delete.txt').exists())
                response = invoke('check', 'types')
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                self.assertIn('after', response.stdout)
            finally:
                response = invoke('stop')
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
            state = json.loads(invoke('status', '--json').stdout)
            self.assertFalse(state['workerRunning'])
            self.assertEqual(state['codespaceState'], 'Shutdown')
            self.assertEqual((source / 'live.txt').read_text(), 'after')
            self.assertEqual(invoke('enabled').returncode, 0)
            self.assertEqual(invoke('disable').returncode, 0)
            self.assertEqual(invoke('enabled').returncode, 3)

if __name__ == '__main__':
    unittest.main()
