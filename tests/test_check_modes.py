"""Check selection, isolated validation sources, and interactive service lifecycle."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from redev.config import ConfigError, validate_config
from redev.snapshot import Snapshot
import test_runner


class CheckConfigTests(unittest.TestCase):
    def test_structured_commands_keep_arguments_and_relative_cwd(self):
        config = validate_config({'version': 1, 'checks': {'unit': {'argv': ['python3', '-m', 'unittest'], 'cwd': 'package'}},
                                  'servicePrepare': 'generate-client', 'sync': {'mode': 'live'}})
        self.assertEqual(config['checks']['unit']['argv'], ['python3', '-m', 'unittest'])
        self.assertEqual(config['checks']['unit']['cwd'], 'package')

    def test_rejects_invalid_command_paths_and_argv(self):
        commands = [{'argv': []}, {'argv': ['']}, {'argv': ['python3', 2]},
                    {'argv': ['python3'], 'cwd': '../outside'}, {'argv': ['python3'], 'cwd': '/tmp'},
                    {'argv': ['python3'], 'unknown': True}, {'argv': ['python3', '\x00']}]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ConfigError):
                validate_config({'version': 1, 'checks': {'test': command}})

    def test_rejects_invalid_sync_mode_and_timeout_before_provisioning(self):
        for settings in [{'sync': {'mode': 'other'}}, {'codespace': {'idleTimeout': '3m'}},
                         {'codespace': {'idleTimeout': '241m'}}, {'codespace': {'idleTimeout': 'later'}}]:
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                validate_config({'version': 1, 'checks': {'test': 'true'}, **settings})


class ValidationSnapshotTests(unittest.TestCase):
    def test_validation_includes_only_tracked_generated_files_and_excludes_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'generated').mkdir()
            (root / 'generated/types.ts').write_text('tracked types')
            (root / 'generated/local.ts').write_text('local generated')
            (root / 'generated/.env').write_text('secret')
            subprocess.run(['git', '-C', str(root), 'add', 'generated/types.ts', 'generated/.env'], check=True)
            config = {'sync': {'generated': ['generated']}}
            with Snapshot(root, config, include_generated=True) as snapshot:
                self.assertEqual(set(snapshot.manifest), {'generated/types.ts'})
            with Snapshot(root, config) as snapshot:
                self.assertEqual(snapshot.manifest, {})


class CheckRunnerTests(unittest.TestCase):
    setUp = test_runner.RunnerTests.setUp
    tearDown = test_runner.RunnerTests.tearDown
    request = test_runner.RunnerTests.request
    invoke = test_runner.RunnerTests.invoke
    assert_ok = test_runner.RunnerTests.assert_ok
    source = test_runner.RunnerTests.source
    result = test_runner.RunnerTests.result
    status = test_runner.RunnerTests.status
    use_service = test_runner.RunnerTests.use_service

    def test_batch_runs_each_selection_and_preserves_first_failure(self):
        self.config['checks'] = {'first': {'argv': ['bash', '-c', 'printf first >> checks.order; exit 12']},
                                 'second': {'argv': ['bash', '-c', 'printf second >> checks.order']}}
        request = self.request()
        request['checks'] = ['first', 'second']
        response = self.invoke(request)
        self.assertEqual(response.returncode, 12, response.stderr)
        self.assertEqual(self.source('checks.order').read_text(), 'firstsecond')
        results = self.result()['checks']
        self.assertEqual([item['name'] for item in results], ['first', 'second'])
        self.assertEqual([item['exit'] for item in results], [12, 0])

    def test_structured_arguments_are_literal_and_cwd_is_respected(self):
        code = 'from pathlib import Path; import json,sys; Path("arguments.json").write_text(json.dumps(sys.argv[1:]))'
        self.config['checks'] = {'unit': {'argv': [sys.executable, '-c', code], 'cwd': 'package'}}
        request = self.request({'package/input.txt': 'fixture'})
        request.update(checks=['unit'], checkArgs=['a b', '$(touch injected)', '; exit 9'])
        self.assert_ok(self.invoke(request))
        self.assertEqual(json.loads(self.source('package/arguments.json').read_text()), ['a b', '$(touch injected)', '; exit 9'])
        self.assertFalse(self.source('injected').exists())
        self.assertEqual(self.result()['checks'][0]['cwd'], 'package')

    def test_rejects_unsafe_or_ambiguous_argument_forwarding_before_setup(self):
        for names in [['test'], ['test', 'test']]:
            request = self.request()
            request.update(checks=names, checkArgs=['--filter'])
            response = self.invoke(request)
            self.assertEqual(response.returncode, 70)
            self.assertFalse(self.source('setup.log').exists())

    def test_service_prepare_does_not_run_for_check_only(self):
        self.config['servicePrepare'] = 'touch service-prepared'
        self.config['checks'] = {'pass': 'true'}
        request = self.request(check='pass')
        request['checkOnly'] = True
        self.assert_ok(self.invoke(request))
        self.assertFalse(self.source('service-prepared').exists())

    def test_check_only_clears_old_service_intent(self):
        self.config['servicePrepare'] = 'printf prepared >> service-prepared'
        self.config['checks'] = {'pass': 'true'}
        self.assert_ok(self.invoke(self.request(start=True)))
        request = self.request(check='pass')
        request['checkOnly'] = True
        self.assert_ok(self.invoke(request))
        self.assertFalse(self.status()['desiredServices'])
        self.assertEqual(self.source('service-prepared').read_text(), 'prepared')

    def test_live_sync_preserves_running_service_and_unchanged_file_mtime(self):
        self.config['sync']['mode'] = 'live'
        self.use_service()
        first = self.request(start=True)
        self.assert_ok(self.invoke(first))
        service_pid = self.status()['services'][0]['pid']
        before = self.source('lock.txt').stat().st_mtime_ns
        self.assert_ok(self.invoke(self.request({'app.txt': 'second\n', 'lock.txt': 'one\n'})))
        self.assertEqual(self.status()['services'][0]['pid'], service_pid)
        self.assertEqual(self.source('lock.txt').stat().st_mtime_ns, before)
        self.assertEqual(self.source('app.txt').read_text(), 'second\n')

    def test_live_sync_restarts_when_setup_inputs_change(self):
        self.config['sync']['mode'] = 'live'
        self.use_service()
        self.assert_ok(self.invoke(self.request(start=True)))
        first_pid = self.status()['services'][0]['pid']
        self.assert_ok(self.invoke(self.request({'app.txt': 'second\n', 'lock.txt': 'two\n'})))
        self.assertNotEqual(self.status()['services'][0]['pid'], first_pid)

    def test_batch_detects_source_mutation_instead_of_claiming_a_snapshot_pass(self):
        self.config['checks'] = {'mutate': 'printf mutated > app.txt', 'pass': 'true'}
        request = self.request()
        request['checks'] = ['mutate', 'pass']
        response = self.invoke(request)
        self.assertEqual(response.returncode, 70)
        self.assertIn('source', self.result()['error'].lower())
        self.assertEqual(len(self.result()['checks']), 1)


if __name__ == '__main__':
    unittest.main()
