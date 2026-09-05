import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from redev.app import Application
from redev.state import StateStore
from redev.transport import TransportError


class LocalProvider:
    """Only the provider boundary is replaced; run the real remote transaction."""
    def __init__(self, base):
        self.base = base
        self.interactive_base = base
        self.environments = []
        self.creations = 0
        self.fail_upload = False
        self.after_run = None

    def account(self):
        return 'fixture-user'

    def repository(self, root):
        return 'example/project'

    def list_codespaces(self, repository):
        return self.environments

    def creation_label(self, root, branch=None):
        return branch or 'fixture'

    def codespace_metadata(self, name):
        return next(item for item in self.environments if item['name'] == name)

    def create(self, repository, display_name, config, branch=None, machine=None):
        self.creations += 1
        self.environments.append({'name': 'fixture-environment', 'displayName': display_name, 'state': 'Available', 'repository': {'nameWithOwner': repository}})
        return 'fixture-environment'

    def connect(self, name, identity, workspace='interactive'):
        self.base = self.interactive_base / 'validation' if workspace == 'validation' else self.interactive_base
        self.base.mkdir(parents=True, exist_ok=True)
        self.environments[0]['state'] = 'Available'

    def upload(self, snapshot, transaction_id):
        if self.fail_upload:
            raise TransportError('fixture connection lost')
        shutil.copytree(snapshot, self.base / 'incoming' / transaction_id, dirs_exist_ok=True)

    def run(self, request):
        runner = Path(__file__).parents[1] / 'redev/runner.py'
        result = subprocess.run([sys.executable, str(runner), str(self.base), 'run'], input=json.dumps(request), text=True)
        if self.after_run:
            self.after_run()
        return result.returncode

    def result(self):
        return json.loads((self.base / 'result.json').read_text())

    def remote_status(self):
        return {'services': []}

    def download_generated(self, target):
        shutil.copytree(self.base / 'generated', target, dirs_exist_ok=True)

    def stop(self, name):
        self.environments[0]['state'] = 'Shutdown'

    def stop_services(self, workspace=None):
        runner = Path(__file__).parents[1] / 'redev/runner.py'
        subprocess.run([sys.executable, str(runner), str(self.base), 'stop'], check=True, capture_output=True)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.devcontainer').mkdir()
        config = {'version': 1, 'setup': 'echo installed >> install-count', 'setupInputs': ['manifest.txt'],
                  'checks': {'types': 'cat source.txt; exit 23', 'pass': 'cat source.txt'},
                  'services': [], 'ports': {}, 'sync': {'generated': []}}
        (self.root / '.devcontainer/devcontainer.json').write_text(json.dumps({'customizations': {'redev': config}}))
        (self.root / 'source.txt').write_text('uncommitted version')
        (self.root / 'manifest.txt').write_text('dependency lock v1')
        self.store = StateStore(self.root, self.base / 'state')
        self.provider = LocalProvider(self.base / 'remote')
        self.app = Application(self.root, self.store, self.provider)

    def test_up_reuses_mapping_and_check_preserves_exit_status(self):
        self.assertEqual(self.app.up(no_watch=True), 0)
        self.assertEqual(self.app.up(no_watch=True), 0)
        self.assertEqual(self.provider.creations, 1)
        self.assertEqual(self.app.check('types'), 23)
        self.assertEqual((self.provider.base / 'source/source.txt').read_text(), 'uncommitted version')
        self.assertEqual((self.provider.base / 'source/install-count').read_text().splitlines(), ['installed'])

    def test_failed_sync_does_not_run_check_or_local_command(self):
        self.app.up(no_watch=True)
        self.provider.fail_upload = True
        (self.root / 'source.txt').write_text('new edit')
        with self.assertRaisesRegex(TransportError, 'connection lost'):
            self.app.check('types')
        self.assertEqual((self.provider.base / 'source/source.txt').read_text(), 'uncommitted version')
        self.assertEqual(self.store.read()['lastCheck']['exit'], 70)
        self.assertIsNone(self.store.read()['lastCheck']['remoteExit'])

    def test_successful_check_becomes_stale_when_local_source_changes(self):
        self.app.up(no_watch=True)
        self.provider.after_run = lambda: (self.root / 'source.txt').write_text('new edit during check')
        self.assertEqual(self.app.check('pass'), 75)
        self.assertTrue(self.store.read()['lastCheck']['stale'])
        self.assertEqual(self.store.read()['lastCheck']['remoteExit'], 0)

    def test_failed_check_keeps_exit_code_when_stale(self):
        self.app.up(no_watch=True)
        self.provider.after_run = lambda: (self.root / 'source.txt').write_text('new edit during check')
        self.assertEqual(self.app.check('types'), 23)
        self.assertTrue(self.store.read()['lastCheck']['stale'])

    def test_check_can_create_a_validation_environment_without_up(self):
        self.assertEqual(self.app.check('types'), 23)
        self.assertEqual(self.provider.creations, 1)
        self.assertEqual(self.store.read()['workspace'], 'validation')

    def test_stop_preserves_data_mapping_and_opt_in(self):
        self.app.up(no_watch=True)
        database = self.provider.base / 'source/.convex/data'
        database.parent.mkdir()
        database.write_text('persistent development state')
        self.app.stop()
        self.assertEqual(database.read_text(), 'persistent development state')
        self.assertTrue(self.store.read()['enabled'])
        self.assertEqual(self.provider.environments[0]['state'], 'Shutdown')
        self.app.up(no_watch=True)
        self.assertEqual(self.provider.creations, 1)

    def test_edit_during_status_read_marks_result_stale(self):
        self.app.up(no_watch=True)
        def status_after_edit():
            (self.root / 'source.txt').write_text('edited while status was loading')
            return {'services': []}
        self.provider.remote_status = status_after_edit
        self.assertEqual(self.app.check('pass'), 75)
        self.assertTrue(self.store.read()['lastCheck']['stale'])

    def test_stop_allows_disable_after_up_fails_before_mapping(self):
        self.store.save({'enabled': True, 'ports': {}})
        self.app.stop()
        self.assertEqual(self.app.disable(), 0)
        self.assertFalse(self.store.read()['enabled'])
        self.assertEqual(self.provider.creations, 0)

    def test_disable_requires_stop_again_after_a_check_resumes(self):
        self.app.up(no_watch=True)
        self.app.stop()
        self.app.check('pass')
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            self.app.disable()

    def test_uncertain_creation_is_not_retried_as_duplicate(self):
        self.store.save({'enabled': True, 'repository': 'example/project', 'owner': 'fixture-user', 'creating': True})
        with self.assertRaisesRegex(RuntimeError, 'creation'):
            self.app.up(no_watch=True)
        self.assertEqual(self.provider.creations, 0)

if __name__ == '__main__':
    unittest.main()
