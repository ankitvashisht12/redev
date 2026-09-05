"""Worktree lifecycle and the local side of a remote source transaction."""
import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

from .config import read_config
from .snapshot import Snapshot, accept_generated, generated_manifest, source_manifest, tracked_generated_manifest
from .state import StateStore, file_lock, write_json
from .transport import GitHubTransport


class Application:
    def __init__(self, root, store=None, transport=None):
        self.root = root
        self.store = store or StateStore(root)
        self.transport = transport or GitHubTransport(self.store.directory)

    def resolve(self, config, create=False, codespace=None, replace=False, branch=None, machine=None, workspace='interactive'):
        state = self.store.read()
        owner = self.transport.account()
        repository = state.get('repository') or self.transport.repository(self.root)
        if state.get('owner') and state['owner'] != owner:
            raise RuntimeError(f'This mapping belongs to GitHub account {state["owner"]}. Restore that gh login.')
        environments = self.transport.list_codespaces(repository)
        previous_names = {item['name'] for item in environments}
        creation_error = None
        identity_marker = 'redev-' + self.store.identity
        selected = codespace or state.get('codespace')
        environment = next((item for item in environments if item['name'] == selected), None) if selected else None
        if codespace and not environment:
            raise RuntimeError('That Codespace is not in the current account and repository')
        if selected and not environment and not replace:
            raise RuntimeError('The mapped Codespace is missing. Inspect gh codespace list; use check or up with --replace to create a replacement.')
        if not environment and not selected:
            matches = [item for item in environments
                       if item.get('displayName', '') == identity_marker
                       or item.get('displayName', '').endswith('-' + identity_marker)
                       or item.get('displayName') == state.get('creatingDisplayName') and state.get('creatingDisplayName')]
            if len(matches) > 1:
                raise RuntimeError('Multiple matching Codespaces exist. Select one with check or up --codespace NAME.')
            environment = matches[0] if matches else None
        if not environment:
            if not create:
                raise RuntimeError('No Codespace is mapped to this worktree. Use gh redev check NAME or gh redev up.')
            if state.get('creating') and not replace:
                raise RuntimeError('A previous creation result is uncertain. Inspect gh codespace list, then use check or up with --codespace NAME or --replace.')
            label = self.transport.creation_label(self.root, branch or config.get('codespace', {}).get('branch'))
            label = re.sub(r'[^a-zA-Z0-9-]+', '-', label).strip('-') or 'worktree'
            display_name = label[:48 - len(identity_marker) - 1] + '-' + identity_marker
            state.update(enabled=True, owner=owner, repository=repository, creating=True, creatingDisplayName=display_name)
            self.store.save(state)
            try:
                selected = self.transport.create(repository, display_name, config, branch=branch, machine=machine)
            except BaseException as error:
                try:
                    created = [item for item in self.transport.list_codespaces(repository)
                               if item['name'] not in previous_names and item.get('displayName') == display_name]
                except BaseException:
                    raise error
                if len(created) != 1:
                    raise
                selected = created[0]['name']
                creation_error = error
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
        self.active_codespace = selected
        if creation_error is not None:
            raise creation_error
        if workspace == 'interactive':
            self.transport.connect(selected, self.store.identity)
        else:
            self.transport.connect(selected, self.store.identity, workspace=workspace)
        return self.store.read()

    def transaction(self, config, state, check=None, start_services=False, arguments=None, run_record=None, capture_freshness=None):
        validation = state.get('workspace') == 'validation'
        effective_config = copy.deepcopy(config)
        if validation:
            effective_config['sync']['generated'] = []
            effective_config['services'] = []
            effective_config['ports'] = {}
            effective_config['portSchemes'] = {}
            effective_config.pop('servicePrepare', None)
        else:
            state['portSchemes'] = config.get('portSchemes', {})
        state['syncStatus'] = 'syncing'
        self.store.save(state)
        try:
            baseline = {} if validation else generated_manifest(self.root, config)
            with Snapshot(self.root, config, include_generated=validation) as snapshot:
                if read_config(snapshot.directory) != config:
                    raise RuntimeError('Configuration changed while preparing the snapshot. Retry the command.')
                transaction_id = secrets.token_hex(16)
                if run_record is not None:
                    run_record.update(snapshotId=snapshot.identity, transactionId=transaction_id)
                self.transport.upload(snapshot.directory, transaction_id)
                request = {'config': effective_config, 'manifest': snapshot.manifest, 'snapshotId': snapshot.identity,
                           'transactionId': transaction_id, 'incomingId': transaction_id,
                           'ports': {} if validation else state.get('ports', {}),
                           'startServices': start_services, 'checkOnly': validation}
                if snapshot.seed_manifest:
                    request['seedManifest'] = snapshot.seed_manifest
                if check is not None:
                    request.update(checks=check, checkArgs=arguments or [])
                remote_exit = self.transport.run(request)
                result = self.transport.result()
                if result.get('transactionId') != transaction_id or result.get('snapshotId') != snapshot.identity:
                    raise RuntimeError('Remote result does not match this transaction. The connection may have failed.')
                if check is not None and not result.get('error'):
                    expected_checks = self.check_selections(config, check, arguments or [])
                    actual_checks = result.get('checks', [])
                    valid_checks = isinstance(actual_checks, list) and len(actual_checks) == len(expected_checks)
                    if valid_checks:
                        valid_checks = all(
                            isinstance(actual, dict)
                            and all(actual.get(field) == expected[field] for field in ('name', 'argv', 'cwd'))
                            and type(actual.get('exit')) is int and 0 <= actual['exit'] <= 255
                            for expected, actual in zip(expected_checks, actual_checks)
                        )
                    if not valid_checks or next((item['exit'] for item in actual_checks if item['exit']), 0) != result.get('checkExit'):
                        raise RuntimeError('Remote check results do not match the requested selection')
                if run_record is not None:
                    completed = {item['name']: item for item in result.get('checks', [])}
                    run_record['checks'] = [completed.get(item['name'], item) for item in run_record['checks']]
                    run_record['remoteExit'] = result.get('checkExit')
                if check is not None and result.get('checkExit') is None:
                    raise RuntimeError(result.get('error', 'Remote preparation failed before the check'))
                if not result.get('success') and (check is None or result.get('error')):
                    raise RuntimeError(result.get('error', f'Remote operation failed ({remote_exit})'))
                expected_exit = result['checkExit'] if check is not None else 0
                if remote_exit != expected_exit:
                    raise RuntimeError(f'Remote transport failed ({remote_exit}); command result was {expected_exit}.')
                generated_accepted = False
                def source_changed():
                    try:
                        return (source_manifest(self.root, config, include_generated=validation) != snapshot.manifest
                                or not generated_accepted and bool(snapshot.seed_manifest)
                                and tracked_generated_manifest(self.root, config) != snapshot.seed_manifest)
                    except (OSError, RuntimeError, subprocess.CalledProcessError):
                        return True
                if capture_freshness is not None:
                    capture_freshness(source_changed)
                stale = source_changed()
                if result.get('generated') and not stale and not validation:
                    exported = self.store.directory / 'generated' / transaction_id
                    self.transport.download_generated(exported)
                    stale = source_changed()
                    if not stale:
                        try:
                            accept_generated(self.root, exported, config, baseline, result['generated'])
                            generated_accepted = True
                        except RuntimeError as error:
                            raise RuntimeError(f'{error} Remote output: {exported}') from error
                        shutil.rmtree(exported)
                remote_status = self.transport.remote_status()
                stale = stale or source_changed()
                state.update(syncStatus='stale' if stale else 'synced', snapshotId=snapshot.identity,
                             lastSync=time.time(), lastError=None, remoteStatus=remote_status)
                if check is not None:
                    state['lastCheck'] = {'name': check[0] if len(check) == 1 else None, 'names': check,
                                          'remoteExit': expected_exit, 'stale': stale,
                                          'snapshotId': snapshot.identity, 'finishedAt': time.time()}
                    if run_record is not None:
                        run_record.update(stale=stale, remoteExit=expected_exit)
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
        from .worker import start_worker, stop_worker
        with self.store.lock():
            config = read_config(self.root)
            state = self.store.read()
            stop_worker(self.store, state)
            self.store.allocate_ports(config['ports'], state.get('ports'), overrides)
            state = self.resolve(config, create=True, codespace=codespace, replace=replace, branch=branch, machine=machine)
            state['workspace'] = 'interactive'
            state['portSchemes'] = config.get('portSchemes', {})
            result = self.transaction(config, state, start_services=True)
            if not no_watch and result == 0:
                start_worker(self.store, self.transport)
            return result

    def check(self, names, stop=False, arguments=None, codespace=None, replace=False, branch=None, machine=None):
        from .worker import stop_worker
        names = [names] if isinstance(names, str) else names
        arguments = arguments or []
        config = read_config(self.root)
        selections = self.check_selections(config, names, arguments)
        if stop:
            stop_worker(self.store, self.store.read())
        with self.store.lock():
            if read_config(self.root) != config:
                raise RuntimeError('Configuration changed while waiting for the worktree lock. Retry the command.')
            state = self.store.read()
            if stop:
                stop_worker(self.store, state)
            previous_workspace = state.get('workspace', 'interactive' if 'ports' in state else 'validation')
            workspace = 'validation' if stop else previous_workspace
            run_directory = self.store.directory / 'runs' / secrets.token_hex(16)
            run_directory.mkdir(parents=True, mode=0o700)
            log_path = run_directory / 'output.log'
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            record = {'names': names, 'checks': selections, 'workspace': workspace,
                      'startedAt': time.time(), 'exit': None, 'remoteExit': None, 'stale': None,
                      'resultPath': str(run_directory / 'result.json'), 'logPath': str(log_path),
                      'stop': {'requested': stop, 'confirmed': False}}
            self.active_codespace = None
            self.transport.log_path = log_path
            source_changed = None
            def capture_freshness(check_source):
                nonlocal source_changed
                source_changed = check_source
            code = 70
            try:
                state = self.resolve(config, create=True, codespace=codespace, replace=replace,
                                     branch=branch, machine=machine, workspace=workspace)
                state['workspace'] = workspace
                record['codespace'] = state['codespace']
                suffix = '/validation' if workspace == 'validation' else ''
                record['remoteDirectory'] = f'/workspaces/.redev/{self.store.identity}{suffix}/source'
                if workspace == 'validation':
                    self.transport.stop_services(workspace='interactive')
                    state['worker'] = None
                code = self.transaction(config, state, check=names, arguments=arguments, run_record=record,
                                        capture_freshness=capture_freshness)
            except BaseException as error:
                code = 130 if isinstance(error, KeyboardInterrupt) else 70
                record['error'] = str(error) or 'Check interrupted'
                with log_path.open('a') as log:
                    log.write(record['error'] + '\n')
                raise
            finally:
                if stop and self.active_codespace:
                    try:
                        self._stop_locked(stop_remote_services=False)
                        record['stop'].update(confirmed=True, finishedAt=time.time())
                    except BaseException as error:
                        record['stop']['error'] = str(error) or 'Stop interrupted'
                        print('Codespace stop failed: ' + record['stop']['error'], file=sys.stderr)
                        code = code or 70
                elif stop:
                    record['stop']['error'] = 'No Codespace was selected; inspect any uncertain creation before retrying.'
                if source_changed is not None and source_changed():
                    if not record.get('stale'):
                        print('Source changed before this result was saved. Run the check again after edits settle.', file=sys.stderr)
                    record['stale'] = True
                    code = code or 75
                record.update(exit=code, finishedAt=time.time())
                if self.active_codespace:
                    record['codespace'] = self.active_codespace
                write_json(run_directory / 'result.json', record)
                latest = self.store.read()
                latest['lastCheck'] = {**record, 'name': names[0] if len(names) == 1 else None}
                self.store.save(latest)
                self.transport.log_path = None
                print('Check results: ' + record['resultPath'])
            return code

    @staticmethod
    def check_selections(config, names, arguments):
        if (not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('Select one or more distinct check names')
        unknown = [name for name in names if name not in config['checks']]
        if unknown:
            raise RuntimeError(f'Unknown checks: {", ".join(unknown)}. Configured checks: {", ".join(config["checks"])}')
        if not isinstance(arguments, list) or any(not isinstance(argument, str) or '\x00' in argument for argument in arguments):
            raise ValueError('Check arguments must be strings')
        if arguments and (len(names) != 1 or not isinstance(config['checks'][names[0]], dict)):
            raise ValueError('Arguments after -- require exactly one structured argv check')
        selections = []
        for name in names:
            settings = config['checks'][name]
            argv = settings['argv'] + arguments if isinstance(settings, dict) else ['bash', '-lc', settings]
            cwd = settings.get('cwd', '.') if isinstance(settings, dict) else '.'
            selections.append({'name': name, 'argv': argv, 'cwd': cwd, 'exit': None})
        return selections

    def sync(self):
        with self.store.lock():
            state = self.store.read()
            if not state.get('enabled'):
                raise RuntimeError('Use gh redev check NAME or gh redev up to create a mapping first')
            config = read_config(self.root)
            state = self.resolve(config, workspace=state.get('workspace', 'interactive'))
            return self.transaction(config, state)

    def status(self):
        from .worker import worker_alive, worker_health
        state = self.store.read()
        if not state.get('enabled'):
            return {'enabled': False, 'root': str(self.root)}
        result = {**state, 'workerRunning': worker_alive(state.get('worker')), 'workerStatus': worker_health(self.store)}
        validation = state.get('workspace') == 'validation'
        result['urls'] = {} if validation else {
            name: f'{state.get("portSchemes", {}).get(name, "http")}://localhost:{port}'
            for name, port in state.get('ports', {}).items()}
        result['stateDirectory'] = str(self.store.directory)
        suffix = '/validation' if validation else ''
        result['remoteDirectory'] = f'/workspaces/.redev/{self.store.identity}{suffix}/source'
        try:
            environments = self.transport.list_codespaces(state['repository'])
            environment = next((item for item in environments if item['name'] == state.get('codespace')), None)
            result['codespaceState'] = environment['state'] if environment else 'Creation pending' if state.get('creating') else 'Missing'
            if environment:
                metadata = self.transport.codespace_metadata(state['codespace'])
                for field in ('displayName', 'billableOwner', 'machineName', 'machineDisplayName', 'idleTimeoutMinutes',
                              'retentionPeriodDays', 'retentionExpiresAt', 'createdAt', 'lastUsedAt', 'devcontainerPath'):
                    result[field] = metadata.get(field)
                result['codespaceState'] = metadata.get('state', result['codespaceState'])
        except (RuntimeError, KeyError) as error:
            result['statusError'] = str(error)
        services = state.get('remoteStatus', {}).get('services', [])
        stopped = result.get('codespaceState') in ('Shutdown', 'Missing')
        result['readiness'] = {
            'providerAvailable': result.get('codespaceState') == 'Available',
            'setup': 'completed in last source transaction' if state.get('lastSync') else 'not observed',
            'services': 'stopped' if stopped else 'last observed ready' if services and all(item.get('skipped') or item.get('active') for item in services) else 'not observed',
            'checkedLive': False,
        }
        return result

    def stop(self):
        from .worker import stop_worker
        # End the watcher before locking so it cannot queue another source transaction.
        stop_worker(self.store, self.store.read())
        with self.store.lock():
            return self._stop_locked()

    def _stop_locked(self, stop_remote_services=True):
        from .worker import stop_worker
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
        if stop_remote_services and environment and environment['state'] == 'Available':
            try:
                self.transport.connect(state['codespace'], self.store.identity)
                self.transport.stop_services()
            except (OSError, RuntimeError) as error:
                errors.append(str(error))
        confirmed_stopped = environment is None or environment['state'] == 'Shutdown'
        try:
            if not confirmed_stopped:
                self.transport.stop(state['codespace'])
                self.wait_for_shutdown(state['repository'], state['codespace'])
                confirmed_stopped = True
        except (OSError, RuntimeError) as error:
            errors.append(str(error))
        state.pop('stoppedAt', None)
        if confirmed_stopped:
            state['stoppedAt'] = time.time()
        state.update(worker=None, syncStatus='stopped' if confirmed_stopped else 'stop failed', lastError='; '.join(errors) or None)
        self.store.save(state)
        if errors:
            raise RuntimeError('Stop could not finish cleanly: ' + '; '.join(errors))
        return 0

    def wait_for_shutdown(self, repository, codespace, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            environments = self.transport.list_codespaces(repository)
            environment = next((item for item in environments if item['name'] == codespace), None)
            if environment is None or environment['state'] == 'Shutdown':
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('GitHub has not confirmed that the Codespace stopped. Check gh codespace list.')
            time.sleep(2)

    def disable(self):
        from .worker import worker_alive
        with self.store.lock():
            state = self.store.read()
            if worker_alive(state.get('worker')) or not state.get('stoppedAt'):
                raise RuntimeError('Run gh redev stop before disabling remote routing')
            state['enabled'] = False
            self.store.save(state)
        return 0
