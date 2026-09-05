from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from redev.app import Application
from redev.config import read_config
from redev.snapshot import manifest_id, source_manifest
from redev.state import StateStore
from redev.worker import watch, worker_health


class ExistingCodespaceProvider:
    def account(self):
        return 'fixture-owner'

    def repository(self, root):
        return 'fixture/project'

    def list_codespaces(self, repository):
        return [{'name': 'shared-space', 'state': 'Available'}]

    def connect(self, name, identity):
        pass

    def stop_services(self):
        pass

    def stop(self, name):
        raise RuntimeError('fixture provider stop failed')


class SlowMappingStore(StateStore):
    def save(self, value):
        if value.get('codespace') and not self.read().get('codespace'):
            time.sleep(0.1)
        super().save(value)


class LocalSyncBoundary:
    def __init__(self, store):
        self.store = store
        self.remote_source = None

    def sync(self):
        config = read_config(self.store.root)
        manifest = source_manifest(self.store.root, config)
        self.remote_source = (self.store.root / 'source.txt').read_text()
        state = self.store.read()
        state.update(snapshotId=manifest_id(manifest), syncStatus='synced')
        self.store.save(state)
        return 0


class ReviewRegressionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def make_watcher_fixture(self):
        root = self.base / 'repo'
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / '.devcontainer').mkdir()
        config_path = root / '.devcontainer/devcontainer.json'
        config_text = json.dumps({'customizations': {'redev': {
            'version': 1, 'checks': {'types': 'true'}, 'ports': {},
        }}})
        config_path.write_text(config_text)
        (root / 'source.txt').write_text('last completed snapshot')
        store = StateStore(root, self.base / 'state')
        snapshot = source_manifest(root, read_config(root))
        store.save({'enabled': True, 'codespace': 'shared-space', 'ports': {},
                    'snapshotId': manifest_id(snapshot), 'syncStatus': 'synced'})
        return LocalSyncBoundary(store), config_path, config_text

    def test_two_worktrees_cannot_adopt_the_same_codespace_concurrently(self):
        stores = [SlowMappingStore(self.base / name, self.base / 'state')
                  for name in ('first-worktree', 'second-worktree')]
        ready = threading.Barrier(2)

        def adopt(store):
            with store.lock():
                ready.wait(timeout=5)
                try:
                    Application(store.root, store, ExistingCodespaceProvider()).resolve(
                        {}, create=True, codespace='shared-space')
                    return True
                except RuntimeError:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(adopt, stores))
        self.assertEqual(sum(results), 1, 'Only one worktree can own the Codespace')
        self.assertEqual(sum(store.read().get('codespace') == 'shared-space' for store in stores), 1)

    def test_watcher_syncs_an_edit_made_before_its_first_observation(self):
        application, _, _ = self.make_watcher_fixture()
        (application.store.root / 'source.txt').write_text('edit before watcher started')
        ticks = 0

        def next_tick(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks >= 3:
                raise KeyboardInterrupt

        with patch('redev.worker.time.sleep', side_effect=next_tick), patch('redev.worker.signal.signal'):
            result = watch(application, 'startup-fixture')
        self.assertEqual(result, 0)
        self.assertEqual(application.remote_source, 'edit before watcher started')

    def test_watcher_recovers_after_a_partially_written_configuration(self):
        application, config_path, config_text = self.make_watcher_fixture()
        ticks = 0

        def next_tick(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                config_path.write_text('{')
            elif ticks == 2:
                config_path.write_text(config_text)
                (application.store.root / 'source.txt').write_text('edit after config recovered')
            elif ticks >= 4:
                raise KeyboardInterrupt

        with patch('redev.worker.time.sleep', side_effect=next_tick), patch('redev.worker.signal.signal'):
            result = watch(application, 'recovery-fixture')
        self.assertEqual(result, 0, 'A temporary read error must not terminate the watcher')
        self.assertEqual(application.remote_source, 'edit after config recovered')
        self.assertIsNone(worker_health(application.store).get('error'))

    def test_failed_provider_stop_does_not_allow_disable(self):
        store = StateStore(self.base / 'repo', self.base / 'state')
        store.save({'enabled': True, 'codespace': 'shared-space',
                    'owner': 'fixture-owner', 'repository': 'fixture/project'})
        application = Application(store.root, store, ExistingCodespaceProvider())
        with self.assertRaisesRegex(RuntimeError, 'provider stop failed'):
            application.stop()
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            application.disable()
        self.assertTrue(store.read()['enabled'])

    def test_stop_cleans_a_worker_started_by_an_already_running_up(self):
        stop_observed_state = threading.Event()
        up_has_lock = threading.Event()
        stop_thread = threading.current_thread()

        class ObservedStore(StateStore):
            def read(self):
                state = super().read()
                if threading.current_thread() is stop_thread and up_has_lock.is_set():
                    stop_observed_state.set()
                return state

        class StoppableProvider(ExistingCodespaceProvider):
            def stop(self, name):
                pass

        store = ObservedStore(self.base / 'repo', self.base / 'state')
        store.save({'enabled': True, 'codespace': 'shared-space',
                    'owner': 'fixture-owner', 'repository': 'fixture/project'})
        application = Application(store.root, store, StoppableProvider())
        processes = []

        def finish_concurrent_up():
            with store.lock():
                up_has_lock.set()
                if not stop_observed_state.wait(timeout=5):
                    raise AssertionError('Stop did not read its initial state')
                token = 'concurrent-up-fixture'
                process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)',
                                            '_watch', '--token', token])
                processes.append(process)
                state = store.read()
                state['worker'] = {'pid': process.pid, 'token': token}
                store.save(state)

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending_up = pool.submit(finish_concurrent_up)
                self.assertTrue(up_has_lock.wait(timeout=5))
                self.assertEqual(application.stop(), 0)
                pending_up.result(timeout=5)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll(), 'Stop must also end the newly started worker')
            self.assertTrue(store.read()['enabled'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
