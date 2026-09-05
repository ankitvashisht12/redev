import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from redev.config import read_config, ConfigError
from redev.snapshot import Snapshot, source_manifest, generated_manifest, accept_generated, SnapshotError
from redev.state import StateStore, worktree_id


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        git(self.root, 'init', '-q')
        (self.root / '.devcontainer').mkdir()
        self.config = {'version': 1, 'setup': 'true', 'checks': {'types': 'exit 0'},
                       'services': [], 'ports': {'web': 3040},
                       'sync': {'exclude': ['private'], 'generated': ['generated/client']}}
        self.write_config()

    def write_config(self):
        (self.root / '.devcontainer/devcontainer.json').write_text(json.dumps({'customizations': {'redev': self.config}}))

    def test_jsonc_namespace_accepts_comments_without_damaging_strings(self):
        path = self.root / '.devcontainer/devcontainer.json'
        path.write_text('// header\n{"customizations":{"redev":{"version":1,"setup":"echo https://host/a,}","checks":{"types":"true",},"services":[],"ports":{},"sync":{},},},}')
        self.assertEqual(read_config(self.root)['setup'], 'echo https://host/a,}')

    def test_invalid_config_fails_before_any_command(self):
        for field, value in [('version', 2), ('checks', {'types': []}), ('ports', {'web': 80}), ('unknownSetting', True), ('sync', {'generated': ['../outside']})]:
            with self.subTest(field=field):
                original = self.config.copy()
                self.config[field] = value
                self.write_config()
                with self.assertRaises(ConfigError):
                    read_config(self.root)
                self.config = original

    def test_snapshot_contains_uncommitted_untracked_and_deletions(self):
        (self.root / 'old.ts').write_text('old')
        git(self.root, 'add', '.')
        (self.root / 'old.ts').unlink()
        (self.root / 'new.ts').write_text('untracked')
        with Snapshot(self.root, self.config) as snapshot:
            self.assertNotIn('old.ts', snapshot.manifest)
            self.assertEqual((snapshot.directory / 'new.ts').read_text(), 'untracked')
            self.assertEqual(snapshot.manifest, source_manifest(self.root, self.config))

    def test_unstaged_file_to_directory_change_includes_new_children(self):
        (self.root / 'module').write_text('old file')
        git(self.root, 'add', 'module')
        (self.root / 'module').unlink()
        (self.root / 'module').mkdir()
        (self.root / 'module/index.ts').write_text('new source')
        with Snapshot(self.root, self.config) as snapshot:
            self.assertNotIn('module', snapshot.manifest)
            self.assertIn('module/index.ts', snapshot.manifest)
            self.assertEqual((snapshot.directory / 'module/index.ts').read_text(), 'new source')

    def test_sensitive_files_excluded_even_when_tracked(self):
        for name in ['.env.local', '.npmrc', 'credentials.json', 'secret.pem', 'secret.PEM', 'keys.PEM/file', 'credentials/token', '.gnupg/key', 'node_modules/pkg/a.js', '.convex/db.sqlite3', 'private/token', 'generated/client/api.d.ts']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('secret')
        git(self.root, 'add', '-f', '.')
        (self.root / '.gitignore').write_text('ignored/\n')
        (self.root / 'ignored').mkdir()
        (self.root / 'ignored/file').write_text('ignored')
        manifest = source_manifest(self.root, self.config)
        self.assertEqual(set(manifest), {'.gitignore', '.devcontainer/devcontainer.json'})

    def test_symlink_source_is_rejected_without_reading_target(self):
        (self.root / 'link.ts').symlink_to(self.base / 'outside')
        with self.assertRaises(SnapshotError):
            source_manifest(self.root, self.config)

    def test_generated_return_refuses_newer_local_edits(self):
        target = self.root / 'generated/client/api.d.ts'
        target.parent.mkdir(parents=True)
        target.write_text('baseline')
        baseline = generated_manifest(self.root, self.config)
        exported = self.base / 'exported'
        remote = exported / 'generated/client/api.d.ts'
        remote.parent.mkdir(parents=True)
        remote.write_text('remote')
        manifest = generated_manifest(exported, self.config)
        target.write_text('local edit')
        with self.assertRaises(SnapshotError):
            accept_generated(self.root, exported, self.config, baseline, manifest)
        self.assertEqual(target.read_text(), 'local edit')

    def test_generated_return_installs_new_files_and_keeps_unrelated_files(self):
        baseline = generated_manifest(self.root, self.config)
        exported = self.base / 'exported'
        remote = exported / 'generated/client/api.d.ts'
        remote.parent.mkdir(parents=True)
        remote.write_text('remote types')
        (self.root / 'keep.ts').write_text('keep')
        accept_generated(self.root, exported, self.config, baseline, generated_manifest(exported, self.config))
        self.assertEqual((self.root / 'generated/client/api.d.ts').read_text(), 'remote types')
        self.assertEqual((self.root / 'keep.ts').read_text(), 'keep')

    def test_mapping_uses_real_worktree_path_and_private_permissions(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        other = self.base / 'other'
        other.mkdir()
        self.assertEqual(worktree_id(alias), worktree_id(self.root))
        self.assertNotEqual(worktree_id(other), worktree_id(self.root))
        store = StateStore(self.root, self.base / 'state')
        with store.lock():
            store.save({'enabled': True, 'codespace': 'fixture'})
        self.assertEqual(store.read()['codespace'], 'fixture')
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)

if __name__ == '__main__':
    unittest.main()
