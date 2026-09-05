"""Optional services and generated-file seeds preserve existing remote state."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from redev.config import ConfigError, validate_config
from redev.snapshot import Snapshot
import test_runner
import test_check_workflow


class OptionalServiceTests(unittest.TestCase):
    setUp = test_runner.RunnerTests.setUp
    tearDown = test_runner.RunnerTests.tearDown
    request = test_runner.RunnerTests.request
    invoke = test_runner.RunnerTests.invoke
    assert_ok = test_runner.RunnerTests.assert_ok
    source = test_runner.RunnerTests.source
    result = test_runner.RunnerTests.result
    status = test_runner.RunnerTests.status
    use_service = test_runner.RunnerTests.use_service

    def test_skipped_service_is_explicit_and_does_not_restart_live_services(self):
        self.config['sync']['mode'] = 'live'
        self.use_service()
        self.config['services'].append({'name': 'optional', 'when': 'exit 3', 'command': 'touch should-not-start; exit 1'})
        self.assert_ok(self.invoke(self.request(start=True)))
        first = self.status()['services']
        self.assertTrue(first[1]['skipped'])
        self.assertNotIn('error', first[1])
        self.assertNotIn('pid', first[1])
        self.assertFalse(self.source('should-not-start').exists())
        self.assert_ok(self.invoke(self.request({'app.txt': 'changed\n', 'lock.txt': 'one\n'})))
        second = self.status()['services']
        self.assertEqual(first[0]['pid'], second[0]['pid'])
        self.assertTrue(second[1]['skipped'])

    def test_condition_zero_starts_service_and_other_failure_stops_existing_services(self):
        self.use_service()
        self.config['services'][0]['when'] = 'exit 0'
        self.assert_ok(self.invoke(self.request(start=True)))
        self.assertTrue(self.status()['services'][0]['active'])
        self.config['services'].append({'name': 'broken', 'when': 'exit 7', 'command': 'touch should-not-start'})
        response = self.invoke(self.request(start=True))
        self.assertEqual(response.returncode, 70)
        self.assertIn('condition', self.result()['error'])
        self.assertFalse(self.status()['services'][0]['active'])
        self.assertFalse(self.source('should-not-start').exists())

    def seed_request(self, contents, relative='generated/types.ts'):
        self.config['sync']['seedGenerated'] = True
        request = self.request(start=True)
        seed_path = self.base / 'incoming' / relative
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(contents)
        request['seedManifest'] = {relative: {'sha256': hashlib.sha256(contents.encode()).hexdigest(), 'mode': 0o644}}
        return request

    def test_seeds_missing_generated_files_before_prepare_and_preserves_existing_values(self):
        self.config['prepare'] = 'test -f generated/types.ts'
        self.assert_ok(self.invoke(self.seed_request('tracked type')))
        self.assertEqual(self.source('generated/types.ts').read_text(), 'tracked type')
        self.source('generated/types.ts').write_text('remote generated type')
        self.assert_ok(self.invoke(self.seed_request('new local type')))
        self.assertEqual(self.source('generated/types.ts').read_text(), 'remote generated type')
        self.assertEqual((self.base / 'generated/generated/types.ts').read_text(), 'remote generated type')

    def test_seed_rejects_unconfigured_paths_secrets_and_symlinks(self):
        for relative in ['app.txt', 'generated/.env', 'generated/private.pem']:
            with self.subTest(relative=relative):
                response = self.invoke(self.seed_request('do not copy', relative))
                self.assertEqual(response.returncode, 70)
        outside = Path(self.temporary.name) / 'outside'
        outside.write_text('keep private')
        self.source('generated').mkdir(parents=True, exist_ok=True)
        self.source('generated/types.ts').symlink_to(outside)
        response = self.invoke(self.seed_request('new seed'))
        self.assertEqual(response.returncode, 70)
        self.assertEqual(outside.read_text(), 'keep private')

    def test_seed_staging_is_cleaned_without_removing_remote_content(self):
        request = self.seed_request('tracked type')
        request['incomingId'] = request['transactionId']
        unique = self.base / 'incoming' / request['incomingId']
        unique.mkdir()
        for name in ['app.txt', 'lock.txt', 'generated']:
            (self.base / 'incoming' / name).rename(unique / name)
        self.assert_ok(self.invoke(request))
        self.assertFalse(unique.exists())
        self.assertEqual(self.source('generated/types.ts').read_text(), 'tracked type')


class SeedSnapshotTests(unittest.TestCase):
    def test_seed_manifest_is_separate_tracked_only_and_excludes_env_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'generated').mkdir()
            (root / 'source.txt').write_text('source')
            for name in ['types.ts', 'untracked.ts', '.env']:
                (root / 'generated' / name).write_text(name)
            subprocess.run(['git', '-C', str(root), 'add', 'generated/types.ts', 'generated/.env'], check=True)
            config = {'sync': {'generated': ['generated'], 'seedGenerated': True}}
            with Snapshot(root, config) as snapshot:
                self.assertEqual(set(snapshot.manifest), {'source.txt'})
                self.assertEqual(set(snapshot.seed_manifest), {'generated/types.ts'})
                self.assertEqual((snapshot.directory / 'generated/types.ts').read_text(), 'types.ts')
            with Snapshot(root, config, include_generated=True) as snapshot:
                self.assertEqual(set(snapshot.manifest), {'source.txt', 'generated/types.ts'})
                self.assertEqual(snapshot.seed_manifest, {})

    def test_config_accepts_optional_service_and_rejects_invalid_settings(self):
        config = {'version': 1, 'checks': {'test': 'true'}, 'services': [{'name': 'optional', 'when': 'exit 3', 'command': 'run-server'}], 'sync': {'seedGenerated': True}}
        self.assertEqual(validate_config(config)['services'][0]['when'], 'exit 3')
        for value in ['', False, ['exit 3']]:
            config['services'][0]['when'] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config(config)
        config['services'][0]['when'] = 'exit 3'
        config['sync']['seedGenerated'] = 'true'
        with self.assertRaises(ConfigError):
            validate_config(config)


class SeedWorkflowTests(unittest.TestCase):
    setUp = test_check_workflow.CheckWorkflowTests.setUp
    write_config = test_check_workflow.CheckWorkflowTests.write_config

    def test_up_seeds_missing_types_without_making_generated_return_stale(self):
        self.config['sync']['seedGenerated'] = True
        self.config['servicePrepare'] = 'test -f generated/types.ts; printf remote-type > generated/types.ts'
        self.write_config()
        (self.root / 'generated').mkdir()
        (self.root / 'generated/types.ts').write_text('tracked type')
        subprocess.run(['git', '-C', str(self.root), 'add', 'generated/types.ts'], check=True)
        self.assertEqual(self.app.up(no_watch=True), 0)
        self.assertEqual((self.root / 'generated/types.ts').read_text(), 'remote-type')
        self.assertEqual(self.store.read()['syncStatus'], 'synced')
        self.assertEqual(self.app.check(['pass'], stop=True), 0)


if __name__ == '__main__':
    unittest.main()
