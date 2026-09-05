"""Private worktree state and advisory process locks."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def worktree_root(directory):
    result = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Run this command inside a Git worktree')
    return Path(result.stdout.strip()).resolve()


def worktree_id(root):
    return hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()[:32]


def state_home():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'gh-worktree-cloud'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


@contextmanager
def file_lock(path, timeout=300):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, 'a+') as stream:
        os.chmod(path, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('A sync or check is still running. Wait, or stop that command before retrying.')
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class StateStore:
    def __init__(self, root, home=None):
        self.root = Path(root).resolve()
        self.home = Path(home) if home else state_home()
        self.identity = worktree_id(root)
        self.directory = self.home / 'worktrees' / self.identity
        self.path = self.directory / 'state.json'

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or (value.get('root') and value['root'] != str(self.root)):
                raise ValueError('worktree mapping does not match this directory')
            return value
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            raise RuntimeError(f'Cannot read private state {self.path}: {error}') from error

    def save(self, value):
        value['root'] = str(self.root)
        value['id'] = self.identity
        write_json(self.path, value)

    def lock(self, timeout=300):
        return file_lock(self.directory / 'operation.lock', timeout)

    def allocate_ports(self, preferred, previous=None, overrides=None):
        with file_lock(self.home / 'ports.lock'):
            reserved = set()
            for path in (self.home / 'worktrees').glob('*/state.json'):
                if path == self.path:
                    continue
                other = json.loads(path.read_text())
                if other.get('enabled'):
                    reserved.update(other.get('ports', {}).values())
            selected = {}
            for name, default in preferred.items():
                explicit = (overrides or {}).get(name)
                saved = (previous or {}).get(name)
                candidate = explicit or saved or default
                while candidate in reserved or not port_available(candidate):
                    if explicit or saved:
                        raise RuntimeError(f'Port {candidate} for {name} is in use. Stop its owner or use --port {name}=NUMBER.')
                    candidate += 1
                    if candidate > 65535:
                        raise RuntimeError('No free local port found')
                reserved.add(candidate)
                selected[name] = candidate
            state = self.read()
            state.update(enabled=True, ports=selected)
            self.save(state)
            return selected


def port_available(port):
    with socket.socket() as server:
        try:
            server.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False
