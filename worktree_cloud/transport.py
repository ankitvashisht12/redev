"""Provider operations through gh, with OpenSSH for exact remote exit status."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


class TransportError(RuntimeError):
    pass


class GitHubTransport:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.ssh_config = self.directory / 'ssh.config'

    @staticmethod
    def capture(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True)
        except OSError as error:
            raise TransportError(f'Cannot run {args[0]}: {error}') from error
        if result.returncode:
            raise TransportError(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()}')
        return result.stdout.strip()

    def account(self):
        return self.capture(['gh', 'api', 'user', '--jq', '.login'])

    def repository(self, root):
        return self.capture(['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'])

    def list_codespaces(self, repository):
        return json.loads(self.capture(['gh', 'codespace', 'list', '--repo', repository, '--limit', '1000',
                                       '--json', 'name,displayName,state,repository,owner']))

    def create(self, repository, display_name, config, branch=None, machine=None):
        options = config.get('codespace', {})
        machine = machine or options.get('machine')
        if not machine:
            data = json.loads(self.capture(['gh', 'api', f'repos/{repository}/codespaces/machines']))
            candidates = [item for item in data['machines']
                          if item['cpus'] >= options.get('minimumCpus', 2)
                          and item['memory_in_bytes'] >= options.get('minimumMemoryGb', 8) * 1024 ** 3]
            if not candidates:
                raise TransportError('No allowed Codespaces machine meets the configuration. Select an available --machine.')
            machine = min(candidates, key=lambda item: (item['cpus'], item['memory_in_bytes']))['name']
        args = ['gh', 'codespace', 'create', '--repo', repository, '--display-name', display_name,
                '--machine', machine, '--devcontainer-path', '.devcontainer/devcontainer.json',
                '--idle-timeout', options.get('idleTimeout', '30m'), '--default-permissions', '--status']
        if branch or options.get('branch'):
            args += ['--branch', branch or options['branch']]
        # gh writes the created name to stdout. Progress is kept on stderr.
        result = subprocess.run(args, stdout=subprocess.PIPE, text=True)
        if result.returncode:
            raise TransportError('Codespace creation did not finish. The private creation record is kept; inspect gh codespace list before retrying.')
        name = result.stdout.strip()
        if not re.fullmatch(r'[a-zA-Z0-9-]+', name):
            raise TransportError('Cannot identify created Codespace. Use up --codespace NAME after inspecting gh codespace list.')
        return name

    def connect(self, name, identity):
        if not re.fullmatch(r'[a-zA-Z0-9-]+', name) or not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise TransportError('Invalid Codespace mapping')
        self.name = name
        self.base = f'/workspaces/.worktree-cloud/{identity}'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        configuration = self.capture(['gh', 'codespace', 'ssh', '--codespace', name, '--config'])
        hosts = re.findall(r'^Host\s+(\S+)\s*$', configuration, re.MULTILINE)
        if len(hosts) != 1 or not re.fullmatch(r'[a-zA-Z0-9._-]+', hosts[0]):
            raise TransportError('gh did not return one usable OpenSSH host for this Codespace')
        self.host = hosts[0]
        descriptor = os.open(self.ssh_config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(configuration + '\n')
        self.capture(self.ssh_args(f'mkdir -p {shlex.quote(self.base + "/incoming")} && chmod 700 {shlex.quote(self.base)}'))
        helper = Path(__file__).with_name('runner.py')
        helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()[:16]
        self.runner = f'{self.base}/runner-{helper_hash}.py'
        self.capture(['gh', 'codespace', 'cp', '--codespace', name, str(helper), f'remote:{self.runner}'])

    def ssh_args(self, command):
        return ['ssh', '-F', str(self.ssh_config), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', self.host, command]

    def transfer_args(self):
        ssh = shlex.join(['ssh', '-F', str(self.ssh_config), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
                          '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3'])
        return ['rsync', '-rtp', '--checksum', '-e', ssh]

    def upload(self, snapshot, transaction_id):
        if not re.fullmatch(r'[0-9a-f]{32}', transaction_id):
            raise TransportError('Invalid transaction id')
        incoming = f'{self.base}/incoming/{transaction_id}'
        self.capture(self.ssh_args('mkdir -p ' + shlex.quote(incoming)))
        self.capture(self.transfer_args() + ['--copy-dest=' + self.base + '/source', str(snapshot) + '/', f'{self.host}:{incoming}/'])

    def run(self, request):
        command = shlex.join(['python3', self.runner, self.base, 'run'])
        result = subprocess.run(self.ssh_args(command), input=json.dumps(request), text=True)
        return result.returncode

    def result(self):
        return json.loads(self.capture(self.ssh_args('cat ' + shlex.quote(self.base + '/result.json'))))

    def download_generated(self, destination):
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.capture(self.transfer_args() + [f'{self.host}:{self.base}/generated/', str(destination) + '/'])

    def remote_status(self):
        command = shlex.join(['python3', self.runner, self.base, 'status'])
        return json.loads(self.capture(self.ssh_args(command)))

    def stop_services(self):
        command = shlex.join(['python3', self.runner, self.base, 'stop'])
        return self.capture(self.ssh_args(command))

    def stop(self, name):
        return self.capture(['gh', 'codespace', 'stop', '--codespace', name])

    def forward_args(self, name, ports):
        return ['gh', 'codespace', 'ports', 'forward', '--codespace', name, *[f'{port}:{port}' for port in ports.values()]]
