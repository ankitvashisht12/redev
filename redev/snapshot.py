"""Source snapshots and a separate, guarded generated-source return path."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .config import relative_path


class SnapshotError(RuntimeError):
    pass


class SourceChanged(SnapshotError):
    pass


BLOCKED_PARTS = {'.git', '.hg', '.svn', 'node_modules', '.pnpm-store', '.next', '.turbo',
                 'dist', 'build', 'out', 'coverage', '.convex', '.ssh', '.aws', '.azure',
                 '.config', '.cache', '.gnupg', '.npm', '.yarn', '.venv', 'venv', '__pycache__', 'credentials', '.credentials', '.vercel', '.netrc', '.npmrc', '.pypirc',
                 'credentials.json', 'id_rsa', 'id_ed25519', '.redev', '.agents', '.claude', '.codex'}


def under(path, prefixes):
    return any(path == prefix or path.startswith(prefix + '/') for prefix in prefixes)


def sensitive(path):
    parts = path.split('/')
    return (any(part in BLOCKED_PARTS or (part.startswith('.env') and part != '.env.example') for part in parts)
            or any(part.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.tsbuildinfo', '.log')) for part in parts))


def excluded(path, config):
    return sensitive(path) or under(path, config.get('sync', {}).get('exclude', []) + config.get('sync', {}).get('generated', []))


def safe_file(root, relative):
    relative_path(relative)
    candidate = root / relative
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise SnapshotError(f'Symlinks are not supported by source sync: {relative}')
    return candidate


def file_record(path):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotError(f'Only regular files can be synchronized: {path}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino):
        raise SourceChanged(f'File changed while reading: {path}')
    return {'sha256': digest.hexdigest(), 'mode': 0o755 if before.st_mode & 0o111 else 0o644}


def source_files(root, config):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], check=True, capture_output=True)
    for name in sorted(set(os.fsdecode(name) for name in result.stdout.split(b'\0') if name)):
        if excluded(name, config):
            continue
        path = safe_file(root, name)
        if path.is_dir():
            index_entry = subprocess.run(['git', '-C', str(root), 'ls-files', '--stage', '--', name], check=True, capture_output=True).stdout
            if index_entry.startswith(b'160000 '):
                raise SnapshotError(f'Git submodules are not supported by source sync: {name}')
            continue
        if path.exists():
            yield name, path


def source_manifest(root, config):
    return {name: file_record(path) for name, path in source_files(root, config)}


def manifest_id(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def change_stamp(root, config):
    return [(name, path.stat().st_mtime_ns, path.stat().st_ctime_ns, path.stat().st_size)
            for name, path in source_files(root, config)]


class Snapshot:
    def __init__(self, root, config):
        self.temporary = tempfile.TemporaryDirectory(prefix='redev-snapshot-')
        self.directory = Path(self.temporary.name)
        try:
            for attempt in range(3):
                try:
                    manifest = source_manifest(root, config)
                    for name, record in manifest.items():
                        target = self.directory / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(safe_file(root, name), target)
                        target.chmod(record['mode'])
                        if file_record(target) != record:
                            raise SourceChanged(f'File changed while copying: {name}')
                    if source_manifest(root, config) != manifest:
                        raise SourceChanged('Source changed while making the snapshot')
                    self.manifest = manifest
                    self.identity = manifest_id(manifest)
                    break
                except (SourceChanged, FileNotFoundError):
                    for child in self.directory.iterdir():
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                    if attempt == 2:
                        raise SnapshotError('Source is changing too fast for a coherent snapshot. Save edits and retry.')
        except BaseException:
            self.temporary.cleanup()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temporary.cleanup()


def generated_manifest(root, config):
    records = {}
    for prefix in config.get('sync', {}).get('generated', []):
        directory = safe_file(root, prefix)
        if not directory.exists():
            continue
        paths = [directory] if directory.is_file() else directory.rglob('*')
        for path in paths:
            relative = path.relative_to(root).as_posix()
            safe_file(root, relative)
            if path.is_file():
                if sensitive(relative):
                    raise SnapshotError(f'Generated output contains a protected path: {relative}')
                records[relative] = file_record(path)
    return records


def accept_generated(root, exported, config, baseline, manifest):
    allowed = config.get('sync', {}).get('generated', [])
    for name in manifest:
        if not under(name, allowed) or sensitive(name):
            raise SnapshotError(f'Remote generated path is not allowed: {name}')
    actual = generated_manifest(exported, config)
    if actual != manifest:
        raise SnapshotError('Generated download does not match the remote manifest')
    current = generated_manifest(root, config)
    if current != baseline:
        raise SnapshotError('Generated files changed locally. Remote output was kept in the private state directory; no local files were changed.')
    # No deletes on the return path: a generator can omit outputs temporarily.
    for name, record in manifest.items():
        target = safe_file(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = baseline.get(name)
        if (file_record(target) if target.exists() else None) != expected:
            raise SnapshotError(f'Local generated file changed: {name}')
        descriptor, temporary = tempfile.mkstemp(prefix='.redev-', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write((exported / name).read_bytes())
            os.chmod(temporary, record['mode'])
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
