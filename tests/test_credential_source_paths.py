"""Credential-management source is distinct from credential data."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from redev import runner
from redev.snapshot import SnapshotError, accept_generated, generated_manifest, sensitive
import test_check_workflow
import test_runner


CODE_PATHS = [
    'src/accounts/credentials/mutations.ts', 'credentials/nested/client.TSX',
    'src/credentials/loader.PY', 'credentials/client.js', 'credentials/client.go',
]
SECRET_PATHS = [
    'credentials', 'credentials/token', 'credentials/session.json',
    'credentials/settings.yaml', 'credentials/passwords.txt', 'credentials/profile.ini',
    'credentials/private.pem', 'credentials/.env', 'credentials/.env.local',
    'credentials/.credentials/client.ts', '.credentials/client.ts',
    'credentials/.ssh/client.ts', 'credentials/keys.PEM/client.ts',
    'credentials/credentials.json', 'credentials/id_ed25519',
]


class CredentialPathTests(unittest.TestCase):
    def test_local_and_remote_protection_agree_on_code_and_secret_paths(self):
        for relative in CODE_PATHS:
            with self.subTest(relative=relative):
                self.assertFalse(sensitive(relative))
                self.assertFalse(runner.protected(relative))
                self.assertTrue(runner.protected(relative, ['credentials', 'src']))
        for relative in SECRET_PATHS:
            with self.subTest(relative=relative):
                self.assertTrue(sensitive(relative))
                self.assertTrue(runner.protected(relative))

    def test_generated_return_rejects_credential_data_before_touching_local_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in SECRET_PATHS:
                with self.subTest(relative=relative), self.assertRaises(SnapshotError):
                    accept_generated(root, root / 'unused-export', {'sync': {'generated': ['credentials', '.credentials']}},
                                     {}, {relative: {'sha256': '0' * 64, 'mode': 0o644}})

    def test_generated_code_cannot_bypass_an_explicit_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = 'credentials/private/client.ts'
            source = root / relative
            source.parent.mkdir(parents=True)
            source.write_text('synthetic private code')
            config = {'sync': {'generated': ['credentials'], 'exclude': ['credentials/private']}}
            with self.assertRaises(SnapshotError):
                generated_manifest(root, config)
            with self.assertRaises(SnapshotError):
                accept_generated(root, root / 'unused-export', config, {},
                                 {relative: {'sha256': '0' * 64, 'mode': 0o644}})


class CredentialSourceWorkflowTests(unittest.TestCase):
    setUp = test_check_workflow.CheckWorkflowTests.setUp
    write_config = test_check_workflow.CheckWorkflowTests.write_config

    def test_credential_source_reaches_remote_validation_and_can_be_removed(self):
        relative = 'src/accounts/credentials/mutations.ts'
        code = self.root / relative
        code.parent.mkdir(parents=True)
        code.write_text('export const fixture = true')
        blocked = [path for path in SECRET_PATHS if path != 'credentials']
        for path in blocked:
            secret = self.root / path
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text('synthetic secret fixture')
        self.config['checks']['source'] = {'argv': [sys.executable, '-c',
            'from pathlib import Path; import sys; assert Path(sys.argv[1]).read_text() == "export const fixture = true"', relative]}
        self.write_config()
        self.assertEqual(self.app.check(['source'], stop=True), 0)
        remote_source = self.provider.base / 'source'
        self.assertEqual((remote_source / relative).read_text(), code.read_text())
        for path in blocked:
            self.assertFalse((remote_source / path).exists())
        code.unlink()
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertFalse((remote_source / relative).exists())

    def test_credential_source_can_seed_and_return_generated_types(self):
        relative = 'src/credentials/client.ts'
        self.config['sync'].update(generated=['src/credentials'], seedGenerated=True)
        self.config['servicePrepare'] = 'test -f src/credentials/client.ts && printf generated-type > src/credentials/client.ts'
        self.write_config()
        code = self.root / relative
        code.parent.mkdir(parents=True)
        code.write_text('tracked type')
        subprocess.run(['git', '-C', str(self.root), 'add', relative], check=True)
        self.assertEqual(self.app.up(no_watch=True), 0)
        self.assertEqual(code.read_text(), 'generated-type')
        self.assertEqual(self.app.check(['pass'], stop=True), 0)


class CredentialRunnerTests(unittest.TestCase):
    setUp = test_runner.RunnerTests.setUp
    tearDown = test_runner.RunnerTests.tearDown
    request = test_runner.RunnerTests.request
    invoke = test_runner.RunnerTests.invoke

    def test_runner_rejects_credential_data_in_source_and_seed_manifests(self):
        entry = {'sha256': hashlib.sha256(b'synthetic fixture').hexdigest(), 'mode': 0o644}
        for relative in SECRET_PATHS:
            with self.subTest(relative=relative):
                request = self.request({})
                request['manifest'] = {relative: entry}
                self.assertEqual(self.invoke(request).returncode, 70)
                request['manifest'] = {}
                request['config']['sync'].update(generated=['credentials'], seedGenerated=True)
                request['seedManifest'] = {relative: entry}
                self.assertEqual(self.invoke(request).returncode, 70)

    def test_runner_does_not_export_explicitly_excluded_generated_code(self):
        self.config['sync'].update(generated=['credentials'], exclude=['credentials/private'])
        self.config['prepare'] = 'mkdir -p credentials/private; printf fixture > credentials/private/client.ts'
        self.assertEqual(self.invoke(self.request()).returncode, 70)
        self.assertFalse((self.base / 'generated/credentials/private/client.ts').exists())


if __name__ == '__main__':
    unittest.main()
