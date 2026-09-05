import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

from redev.transport import GitHubTransport, TransportError


class TransportTest(unittest.TestCase):
    def test_ssh_config_limits_transfer_to_selected_host_and_no_broad_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                output = 'Host cs.example\n User codespace\n ProxyCommand gh codespace ssh -c example --stdio\n' if '--config' in args else ''
                return subprocess.CompletedProcess(args, 0, output, '')
            with patch('redev.transport.subprocess.run', command):
                transport = GitHubTransport(directory)
                transport.connect('example', 'a' * 32)
                transport.upload(directory / 'snapshot', 'b' * 32)
            rsync = next(args for args in calls if args[0] == 'rsync' and str(directory / 'snapshot') in args[-2])
            self.assertNotIn('--delete', rsync)
            self.assertEqual(rsync[-1], 'cs.example:/workspaces/.redev/' + 'a' * 32 + '/incoming/' + 'b' * 32 + '/')
            self.assertIn('--checksum', rsync)

    def test_provider_failure_is_not_treated_as_empty_environment_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('redev.transport.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'network down')):
                with self.assertRaisesRegex(TransportError, 'network down'):
                    GitHubTransport(Path(temporary)).list_codespaces('owner/repo')

    def test_check_output_and_exit_status_use_openssh_directly(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = GitHubTransport(Path(temporary))
            transport.host = 'cs.fixture'
            transport.base = '/workspaces/.redev/' + 'a' * 32
            transport.runner = transport.base + '/runner.py'
            def command(args, **kwargs):
                self.assertEqual(args[0], 'ssh')
                self.assertNotIn('stdout', kwargs)
                self.assertEqual(json.loads(kwargs['input'])['check'], 'types')
                return subprocess.CompletedProcess(args, 23)
            with patch('redev.transport.subprocess.run', command):
                self.assertEqual(transport.run({'check': 'types'}), 23)

    def test_runner_upload_uses_rsync_when_gh_scp_rejects_quoted_remote_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = GitHubTransport(Path(temporary))
            uploaded = []
            def execute(args, **kwargs):
                if args[:3] == ['gh', 'codespace', 'cp']:
                    return subprocess.CompletedProcess(args, 1, '', "scp: dest open quoted remote path: No such file or directory")
                if '--config' in args:
                    return subprocess.CompletedProcess(args, 0, 'Host cs.fixture\n User codespace\n', '')
                if args[0] == 'rsync':
                    uploaded.append(args)
                return subprocess.CompletedProcess(args, 0, '', '')
            with patch('redev.transport.subprocess.run', execute):
                transport.connect('fixture-space', 'a' * 32)
            self.assertEqual(len(uploaded), 1)
            self.assertEqual(Path(uploaded[0][-2]).name, 'runner.py')
            self.assertEqual(uploaded[0][-1], 'cs.fixture:' + transport.runner)
            self.assertIn(str(transport.ssh_config), uploaded[0][uploaded[0].index('-e') + 1])

if __name__ == '__main__':
    unittest.main()
