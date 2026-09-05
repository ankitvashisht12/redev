"""Login hooks cannot redirect a source transaction into another checkout."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

import test_runner


class LoginShellTests(unittest.TestCase):
    tearDown = test_runner.RunnerTests.tearDown
    request = test_runner.RunnerTests.request
    assert_ok = test_runner.RunnerTests.assert_ok
    source = test_runner.RunnerTests.source

    def setUp(self):
        test_runner.RunnerTests.setUp(self)
        self.base = Path(self.temporary.name) / "remote ' $HOME; fixture"
        (self.base / 'incoming').mkdir(parents=True)
        self.original_clone = Path(self.temporary.name) / 'original-clone'
        self.original_clone.mkdir()
        bin_directory = Path(self.temporary.name) / 'bin'
        bin_directory.mkdir()
        wrapper = bin_directory / 'bash'
        login_script = 'cd -- ' + shlex.quote(str(self.original_clone)) + '\nexport LOGIN_FIXTURE=loaded\n'
        wrapper.write_text(
            '#!' + sys.executable + '\nimport os, sys\n'
            'assert sys.argv[1] == "-lc"\n'
            'os.execv("/bin/bash", ["/bin/bash", "--noprofile", "--norc", "-c", '
            + repr(login_script) + ' + sys.argv[2]])\n'
        )
        wrapper.chmod(0o755)
        self.environment = {**os.environ, 'PATH': str(bin_directory) + os.pathsep + os.environ['PATH']}

    def invoke(self, request=None, *, action='run', timeout=10):
        return subprocess.run(
            [sys.executable, str(test_runner.RUNNER), str(self.base), action],
            input=json.dumps(request) if request is not None else '',
            text=True, capture_output=True, timeout=timeout, env=self.environment,
        )

    def test_all_shell_hooks_enter_source_after_login_changes_directory(self):
        def record(name):
            return 'test "$LOGIN_FIXTURE" = loaded && pwd > ' + name + '.cwd'
        self.config.update(setup=record('setup'), prepare=record('prepare'),
                           servicePrepare=record('service-prepare'), checks={'test': record('check')})
        self.config['services'] = [{
            'name': 'service', 'when': record('when'),
            'command': record('service') + '; exec ' + shlex.join([sys.executable, '-c', 'import time; time.sleep(60)']),
        }]
        self.assert_ok(self.invoke(self.request(check='test', start=True)))
        for hook in ['setup', 'prepare', 'service-prepare', 'check', 'when', 'service']:
            with self.subTest(hook=hook):
                self.assertTrue(self.source(hook + '.cwd').exists())
                self.assertEqual(Path(self.source(hook + '.cwd').read_text().strip()).resolve(), self.source('').resolve())
                self.assertFalse((self.original_clone / (hook + '.cwd')).exists())

    def test_structured_check_stays_direct_and_preserves_literal_arguments(self):
        code = 'import json,os,sys; from pathlib import Path; Path("argv.json").write_text(json.dumps([os.getcwd(), os.getenv("LOGIN_FIXTURE"), sys.argv[1:]]))'
        arguments = ['two words', '$(touch injected)', '; exit 99']
        self.config['checks'] = {'test': {'argv': [sys.executable, '-c', code, *arguments]}}
        self.assert_ok(self.invoke(self.request(check='test')))
        directory, login_value, actual = json.loads(self.source('argv.json').read_text())
        self.assertEqual(Path(directory).resolve(), self.source('').resolve())
        self.assertIsNone(login_value)
        self.assertEqual(actual, arguments)

    def test_legacy_setup_cache_is_invalidated_once(self):
        request = self.request()
        self.assert_ok(self.invoke(request))
        state_path = self.base / 'state.json'
        state = json.loads(state_path.read_text())
        setup_manifest = {'lock.txt': request['manifest']['lock.txt']}
        state['setupDigest'] = hashlib.sha256(json.dumps([
            request['config'], setup_manifest, request['ports']
        ], sort_keys=True).encode()).hexdigest()
        state_path.write_text(json.dumps(state))
        self.assert_ok(self.invoke(request))
        self.assert_ok(self.invoke(request))
        self.assertEqual(self.source('setup.log').read_text().splitlines(), ['setup', 'setup'])


if __name__ == '__main__':
    unittest.main()
