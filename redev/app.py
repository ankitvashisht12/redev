"""Worktree lifecycle and the local side of a remote source transaction."""
import json
import secrets
import shutil
import sys
import time

from .config import read_config
from .snapshot import Snapshot, accept_generated, generated_manifest, source_manifest
from .state import StateStore, file_lock
from .transport import GitHubTransport


class Application:
    def __init__(self, root, store=None, transport=None):
        self.root = root
        self.store = store or StateStore(root)
        self.transport = transport or GitHubTransport(self.store.directory)

    def resolve(self, config, create=False, codespace=None, replace=False, branch=None, machine=None):
        state = self.store.read()
        owner = self.transport.account()
        repository = state.get('repository') or self.transport.repository(self.root)
        if state.get('owner') and state['owner'] != owner:
            raise RuntimeError(f'This mapping belongs to GitHub account {state["owner"]}. Restore that gh login.')
        environments = self.transport.list_codespaces(repository)
        display_name = 'redev-' + self.store.identity
        selected = codespace or state.get('codespace')
        environment = next((item for item in environments if item['name'] == selected), None) if selected else None
        if codespace and not environment:
            raise RuntimeError('That Codespace is not in the current account and repository')
        if selected and not environment and not replace:
            raise RuntimeError('The mapped Codespace is missing. Inspect gh codespace list; use up --replace only to create replacement state.')
        if not environment and not selected:
            matches = [item for item in environments if item.get('displayName') == display_name]
            if len(matches) > 1:
                raise RuntimeError('Multiple matching Codespaces exist. Select one with up --codespace NAME.')
            environment = matches[0] if matches else None
        if not environment:
            if not create:
                raise RuntimeError('No Codespace is mapped to this worktree. Run gh redev up first.')
            if state.get('creating') and not replace:
                raise RuntimeError('A previous creation result is uncertain. Inspect gh codespace list, then use up --codespace NAME or up --replace.')
            state.update(enabled=True, owner=owner, repository=repository, creating=True)
            self.store.save(state)
            selected = self.transport.create(repository, display_name, config, branch=branch, machine=machine)
        else:
            selected = environment['name']
        with file_lock(self.store.home / 'mappings.lock'):
            for path in (self.store.home / 'worktrees').glob('*/state.json'):
                if path != self.store.path:
                    other = json.loads(path.read_text())
                    if other.get('codespace') == selected:
                        raise RuntimeError('This Codespace is already mapped to a different local worktree')
            state.pop('stoppedAt', None)
            state.update(enabled=True, owner=owner, repository=repository, codespace=selected, creating=False)
            self.store.save(state)
        self.transport.connect(selected, self.store.identity)
        return self.store.read()

    def transaction(self, config, state, check=None, start_services=False):
        state['syncStatus'] = 'syncing'
        self.store.save(state)
        try:
            baseline = generated_manifest(self.root, config)
            with Snapshot(self.root, config) as snapshot:
                if read_config(snapshot.directory) != config:
                    raise RuntimeError('Configuration changed while preparing the snapshot. Retry the command.')
                transaction_id = secrets.token_hex(16)
                self.transport.upload(snapshot.directory, transaction_id)
                request = {'config': config, 'manifest': snapshot.manifest, 'snapshotId': snapshot.identity,
                           'transactionId': transaction_id, 'incomingId': transaction_id, 'ports': state.get('ports', {}),
                           'check': check, 'startServices': start_services}
                remote_exit = self.transport.run(request)
                result = self.transport.result()
                if result.get('transactionId') != transaction_id or result.get('snapshotId') != snapshot.identity:
                    raise RuntimeError('Remote result does not match this transaction. The connection may have failed.')
                if check is not None and result.get('checkExit') is None:
                    raise RuntimeError(result.get('error', 'Remote preparation failed before the check'))
                if not result.get('success') and (check is None or result.get('error')):
                    raise RuntimeError(result.get('error', f'Remote operation failed ({remote_exit})'))
                expected_exit = result['checkExit'] if check is not None else 0
                if remote_exit != expected_exit:
                    raise RuntimeError(f'Remote transport failed ({remote_exit}); command result was {expected_exit}.')
                def source_changed():
                    try:
                        return source_manifest(self.root, config) != snapshot.manifest
                    except (OSError, RuntimeError):
                        return True
                stale = source_changed()
                if result.get('generated') and not stale:
                    exported = self.store.directory / 'generated' / transaction_id
                    self.transport.download_generated(exported)
                    stale = source_changed()
                    if not stale:
                        try:
                            accept_generated(self.root, exported, config, baseline, result['generated'])
                        except RuntimeError as error:
                            raise RuntimeError(f'{error} Remote output: {exported}') from error
                        shutil.rmtree(exported)
                remote_status = self.transport.remote_status()
                stale = stale or source_changed()
                state.update(syncStatus='stale' if stale else 'synced', snapshotId=snapshot.identity,
                             lastSync=time.time(), lastError=None, remoteStatus=remote_status)
                if check is not None:
                    state['lastCheck'] = {'name': check, 'remoteExit': expected_exit, 'stale': stale,
                                          'snapshotId': snapshot.identity, 'finishedAt': time.time()}
                self.store.save(state)
                if stale:
                    print('Source changed during this operation. The remote result is out of date; run the check again after edits settle.', file=sys.stderr)
                    return expected_exit or 75
                return expected_exit
        except BaseException as error:
            state.update(syncStatus='failed', lastError=str(error))
            self.store.save(state)
            raise

    def up(self, no_watch=False, overrides=None, codespace=None, replace=False, branch=None, machine=None):
        from .worker import start_worker, stop_worker, worker_alive
        with self.store.lock():
            config = read_config(self.root)
            state = self.store.read()
            stop_worker(self.store, state)
            self.store.allocate_ports(config['ports'], state.get('ports'), overrides)
            state = self.resolve(config, create=True, codespace=codespace, replace=replace, branch=branch, machine=machine)
            result = self.transaction(config, state, start_services=True)
            if not no_watch and result == 0:
                start_worker(self.store, self.transport)
            return result

    def check(self, name):
        with self.store.lock():
            state = self.store.read()
            if not state.get('enabled'):
                raise RuntimeError('This worktree is not enabled. Run gh redev up first.')
            config = read_config(self.root)
            if name not in config['checks']:
                raise RuntimeError(f'Unknown check {name!r}. Configured checks: {", ".join(config["checks"])}')
            state = self.resolve(config)
            return self.transaction(config, state, check=name)

    def sync(self):
        with self.store.lock():
            state = self.store.read()
            if not state.get('enabled'):
                raise RuntimeError('Run gh redev up first')
            config = read_config(self.root)
            state = self.resolve(config)
            return self.transaction(config, state)

    def status(self):
        from .worker import worker_alive, worker_health
        state = self.store.read()
        if not state.get('enabled'):
            return {'enabled': False, 'root': str(self.root)}
        result = {**state, 'workerRunning': worker_alive(state.get('worker')), 'workerStatus': worker_health(self.store)}
        result['urls'] = {name: f'http://localhost:{port}' for name, port in state.get('ports', {}).items()}
        result['stateDirectory'] = str(self.store.directory)
        result['remoteDirectory'] = f'/workspaces/.redev/{self.store.identity}/source'
        try:
            environments = self.transport.list_codespaces(state['repository'])
            environment = next((item for item in environments if item['name'] == state.get('codespace')), None)
            result['codespaceState'] = environment['state'] if environment else 'Missing'
        except (RuntimeError, KeyError) as error:
            result['statusError'] = str(error)
        return result

    def stop(self):
        from .worker import stop_worker
        # Stop the watcher first so it cannot take the transaction lock again.
        stop_worker(self.store, self.store.read())
        with self.store.lock():
            state = self.store.read()
            stop_worker(self.store, state)
            if not state.get('codespace'):
                if state.get('creating'):
                    raise RuntimeError('Creation is uncertain. Inspect gh codespace list and recover the mapping before stopping.')
                state.update(stoppedAt=time.time(), worker=None, syncStatus='stopped')
                self.store.save(state)
                return 0
            if self.transport.account() != state.get('owner'):
                raise RuntimeError('GitHub account changed; use the account that owns this mapping to stop it.')
            environments = self.transport.list_codespaces(state['repository'])
            environment = next((item for item in environments if item['name'] == state['codespace']), None)
            errors = []
            if environment and environment['state'] == 'Available':
                try:
                    self.transport.connect(state['codespace'], self.store.identity)
                    self.transport.stop_services()
                except RuntimeError as error:
                    errors.append(str(error))
            confirmed_stopped = environment is None
            try:
                if environment:
                    self.transport.stop(state['codespace'])
                confirmed_stopped = True
            except RuntimeError as error:
                errors.append(str(error))
            state.pop('stoppedAt', None)
            if confirmed_stopped:
                state['stoppedAt'] = time.time()
            state.update(worker=None, syncStatus='stopped' if confirmed_stopped else 'stop failed', lastError='; '.join(errors) or None)
            self.store.save(state)
            if errors:
                raise RuntimeError('Stop could not finish cleanly: ' + '; '.join(errors))
            return 0

    def disable(self):
        from .worker import worker_alive
        with self.store.lock():
            state = self.store.read()
            if worker_alive(state.get('worker')) or not state.get('stoppedAt'):
                raise RuntimeError('Run gh redev stop before disabling remote routing')
            state['enabled'] = False
            self.store.save(state)
        return 0
