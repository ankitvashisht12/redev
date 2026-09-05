"""Exercise the uploaded runner with real files and child processes."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


RUNNER = Path(__file__).resolve().parents[1] / "redev" / "runner.py"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name) / "remote"
        (self.base / "incoming").mkdir(parents=True)
        self.config = {
            "version": 1,
            "setup": "printf 'setup\\n' >> setup.log",
            "setupInputs": ["lock.txt"],
            "prepare": "printf 'prepare\\n' >> prepare.log",
            "checks": {"test": "cat app.txt; printf 'check-error\\n' >&2; exit 7"},
            "services": [],
            "sync": {"exclude": ["keep"], "generated": ["generated"]},
            "ports": {},
        }

    def tearDown(self):
        if RUNNER.exists():
            self.invoke(action="stop")
        self.temporary.cleanup()

    def request(self, files=None, *, check=None, start=False):
        files = files if files is not None else {"app.txt": "first\n", "lock.txt": "one\n"}
        manifest = {}
        for relative, content in files.items():
            path = self.base / "incoming" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            manifest[relative] = {
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "mode": 0o644,
            }
        snapshot = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        return {
            "transactionId": uuid.uuid4().hex,
            "config": copy.deepcopy(self.config),
            "manifest": manifest,
            "snapshotId": snapshot,
            "ports": dict(self.config["ports"]),
            "check": check,
            "startServices": start,
        }

    def invoke(self, request=None, *, action="run", timeout=10):
        return subprocess.run(
            [sys.executable, str(RUNNER), str(self.base), action],
            input=json.dumps(request) if request is not None else "",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def assert_ok(self, response):
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)

    def source(self, relative):
        return self.base / "source" / relative

    def result(self):
        return json.loads((self.base / "result.json").read_text())

    def status(self):
        response = self.invoke(action="status")
        self.assert_ok(response)
        return json.loads(response.stdout)

    def test_sync_uses_manifest_and_only_deletes_previously_managed_files(self):
        request = self.request({"app.txt": "first\n", "old.txt": "old\n"})
        (self.base / "incoming" / "stale.txt").write_text("stale\n")
        self.assert_ok(self.invoke(request))
        self.source("unmanaged.txt").write_text("keep\n")
        self.assert_ok(self.invoke(self.request({"app.txt": "second\n"})))
        self.assertEqual(self.source("app.txt").read_text(), "second\n")
        self.assertFalse(self.source("old.txt").exists())
        self.assertFalse(self.source("stale.txt").exists())
        self.assertEqual(self.source("unmanaged.txt").read_text(), "keep\n")

    def test_setup_cache_and_prepare_follow_source_changes_and_checks(self):
        request = self.request()
        self.assert_ok(self.invoke(request))
        self.assert_ok(self.invoke(request))
        self.assert_ok(self.invoke(self.request({"app.txt": "second\n", "lock.txt": "one\n"})))
        self.assert_ok(self.invoke(self.request({"app.txt": "third\n", "lock.txt": "two\n"})))
        self.assertEqual(self.source("setup.log").read_text().splitlines(), ["setup", "setup"])
        self.assertEqual(self.source("prepare.log").read_text().splitlines(), ["prepare"] * 3)
        response = self.invoke(self.request({"app.txt": "third\n", "lock.txt": "two\n"}, check="test"))
        self.assertEqual(response.returncode, 7, response.stderr)
        self.assertIn("third", response.stdout)
        self.assertIn("check-error", response.stderr)
        self.assertEqual(self.result()["checkExit"], 7)
        self.assertFalse(self.result()["success"])
        self.assertEqual(self.source("prepare.log").read_text().splitlines(), ["prepare"] * 4)

    def test_bad_hash_is_rejected_before_source_changes(self):
        self.assert_ok(self.invoke(self.request()))
        request = self.request({"app.txt": "changed\n"})
        request["manifest"]["app.txt"]["sha256"] = "0" * 64
        response = self.invoke(request)
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertEqual(self.source("app.txt").read_text(), "first\n")
        self.assertIn("hash", self.result()["error"].lower())

    def test_unmanaged_collision_is_not_overwritten(self):
        self.assert_ok(self.invoke(self.request()))
        self.source("local.txt").write_text("remote output\n")
        request = self.request({"app.txt": "changed\n", "local.txt": "incoming\n"})
        response = self.invoke(request)
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertEqual(self.source("local.txt").read_text(), "remote output\n")
        self.assertEqual(self.source("app.txt").read_text(), "first\n")

    def test_traversal_protected_paths_and_symlink_ancestors_are_rejected(self):
        for relative in ("../escape", ".git/config", "nested/.env.local", "node_modules/x", "generated/x", "keep/x"):
            with self.subTest(relative=relative):
                request = self.request({})
                request["manifest"][relative] = {"sha256": "0" * 64, "mode": 0o644}
                response = self.invoke(request)
                self.assertEqual(response.returncode, 70, response.stderr)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "file").write_text("secret\n")
        (self.base / "incoming" / "link").symlink_to(outside, target_is_directory=True)
        request = self.request({})
        request["manifest"]["link/file"] = {
            "sha256": hashlib.sha256(b"secret\n").hexdigest(), "mode": 0o644,
        }
        self.assertEqual(self.invoke(request).returncode, 70)
        self.assertEqual((outside / "file").read_text(), "secret\n")

    def test_protected_previous_entries_are_never_deleted(self):
        self.assert_ok(self.invoke(self.request()))
        protected = [".env", ".git/config", "node_modules/cache", "keep/file", "generated/types.ts"]
        state = json.loads((self.base / "state.json").read_text())
        for relative in protected:
            path = self.source(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep\n")
            state["manifest"][relative] = {"sha256": "0" * 64, "mode": 0o644}
        (self.base / "state.json").write_text(json.dumps(state))
        self.assert_ok(self.invoke(self.request({"app.txt": "changed\n"})))
        for relative in protected:
            self.assertEqual(self.source(relative).read_text(), "keep\n")

    def test_prepare_failure_reports_infrastructure_error_without_check_exit(self):
        self.config["prepare"] = "exit 4"
        response = self.invoke(self.request(check="test"))
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertIsNone(self.result()["checkExit"])
        self.assertIn("prepare", self.result()["error"])

    def test_generated_files_are_exported_with_relative_paths(self):
        self.config["prepare"] = "mkdir -p generated/nested; printf 'types\\n' > generated/nested/types.ts"
        request = self.request()
        self.assert_ok(self.invoke(request))
        self.assertEqual((self.base / "generated" / "generated/nested/types.ts").read_text(), "types\n")
        self.assertEqual(self.result()["generated"], {
            "generated/nested/types.ts": {"sha256": hashlib.sha256(b"types\n").hexdigest(), "mode": 0o644},
        })
        self.assertEqual(self.result()["snapshotId"], request["snapshotId"])
        self.assertEqual(self.result()["transactionId"], request["transactionId"])

    def test_sensitive_previous_entries_are_not_deleted_and_examples_are_allowed(self):
        self.assert_ok(self.invoke(self.request({".env.example": "PUBLIC=example\n"})))
        protected = ["dist/file", ".config/key", ".codex/agent", "task.log", "project.tsbuildinfo"]
        state = json.loads((self.base / "state.json").read_text())
        for relative in protected:
            path = self.source(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep\n")
            state["manifest"][relative] = {"sha256": "0" * 64, "mode": 0o644}
        (self.base / "state.json").write_text(json.dumps(state))
        self.assert_ok(self.invoke(self.request({".env.example": "PUBLIC=new\n"})))
        for relative in protected:
            self.assertEqual(self.source(relative).read_text(), "keep\n")

    def use_service(self, *, command=None, timeout=3):
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        self.config["ports"] = {"web": port}
        service_code = (
            "import os,socket,time; "
            "server=socket.socket(); server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
            "server.bind(('127.0.0.1',int(os.environ['REDEV_PORT_WEB']))); server.listen(); "
            "print('service ready',flush=True); "
            "time.sleep(60)"
        )
        self.config["services"] = [{
            "name": "web", "port": "web", "readyTimeout": timeout,
            "command": command or shlex.join([sys.executable, "-c", service_code]),
        }]
        return port

    def test_services_stay_alive_on_noop_and_restart_after_failed_check(self):
        port = self.use_service()
        request = self.request(start=True)
        self.assert_ok(self.invoke(request))
        initial = self.status()["services"][0]
        self.assertTrue(initial["active"])
        self.assertEqual(initial["port"], port)
        self.assertIn("service ready", Path(initial["log"]).read_text())
        self.assert_ok(self.invoke(self.request()))
        self.assertEqual(self.status()["services"][0]["pid"], initial["pid"])
        self.assertEqual(self.source("prepare.log").read_text().splitlines(), ["prepare"])
        check_code = (
            "import os,socket; "
            "probe=socket.socket(); "
            "assert probe.connect_ex(('127.0.0.1',int(os.environ['REDEV_PORT_WEB']))) != 0; "
            "print(os.environ['REDEV_REMOTE'],os.environ['REDEV_URL_WEB']); "
            "raise SystemExit(9)"
        )
        self.config["checks"]["test"] = shlex.join([sys.executable, "-c", check_code])
        response = self.invoke(self.request(check="test"))
        self.assertEqual(response.returncode, 9, response.stderr)
        self.assertIn("1 http://localhost:" + str(port), response.stdout)
        restarted = self.status()["services"][0]
        self.assertTrue(restarted["active"])
        self.assertNotEqual(restarted["pid"], initial["pid"])
        self.assertEqual(self.result()["checkExit"], 9)
        self.assert_ok(self.invoke(action="stop"))
        self.assertFalse(self.status()["desiredServices"])
        self.assertFalse(self.status()["services"][0]["active"])

    def test_service_start_failure_is_reported_with_log_location(self):
        self.use_service(command="printf 'failed service\\n'; exit 6")
        response = self.invoke(self.request(start=True))
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertIn("web", self.result()["error"])
        service = self.status()["services"][0]
        self.assertFalse(service["active"])
        self.assertIn("error", service)
        self.assertIn("failed service", Path(service["log"]).read_text())

    def test_setup_failure_stops_desired_services(self):
        self.use_service()
        self.assert_ok(self.invoke(self.request(start=True)))
        self.config["setup"] = "exit 2"
        self.assertEqual(self.invoke(self.request()).returncode, 70)
        self.assertFalse(self.status()["services"][0]["active"])

    def test_generated_export_precedes_service_mutation(self):
        self.config["prepare"] = "mkdir -p generated; printf 'prepared\\n' > generated/types.ts"
        code = "from pathlib import Path; import time; Path('generated/types.ts').write_text('watcher\\n'); time.sleep(60)"
        self.config["services"] = [{"name": "writer", "command": shlex.join([sys.executable, "-c", code])}]
        self.assert_ok(self.invoke(self.request(start=True)))
        self.assertEqual(self.source("generated/types.ts").read_text(), "watcher\n")
        self.assertEqual((self.base / "generated/generated/types.ts").read_text(), "prepared\n")
        self.assert_ok(self.invoke(self.request()))
        self.assertEqual((self.base / "generated/generated/types.ts").read_text(), "prepared\n")

    def test_stop_does_not_kill_a_process_with_a_different_identity(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            (self.base / "state.json").write_text(json.dumps({
                "manifest": {}, "desiredServices": True,
                "services": [{"name": "other", "pid": process.pid, "identity": "wrong", "log": "unused"}],
            }))
            self.assert_ok(self.invoke(action="stop"))
            self.assertIsNone(process.poll())
            self.assertFalse(self.status()["services"][0]["active"])
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def wait_for(self, path, *, timeout=4):
        deadline = time.monotonic() + timeout
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, "Timed out waiting for " + str(path))
            time.sleep(0.02)

    def launch(self, request):
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), str(self.base), "run"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        process.stdin.write(json.dumps(request))
        process.stdin.close()
        process.stdin = None
        return process

    def test_prepare_failure_retries_the_same_snapshot(self):
        self.assert_ok(self.invoke(self.request()))
        self.config["prepare"] = "if [ ! -f retry.marker ]; then touch retry.marker; exit 2; fi; printf 'retried' > retry.done"
        request = self.request()
        self.assertEqual(self.invoke(request).returncode, 70)
        self.assert_ok(self.invoke(request))
        self.assertTrue(self.source("retry.done").exists())

    def test_transaction_lock_prevents_sync_during_check(self):
        code = "from pathlib import Path; import time; Path('check.started').touch(); time.sleep(0.7); assert Path('app.txt').read_text() == 'first\\n'"
        self.config["checks"]["test"] = shlex.join([sys.executable, "-c", code])
        first = self.launch(self.request(check="test"))
        second = None
        try:
            self.wait_for(self.source("check.started"))
            second = self.launch(self.request({"app.txt": "second\n", "lock.txt": "one\n"}))
            time.sleep(0.15)
            self.assertIsNone(second.poll())
            self.assertEqual(self.source("app.txt").read_text(), "first\n")
            first_output = first.communicate(timeout=4)
            self.assertEqual(first.returncode, 0, first_output)
            second_output = second.communicate(timeout=4)
            self.assertEqual(second.returncode, 0, second_output)
            self.assertEqual(self.source("app.txt").read_text(), "second\n")
        finally:
            for process in (first, second):
                if process and process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)

    def test_check_inherits_lock_when_runner_is_killed(self):
        code = "from pathlib import Path; import time; Path('check.started').touch(); time.sleep(0.7); assert Path('app.txt').read_text() == 'first\\n'; Path('check.finished').touch()"
        self.config["checks"]["test"] = shlex.join([sys.executable, "-c", code])
        first = self.launch(self.request(check="test"))
        second = None
        try:
            self.wait_for(self.source("check.started"))
            first.kill()
            first.wait(timeout=2)
            second = self.launch(self.request({"app.txt": "second\n", "lock.txt": "one\n"}))
            time.sleep(0.15)
            self.assertIsNone(second.poll())
            second_output = second.communicate(timeout=4)
            self.assertEqual(second.returncode, 0, second_output)
            self.assertTrue(self.source("check.finished").exists())
        finally:
            if first.poll() is None:
                first.terminate()
            first.communicate(timeout=4)
            if second and second.poll() is None:
                second.terminate()
                second.communicate(timeout=4)

    def test_sigterm_stops_check_descendants(self):
        child_code = "from pathlib import Path; import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
        parent_code = "import subprocess,time; subprocess.Popen(" + repr([sys.executable, "-c", child_code]) + "); time.sleep(30)"
        self.config["checks"]["test"] = shlex.join([sys.executable, "-c", parent_code])
        runner = self.launch(self.request(check="test"))
        child_pid = None
        try:
            self.wait_for(self.source("child.pid"))
            child_pid = int(self.source("child.pid").read_text())
            runner.terminate()
            output = runner.communicate(timeout=6)
            self.assertEqual(runner.returncode, 70, output)
            self.assertFalse(self.result()["success"])
            response = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
            self.assertTrue(response.returncode != 0 or response.stdout.strip().startswith("Z"), response.stdout)
        finally:
            if runner.poll() is None:
                runner.kill()
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            runner.communicate(timeout=3)

    def test_generated_symlink_is_rejected(self):
        self.config["prepare"] = "mkdir -p generated; ln -s ../app.txt generated/link"
        response = self.invoke(self.request())
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertIn("Symlink", self.result()["error"])

    def test_source_symlink_and_directory_collisions_preserve_targets(self):
        self.assert_ok(self.invoke(self.request()))
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("outside\n")
        self.source("app.txt").unlink()
        self.source("app.txt").symlink_to(outside)
        self.assertEqual(self.invoke(self.request({"app.txt": "new\n"})).returncode, 70)
        self.assertEqual(outside.read_text(), "outside\n")
        self.source("app.txt").unlink()
        self.source("app.txt").mkdir()
        self.assertEqual(self.invoke(self.request({"app.txt": "new\n"})).returncode, 70)
        self.assertTrue(self.source("app.txt").is_dir())

    def test_executable_mode_is_applied(self):
        request = self.request({"tool.sh": "exit 0\n"})
        request["manifest"]["tool.sh"]["mode"] = 0o755
        self.assert_ok(self.invoke(request))
        self.assertEqual(self.source("tool.sh").stat().st_mode & 0o777, 0o755)

    def test_unique_incoming_directory_is_selected_and_id_must_match(self):
        request = self.request({"app.txt": "unique\n"})
        request["incomingId"] = request["transactionId"]
        unique = self.base / "incoming" / request["incomingId"]
        unique.mkdir()
        (self.base / "incoming/app.txt").rename(unique / "app.txt")
        (self.base / "incoming/app.txt").write_text("wrong staging directory\n")
        self.assert_ok(self.invoke(request))
        self.assertEqual(self.source("app.txt").read_text(), "unique\n")
        request["incomingId"] = "f" * 32
        response = self.invoke(request)
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertIn("incomingId", self.result()["error"])

    def test_invalid_request_replaces_a_prior_result_with_failure(self):
        self.assert_ok(self.invoke(self.request()))
        response = self.invoke(["invalid"])
        self.assertEqual(response.returncode, 70)
        self.assertFalse(self.result()["success"])
        self.assertIsNone(self.result()["transactionId"])

    def test_partial_json_replaces_a_prior_result_with_failure(self):
        self.assert_ok(self.invoke(self.request()))
        response = subprocess.run(
            [sys.executable, str(RUNNER), str(self.base), "run"],
            input='{"transactionId":', text=True, capture_output=True,
        )
        self.assertEqual(response.returncode, 70)
        self.assertFalse(self.result()["success"])

    def test_readiness_timeout_stops_service(self):
        self.use_service(command=shlex.join([sys.executable, "-c", "import time; time.sleep(30)"]), timeout=1)
        response = self.invoke(self.request(start=True))
        self.assertEqual(response.returncode, 70, response.stderr)
        service = self.status()["services"][0]
        self.assertFalse(service["active"])
        self.assertIn("ready", service["error"])

    def test_services_start_in_configured_order_and_all_stop_on_failure(self):
        self.use_service()
        check_port_code = (
            "from pathlib import Path; import os,socket; "
            "socket.create_connection(('127.0.0.1',int(os.environ['REDEV_PORT_WEB']))); "
            "Path('second.started').touch(); raise SystemExit(3)"
        )
        self.config["services"].append({"name": "second", "command": shlex.join([sys.executable, "-c", check_port_code])})
        self.assertEqual(self.invoke(self.request(start=True)).returncode, 70)
        self.assertTrue(self.source("second.started").exists())
        self.assertEqual([service["active"] for service in self.status()["services"]], [False, False])

    def test_deleted_generated_files_are_removed_from_export(self):
        self.config["prepare"] = "mkdir -p generated; if [ -f remove.txt ]; then rm -f generated/old.ts; else printf 'types' > generated/old.ts; fi"
        self.assert_ok(self.invoke(self.request()))
        self.assertTrue((self.base / "generated/generated/old.ts").exists())
        self.assert_ok(self.invoke(self.request({"app.txt": "second\n", "remove.txt": "yes\n"})))
        self.assertEqual(self.result()["generated"], {})
        self.assertFalse((self.base / "generated/generated/old.ts").exists())

    def test_environment_name_collision_is_rejected(self):
        self.config["ports"] = {"api-local": 12345, "api_local": 12346}
        response = self.invoke(self.request())
        self.assertEqual(response.returncode, 70, response.stderr)

    def test_unique_incoming_cleanup_keeps_other_transactions(self):
        request = self.request({"nested/app.txt": "unique\n"})
        request["incomingId"] = request["transactionId"]
        unique = self.base / "incoming" / request["incomingId"]
        unique.mkdir()
        (self.base / "incoming/nested").rename(unique / "nested")
        other = self.base / "incoming" / ("e" * 32)
        other.mkdir()
        (other / "app.txt").write_text("other\n")
        self.assert_ok(self.invoke(request))
        self.assertFalse(unique.exists())
        self.assertEqual((other / "app.txt").read_text(), "other\n")

    def test_generated_export_failure_retries_the_same_snapshot(self):
        self.config["prepare"] = "mkdir -p generated; printf 'types' > generated/types.ts"
        (self.base / "generated/generated/types.ts").mkdir(parents=True)
        request = self.request()
        self.assertEqual(self.invoke(request).returncode, 70)
        (self.base / "generated/generated/types.ts").rmdir()
        self.assert_ok(self.invoke(request))
        self.assertEqual((self.base / "generated/generated/types.ts").read_text(), "types")

    def test_stop_removes_service_children_that_ignore_term(self):
        child_code = "from pathlib import Path; import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('service.child.pid').write_text(str(os.getpid())); time.sleep(30)"
        parent_code = "import subprocess,time; subprocess.Popen(" + repr([sys.executable, "-c", child_code]) + "); time.sleep(30)"
        self.config["services"] = [{"name": "parent", "command": shlex.join([sys.executable, "-c", parent_code])}]
        child_pid = None
        try:
            self.assert_ok(self.invoke(self.request(start=True)))
            self.wait_for(self.source("service.child.pid"))
            child_pid = int(self.source("service.child.pid").read_text())
            self.assert_ok(self.invoke(action="stop"))
            response = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
            self.assertTrue(response.returncode != 0 or response.stdout.strip().startswith("Z"), response.stdout)
        finally:
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_managed_file_can_become_a_directory(self):
        self.assert_ok(self.invoke(self.request({"module": "old\n"})))
        (self.base / "incoming/module").unlink()
        self.assert_ok(self.invoke(self.request({"module/index.txt": "new\n"})))
        self.assertEqual(self.source("module/index.txt").read_text(), "new\n")

    def test_managed_directory_can_become_a_file(self):
        self.assert_ok(self.invoke(self.request({"module/nested/index.txt": "old\n"})))
        (self.base / "incoming/module/nested/index.txt").unlink()
        (self.base / "incoming/module/nested").rmdir()
        (self.base / "incoming/module").rmdir()
        self.assert_ok(self.invoke(self.request({"module": "new\n"})))
        self.assertEqual(self.source("module").read_text(), "new\n")

    def test_directory_replacement_preserves_unmanaged_files_and_empty_directories(self):
        self.assert_ok(self.invoke(self.request({"module/index.txt": "old\n"})))
        self.source("module/keep").mkdir()
        (self.base / "incoming/module/index.txt").unlink()
        (self.base / "incoming/module").rmdir()
        response = self.invoke(self.request({"module": "new\n"}))
        self.assertEqual(response.returncode, 70, response.stderr)
        self.assertTrue(self.source("module/keep").is_dir())
        self.assertEqual(self.source("module/index.txt").read_text(), "old\n")

    def test_each_check_restores_the_requested_source_snapshot(self):
        self.config["checks"]["test"] = "cat app.txt; printf 'check edit\\n' > app.txt"
        request = self.request(check="test")
        first = self.invoke(request)
        self.assert_ok(first)
        second = self.invoke(request)
        self.assert_ok(second)
        self.assertEqual(first.stdout, "first\n")
        self.assertEqual(second.stdout, "first\n")


if __name__ == "__main__":
    unittest.main()
