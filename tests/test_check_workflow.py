"""Run the application and real remote runner through a local provider boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from redev.app import Application
from redev.state import StateStore
from redev.transport import TransportError
from test_workflow import LocalProvider


class ValidationProvider(LocalProvider):
    def __init__(self, base):
        super().__init__(base)
        self.interactive_base = base
        self.requests = []
        self.stop_calls = 0
        self.fail_stop = False
        self.interrupt_run = False
        self.fail_connect = False

    def connect(self, name, identity, workspace='interactive'):
        self.base = self.interactive_base / 'validation' if workspace == 'validation' else self.interactive_base
        self.base.mkdir(parents=True, exist_ok=True)
        next(item for item in self.environments if item['name'] == name)['state'] = 'Available'
        if self.fail_connect:
            raise TransportError('connect failed after resume')

    def run(self, request):
        self.requests.append(request)
        if self.interrupt_run:
            raise KeyboardInterrupt
        runner = Path(__file__).parents[1] / 'redev/runner.py'
        result = subprocess.run([sys.executable, str(runner), str(self.base), 'run'],
                                input=json.dumps(request), text=True, capture_output=True)
        if getattr(self, 'log_path', None):
            with self.log_path.open('a') as log:
                log.write(result.stdout + result.stderr)
        if self.after_run:
            self.after_run()
        return result.returncode

    def stop_services(self, workspace=None):
        base = self.interactive_base if workspace == 'interactive' else self.base
        base.mkdir(parents=True, exist_ok=True)
        runner = Path(__file__).parents[1] / 'redev/runner.py'
        subprocess.run([sys.executable, str(runner), str(base), 'stop'], check=True, capture_output=True)

    def stop(self, name):
        self.stop_calls += 1
        if self.fail_stop:
            raise TransportError('provider stop failed')
        next(item for item in self.environments if item['name'] == name)['state'] = 'Shutdown'


class CheckWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.devcontainer').mkdir()
        self.config = {'version': 1, 'setup': 'printf installed >> install-count', 'setupInputs': ['manifest.txt'],
                       'prepare': 'printf prepared >> prepare-count', 'servicePrepare': 'touch live-credentials-required',
                       'checks': {'pass': {'argv': ['bash', '-c', 'cat source.txt']},
                                  'fail': {'argv': ['bash', '-c', 'echo failure >&2; exit 23']},
                                  'legacy': 'true'}, 'sync': {'generated': ['generated']}}
        self.write_config()
        (self.root / 'source.txt').write_text('uncommitted version')
        (self.root / 'manifest.txt').write_text('lock v1')
        self.store = StateStore(self.root, self.base / 'state')
        self.provider = ValidationProvider(self.base / 'remote')
        self.app = Application(self.root, self.store, self.provider)

    def write_config(self):
        (self.root / '.devcontainer/devcontainer.json').write_text(json.dumps({'customizations': {'redev': self.config}}))

    def latest_result(self):
        return json.loads(Path(self.store.read()['lastCheck']['resultPath']).read_text())

    def test_first_check_creates_without_up_then_stops_and_reuses(self):
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertEqual(self.provider.creations, 1)
        self.assertEqual(self.provider.environments[0]['state'], 'Shutdown')
        self.assertFalse((self.provider.base / 'source/live-credentials-required').exists())
        self.assertNotIn('ports', self.store.read())
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertEqual(self.provider.creations, 1)
        self.assertEqual((self.provider.base / 'source/install-count').read_text(), 'installed')

    def test_batch_uses_one_snapshot_and_saves_exact_results_and_private_logs(self):
        self.assertEqual(self.app.check(['fail', 'pass'], stop=True), 23)
        self.assertEqual(len(self.provider.requests), 1)
        result = self.latest_result()
        self.assertEqual([item['name'] for item in result['checks']], ['fail', 'pass'])
        self.assertEqual([item['exit'] for item in result['checks']], [23, 0])
        self.assertTrue(result['stop']['confirmed'])
        self.assertEqual(result['exit'], 23)
        self.assertEqual(result['workspace'], 'validation')
        log = Path(result['logPath'])
        self.assertIn('failure', log.read_text())
        self.assertIn('uncommitted version', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)

    def test_checks_with_arguments_use_one_structured_command(self):
        self.config['checks']['args'] = {'argv': [sys.executable, '-c', 'import sys; print(repr(sys.argv[1:]))']}
        self.write_config()
        self.assertEqual(self.app.check(['args'], stop=True, arguments=['two words', '$(touch nope)']), 0)
        self.assertEqual(self.latest_result()['checks'][0]['argv'][-2:], ['two words', '$(touch nope)'])

    def test_invalid_selection_never_provisions_or_stops(self):
        selections = [([], []), (['missing'], []), (['pass', 'pass'], []),
                      (['pass', 'fail'], ['--filter']), (['legacy'], ['--filter'])]
        for names, arguments in selections:
            with self.subTest(names=names), self.assertRaises((ValueError, RuntimeError)):
                self.app.check(names, stop=True, arguments=arguments)
        self.assertEqual(self.provider.creations, 0)
        self.assertEqual(self.provider.stop_calls, 0)

    def test_validation_does_not_overwrite_interactive_generated_files_or_read_its_env(self):
        self.app.up(no_watch=True)
        live_source = self.provider.interactive_base / 'source'
        generated = live_source / 'generated/types.ts'
        generated.parent.mkdir(parents=True)
        generated.write_text('live generated types')
        (live_source / '.env').write_text('private credentials')
        local_generated = self.root / 'generated/types.ts'
        local_generated.parent.mkdir()
        local_generated.write_text('tracked generated types')
        subprocess.run(['git', '-C', str(self.root), 'add', 'generated/types.ts'], check=True)
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertEqual(generated.read_text(), 'live generated types')
        self.assertEqual((self.provider.base / 'source/generated/types.ts').read_text(), 'tracked generated types')
        self.assertFalse((self.provider.base / 'source/.env').exists())
        self.assertFalse(json.loads((self.provider.interactive_base / 'state.json').read_text())['desiredServices'])

    def test_interactive_plain_check_restores_services_and_up_reuses_its_source(self):
        self.app.up(no_watch=True)
        self.assertEqual(self.app.check(['pass']), 0)
        self.assertEqual(self.latest_result()['workspace'], 'interactive')
        self.assertTrue(json.loads((self.provider.base / 'state.json').read_text())['desiredServices'])
        self.assertEqual(self.provider.stop_calls, 0)
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.app.up(no_watch=True)
        self.assertEqual(self.provider.base, self.provider.interactive_base)
        self.assertTrue((self.provider.base / 'source/live-credentials-required').exists())

    def test_upload_failure_is_archived_and_stops_compute(self):
        self.provider.fail_upload = True
        with self.assertRaisesRegex(TransportError, 'connection lost'):
            self.app.check(['pass'], stop=True)
        result = self.latest_result()
        self.assertEqual(result['exit'], 70)
        self.assertIn('connection lost', result['error'])
        self.assertTrue(result['stop']['confirmed'])
        self.assertEqual(result['checks'][0]['exit'], None)

    def test_setup_failure_has_logs_and_stops_compute(self):
        self.config['setup'] = 'echo setup-broken >&2; exit 4'
        self.write_config()
        with self.assertRaisesRegex(RuntimeError, 'setup failed'):
            self.app.check(['pass'], stop=True)
        result = self.latest_result()
        self.assertIn('setup-broken', Path(result['logPath']).read_text())
        self.assertTrue(result['stop']['confirmed'])

    def test_resume_failure_still_stops_known_mapping(self):
        self.provider.fail_connect = True
        with self.assertRaisesRegex(TransportError, 'connect failed'):
            self.app.check(['pass'], stop=True)
        self.assertTrue(self.latest_result()['stop']['confirmed'])

    def test_cancel_stops_compute_and_archives_interrupted_result(self):
        self.provider.interrupt_run = True
        with self.assertRaises(KeyboardInterrupt):
            self.app.check(['pass'], stop=True)
        result = self.latest_result()
        self.assertEqual(result['exit'], 130)
        self.assertTrue(result['stop']['confirmed'])

    def test_stop_failure_is_separate_and_keeps_failed_check_exit(self):
        self.provider.fail_stop = True
        self.assertEqual(self.app.check(['fail'], stop=True), 23)
        result = self.latest_result()
        self.assertEqual(result['exit'], 23)
        self.assertFalse(result['stop']['confirmed'])
        self.assertIn('provider stop failed', result['stop']['error'])
        self.assertNotIn('stoppedAt', self.store.read())

    def test_stop_failure_changes_success_to_nonzero(self):
        self.provider.fail_stop = True
        self.assertEqual(self.app.check(['pass'], stop=True), 70)
        self.assertEqual(self.latest_result()['remoteExit'], 0)
        self.assertFalse(self.latest_result()['stop']['confirmed'])

    def test_stale_success_is_nonzero_and_stopped(self):
        self.provider.after_run = lambda: (self.root / 'source.txt').write_text('new edit')
        self.assertEqual(self.app.check(['pass'], stop=True), 75)
        result = self.latest_result()
        self.assertTrue(result['stale'])
        self.assertTrue(result['stop']['confirmed'])

    def test_edit_during_shutdown_makes_success_stale(self):
        stop = self.provider.stop
        def stop_then_edit(name):
            stop(name)
            (self.root / 'source.txt').write_text('edit while stopping')
        self.provider.stop = stop_then_edit
        self.assertEqual(self.app.check(['pass'], stop=True), 75)
        result = self.latest_result()
        self.assertEqual(result['remoteExit'], 0)
        self.assertEqual(result['checks'][0]['exit'], 0)
        self.assertTrue(result['stale'])
        self.assertTrue(result['stop']['confirmed'])
        self.assertTrue(self.store.read()['lastCheck']['stale'])

    def test_edit_during_shutdown_preserves_a_failed_check_exit(self):
        stop = self.provider.stop
        def stop_then_edit(name):
            stop(name)
            (self.root / 'source.txt').write_text('edit while stopping')
        self.provider.stop = stop_then_edit
        self.assertEqual(self.app.check(['fail'], stop=True), 23)
        result = self.latest_result()
        self.assertTrue(result['stale'])
        self.assertEqual(result['remoteExit'], 23)
        self.assertTrue(result['stop']['confirmed'])

    def test_generated_validation_edit_during_shutdown_is_stale(self):
        self.config['sync']['seedGenerated'] = True
        self.write_config()
        generated = self.root / 'generated/types.ts'
        generated.parent.mkdir()
        generated.write_text('tracked type')
        subprocess.run(['git', '-C', str(self.root), 'add', 'generated/types.ts'], check=True)
        stop = self.provider.stop
        def stop_then_edit(name):
            stop(name)
            generated.write_text('edited tracked type')
        self.provider.stop = stop_then_edit
        self.assertEqual(self.app.check(['pass'], stop=True), 75)
        self.assertTrue(self.latest_result()['stale'])
        self.assertTrue(self.latest_result()['stop']['confirmed'])

    def test_status_points_to_validation_source_and_hides_app_ports(self):
        self.app.up(no_watch=True)
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        status = self.app.status()
        self.assertEqual(status['remoteDirectory'], f'/workspaces/.redev/{self.store.identity}/validation/source')
        self.assertEqual(status['urls'], {})

    def test_stop_waits_for_confirmed_shutdown(self):
        self.app.check(['pass'])
        original_list = self.provider.list_codespaces
        polls = []
        def stop_later(name):
            self.provider.environments[0]['state'] = 'ShuttingDown'
        def observe(repository):
            if self.provider.environments[0]['state'] == 'ShuttingDown':
                polls.append(1)
                if len(polls) >= 2:
                    self.provider.environments[0]['state'] = 'Shutdown'
            return original_list(repository)
        self.provider.stop = stop_later
        self.provider.list_codespaces = observe
        with patch('redev.app.time.sleep'):
            self.assertEqual(self.app.stop(), 0)
        self.assertEqual(len(polls), 2)
        self.assertIn('stoppedAt', self.store.read())

    def test_creation_error_recovers_only_the_new_matching_codespace_and_stops_it(self):
        create = self.provider.create
        def failed_creation(*args, **kwargs):
            create(*args, **kwargs)
            raise TransportError('post-create setup failed')
        self.provider.create = failed_creation
        with self.assertRaisesRegex(TransportError, 'post-create setup failed'):
            self.app.check(['pass'], stop=True)
        self.assertEqual(self.provider.environments[0]['state'], 'Shutdown')
        self.assertTrue(self.latest_result()['stop']['confirmed'])
        self.assertFalse(self.store.read()['creating'])

    def test_incomplete_remote_scope_is_not_reported_as_passed(self):
        def remove_check_records():
            result_path = self.provider.base / 'result.json'
            value = json.loads(result_path.read_text())
            value['checks'] = []
            result_path.write_text(json.dumps(value))
        self.provider.after_run = remove_check_records
        with self.assertRaisesRegex(RuntimeError, 'selection'):
            self.app.check(['pass'], stop=True)
        self.assertTrue(self.latest_result()['stop']['confirmed'])


if __name__ == '__main__':
    unittest.main()
