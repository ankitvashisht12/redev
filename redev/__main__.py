import argparse
import json
import os
from pathlib import Path
import signal
import sys

from . import __version__
from .app import Application
from .state import worktree_root
from .worker import watch


def parser():
    result = argparse.ArgumentParser(prog='gh redev', description='Keep source local; run configured checks and services in a Codespace per worktree.')
    result.add_argument('--version', action='version', version=__version__)
    result.add_argument('--root', type=Path, default=Path.cwd(), help='Worktree directory (default: current directory)')
    commands = result.add_subparsers(dest='command', required=True)
    up = commands.add_parser('up', help='Opt in, create or reuse, sync, start services and local forwarding')
    up.add_argument('--port', action='append', default=[], metavar='NAME=NUMBER', help='Set a named port on both the Mac and Codespace')
    up.add_argument('--no-watch', action='store_true', help='Start remote services without local automatic sync or forwarding')
    up.add_argument('--codespace', help='Adopt an existing Codespace in this account and repository')
    up.add_argument('--replace', action='store_true', help='Create a replacement only when the old mapping is missing or creation is uncertain')
    up.add_argument('--branch', help='Published branch containing the devcontainer recipe for initial creation')
    up.add_argument('--machine', help='Codespaces machine name for initial creation')
    check = commands.add_parser('check', help='Create or resume, sync once, and run selected checks; append runner arguments after --')
    check.add_argument('names', nargs='+', help='Names from customizations.redev.checks')
    check.add_argument('--stop', action='store_true', help='Use the validation source and stop the Codespace after success, failure, or interruption')
    check.add_argument('--codespace', help='Adopt an existing Codespace in this account and repository')
    check.add_argument('--replace', action='store_true', help='Create a replacement when the old mapping is missing or creation is uncertain')
    check.add_argument('--branch', help='Published branch containing the devcontainer recipe for initial creation')
    check.add_argument('--machine', help='Codespaces machine name for initial creation')
    commands.add_parser('sync', help='Flush a coherent source snapshot to the mapped environment')
    status = commands.add_parser('status', help='Show mapping, sync state, services, and private URLs; do not resume')
    status.add_argument('--json', action='store_true', help='Print complete machine-readable status')
    commands.add_parser('stop', help='Stop local sync/forwarding and the Codespace; preserve data and opt-in')
    commands.add_parser('disable', help='After stop, opt this worktree out of command routing')
    commands.add_parser('enabled', help='Exit 0 if this worktree is enabled, 3 otherwise')
    internal = commands.add_parser('_watch', help='Internal watcher; started by up')
    internal.add_argument('--token', required=True)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    runner_arguments = []
    if '--' in argv:
        separator = argv.index('--')
        runner_arguments = argv[separator + 1:]
        argv = argv[:separator]
    argument_parser = parser()
    arguments = argument_parser.parse_args(argv)
    if runner_arguments and arguments.command != 'check':
        argument_parser.error('Arguments after -- are supported only for check')
    previous_signal = None
    if arguments.command == 'check':
        previous_signal = signal.signal(signal.SIGTERM, interrupted)
    try:
        root = worktree_root(arguments.root)
        os.chdir(root)
        app = Application(root)
        if arguments.command == 'enabled':
            return 0 if app.store.read().get('enabled') else 3
        if arguments.command == 'up':
            ports = {}
            from .config import read_config
            config = read_config(root)
            for value in arguments.port:
                try:
                    name, raw_port = value.split('=', 1)
                    port = int(raw_port)
                    if name not in config['ports'] or not 1024 <= port <= 65535 or name in ports:
                        raise ValueError
                    ports[name] = port
                except ValueError:
                    raise RuntimeError('--port must be NAME=NUMBER for a configured name, with port 1024–65535')
            code = app.up(arguments.no_watch, ports, arguments.codespace, arguments.replace, arguments.branch, arguments.machine)
            print_status(app.status())
            return code
        if arguments.command == 'check':
            return app.check(arguments.names, stop=arguments.stop, arguments=runner_arguments,
                             codespace=arguments.codespace, replace=arguments.replace,
                             branch=arguments.branch, machine=arguments.machine)
        if arguments.command == 'sync':
            return app.sync()
        if arguments.command == 'status':
            status = app.status()
            print(json.dumps(status, indent=2) if arguments.json else format_status(status))
            return 0
        if arguments.command == 'stop':
            code = app.stop()
            print('Codespace stopped. Development data and worktree opt-in are kept.')
            return code
        if arguments.command == 'disable':
            return app.disable()
        if arguments.command == '_watch':
            return watch(app, arguments.token)
    except KeyboardInterrupt:
        print('Operation interrupted. Inspect status before retrying.', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f'redev: {error}', file=sys.stderr)
        return 70
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def format_status(status):
    if not status.get('enabled'):
        return 'This worktree has no enabled mapping. Use gh redev check NAME or gh redev up.'
    lines = [f'Worktree: {status["root"]}', f'Repository: {status.get("repository", "not selected")}',
             f'Codespace: {status.get("displayName") or status.get("codespace", "creation pending")} ({status.get("codespaceState", "unknown")})']
    if status.get('machineDisplayName') or status.get('machineName'):
        lines.append('Machine: ' + (status.get('machineDisplayName') or status['machineName']))
    owner = status.get('billableOwner')
    if owner:
        lines.append('Paid by: ' + (owner.get('login', 'unknown') if isinstance(owner, dict) else str(owner)))
    if status.get('idleTimeoutMinutes') is not None:
        lines.append(f'Idle timeout: {status["idleTimeoutMinutes"]} minutes')
    if status.get('retentionPeriodDays') is not None:
        lines.append(f'Retention: {status["retentionPeriodDays"]} days; expiry: {status.get("retentionExpiresAt") or "not scheduled"}')
    readiness = status.get('readiness', {})
    lines.append(f'Setup: {readiness.get("setup", "not observed")}; services: {readiness.get("services", "not observed")} (passive status)')
    lines.append(f'Sync: {status.get("syncStatus", "not started")}; watcher: {"running" if status.get("workerRunning") else "stopped"}')
    for name, url in status.get('urls', {}).items():
        lines.append(f'{name}: {url}')
    for service in status.get('remoteStatus', {}).get('services', []):
        service_state = 'skipped' if service.get('skipped') else 'running' if service.get('active') else 'stopped'
        lines.append(f'Service {service["name"]}: {service_state} (last observed status)')
    for error in (status.get('lastError'), status.get('statusError'), status.get('workerStatus', {}).get('error')):
        if error:
            lines.append('Error: ' + error)
    lines.append(f'Private state and logs: {status.get("stateDirectory")}')
    return '\n'.join(lines)


def print_status(status):
    print(format_status(status))


if __name__ == '__main__':
    sys.exit(main())
