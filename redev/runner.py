#!/usr/bin/env python3
"""Standalone remote source transaction runner; upload this file without imports."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


FAILURE = 70
ACTIVITY_INTERVAL = 30
SETUP_EXECUTION_VERSION = 2
HASH = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
PROTECTED_PARTS = {
    ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".gnupg", ".convex", ".cache",
    ".npm", ".pnpm-store", ".yarn", ".next", ".turbo", ".venv", "venv",
    "node_modules", "__pycache__", "credentials.json",
    ".credentials", ".npmrc", ".netrc", ".pypirc", "id_rsa", "id_ed25519",
    "dist", "build", "out", "coverage", ".config", ".vercel",
    ".redev", ".agents", ".claude", ".codex",
}
CREDENTIAL_SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".dart", ".ex", ".exs", ".go", ".h", ".hpp",
    ".html", ".java", ".js", ".jsx", ".mjs", ".cjs", ".kt", ".kts", ".lua", ".php",
    ".py", ".pyi", ".rb", ".rs", ".scala", ".scss", ".svelte", ".swift", ".ts",
    ".tsx", ".mts", ".cts", ".vue",
}


class RunnerError(Exception):
    pass


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RunnerError("Invalid relative path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise RunnerError("Unsafe relative path: " + value)
    return value


def within_prefix(relative, prefix):
    return relative == prefix or relative.startswith(prefix + "/")


def protected(relative, prefixes=(), *, path_prefix=False):
    parts = relative.split("/")
    credential_data = (not path_prefix and "credentials" in parts
                       and Path(parts[-1]).suffix.lower() not in CREDENTIAL_SOURCE_EXTENSIONS)
    return credential_data or any(
        part in PROTECTED_PARTS or (part.startswith(".env") and part != ".env.example")
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".tsbuildinfo", ".log"))
        for part in parts
    ) or any(within_prefix(relative, prefix) for prefix in prefixes)


def inspect_path(root, relative, *, allow_missing=True, pending_removals=()):
    """Reject symlinks at every component, including the destination itself."""
    relative_path(relative)
    cursor = root
    parts = relative.split("/")
    for position, part in enumerate(parts):
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            if allow_missing:
                return root / relative
            raise RunnerError("Missing file: " + str(cursor))
        if stat.S_ISLNK(metadata.st_mode):
            raise RunnerError("Symlink path: " + str(cursor))
        if cursor in pending_removals:
            return root / relative
        if position < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise RunnerError("Non-directory path component: " + str(cursor))
    return cursor


def directory(path):
    if path.is_symlink():
        raise RunnerError("Symlink directory: " + str(path))
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RunnerError("Expected directory: " + str(path))


def atomic_json(base, name, value):
    destination = inspect_path(base, name)
    descriptor, temporary = tempfile.mkstemp(prefix=".json-", dir=base)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(base):
    path = inspect_path(base, "state.json")
    if not path.exists():
        return {"manifest": {}, "services": [], "desiredServices": False}
    with path.open() as source:
        state = json.load(source)
    if not isinstance(state, dict) or not isinstance(state.get("manifest", {}), dict):
        raise RunnerError("Invalid remote state")
    return state


def validate_request(request):
    if not isinstance(request, dict):
        raise RunnerError("Request must be an object")
    config = request.get("config")
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RunnerError("Unsupported configuration version")
    for field in ("setup", "prepare", "servicePrepare"):
        if field in config and not isinstance(config[field], str):
            raise RunnerError(field + " must be a command string")
    checks = config.get("checks", {})
    if not isinstance(checks, dict):
        raise RunnerError("Invalid checks")
    for name, settings in checks.items():
        if not isinstance(name, str):
            raise RunnerError("Invalid check name")
        check_invocation(settings)
    selected = selected_checks(request)
    if any(name not in checks for name in selected) or len(set(selected)) != len(selected):
        raise RunnerError("Unknown or duplicate check")
    arguments = request.get("checkArgs", [])
    if not isinstance(arguments, list) or any(not isinstance(argument, str) or "\x00" in argument for argument in arguments):
        raise RunnerError("Check arguments must be strings")
    if arguments and (len(selected) != 1 or not isinstance(checks[selected[0]], dict)):
        raise RunnerError("Arguments require one structured check")
    if not isinstance(request.get("checkOnly", False), bool):
        raise RunnerError("checkOnly must be a boolean")
    if not isinstance(request.get("startServices", False), bool):
        raise RunnerError("startServices must be a boolean")
    if not isinstance(request.get("snapshotId"), str) or not HASH.fullmatch(request["snapshotId"]):
        raise RunnerError("Invalid snapshotId")
    if not isinstance(request.get("transactionId"), str) or not re.fullmatch(r"[0-9a-f]{32,64}", request["transactionId"]):
        raise RunnerError("Invalid transactionId")
    if "incomingId" in request and request["incomingId"] != request["transactionId"]:
        raise RunnerError("incomingId must equal transactionId")
    sync = config.get("sync", {})
    if not isinstance(sync, dict):
        raise RunnerError("Invalid sync configuration")
    if sync.get("mode", "restart") not in ("restart", "live"):
        raise RunnerError("Invalid sync mode")
    if type(sync.get("seedGenerated", False)) is not bool:
        raise RunnerError("seedGenerated must be a boolean")
    for values in (sync.get("exclude", []), sync.get("generated", []), config.get("setupInputs", [])):
        if not isinstance(values, list):
            raise RunnerError("Path prefixes must be arrays")
        for value in values:
            relative_path(value)
    for prefix in sync.get("generated", []):
        if protected(prefix, path_prefix=True):
            raise RunnerError("Protected generated path: " + prefix)
    ports = request.get("ports", {})
    if not isinstance(ports, dict):
        raise RunnerError("Invalid ports")
    for name, port in ports.items():
        if not NAME.fullmatch(name) or type(port) is not int or not 1 <= port <= 65535:
            raise RunnerError("Invalid named port")
    if len({name.upper().replace("-", "_") for name in ports}) != len(ports):
        raise RunnerError("Named ports produce duplicate environment variables")
    schemes = config.get("portSchemes", {})
    if not isinstance(schemes, dict) or any(name not in ports or scheme not in ("http", "https") for name, scheme in schemes.items()):
        raise RunnerError("Invalid portSchemes")
    services = config.get("services", [])
    if not isinstance(services, list):
        raise RunnerError("Invalid services")
    names = set()
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("name"), str) or not NAME.fullmatch(service["name"]):
            raise RunnerError("Invalid service name")
        if service["name"] in names or not isinstance(service.get("command"), str):
            raise RunnerError("Invalid or duplicate service")
        if "when" in service and (not isinstance(service["when"], str) or not service["when"].strip() or "\x00" in service["when"]):
            raise RunnerError("Invalid service condition")
        names.add(service["name"])
        if "port" in service and service["port"] not in ports:
            raise RunnerError("Unknown service port")
        timeout = service.get("readyTimeout", 60)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
            raise RunnerError("readyTimeout must be between 1 and 600 seconds")
    manifest = request.get("manifest")
    if not isinstance(manifest, dict):
        raise RunnerError("Invalid manifest")
    prefixes = sync.get("exclude", []) + sync.get("generated", [])
    for relative, entry in manifest.items():
        relative_path(relative)
        if protected(relative, prefixes):
            raise RunnerError("Protected source path: " + relative)
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) or not HASH.fullmatch(entry["sha256"]):
            raise RunnerError("Invalid manifest hash: " + relative)
        if entry.get("mode") not in (0o644, 0o755):
            raise RunnerError("Invalid manifest mode: " + relative)
    seeds = request.get("seedManifest", {})
    if not isinstance(seeds, dict) or seeds and (not sync.get("seedGenerated") or request.get("checkOnly")):
        raise RunnerError("Generated seeds require interactive seedGenerated mode")
    for relative, entry in seeds.items():
        relative_path(relative)
        if (relative in manifest or not any(within_prefix(relative, prefix) for prefix in sync.get("generated", []))
                or protected(relative, sync.get("exclude", []))):
            raise RunnerError("Generated seed path is not allowed: " + relative)
        if (not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str)
                or not HASH.fullmatch(entry["sha256"]) or entry.get("mode") not in (0o644, 0o755)):
            raise RunnerError("Invalid generated seed record: " + relative)
    return config


def selected_checks(request):
    selected = request.get("checks")
    if selected is None:
        return [request["check"]] if request.get("check") is not None else []
    if not isinstance(selected, list) or not selected or not all(isinstance(name, str) for name in selected):
        raise RunnerError("checks must be a nonempty list of names")
    if request.get("check") is not None:
        raise RunnerError("Use checks or check, not both")
    return selected


def check_invocation(settings, arguments=()):
    if isinstance(settings, str):
        if not settings.strip() or "\x00" in settings or arguments:
            raise RunnerError("Invalid shell check")
        return ["bash", "-lc", settings], "."
    if not isinstance(settings, dict) or set(settings) - {"argv", "cwd"}:
        raise RunnerError("Invalid structured check")
    argv = settings.get("argv")
    if (not isinstance(argv, list) or not argv or not argv[0]
            or any(not isinstance(argument, str) or "\x00" in argument for argument in argv)):
        raise RunnerError("Invalid check argv")
    cwd = settings.get("cwd", ".")
    if cwd != ".":
        relative_path(cwd)
    return argv + list(arguments), cwd


def file_hash(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise RunnerError("Expected regular file: " + str(path))
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def copy_file(source, destination, entry, overwrite=True):
    directory(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".redev-", dir=destination.parent)
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(source_descriptor, "rb") as source_file, os.fdopen(descriptor, "wb") as output:
            if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
                raise RunnerError("Expected regular file: " + str(source))
            digest = hashlib.sha256()
            for block in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(block)
                output.write(block)
            if digest.hexdigest() != entry["sha256"]:
                raise RunnerError("Source hash changed: " + str(source))
            os.fchmod(output.fileno(), entry["mode"])
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_source(base, request, previous):
    source = base / "source"
    incoming = incoming_directory(base, request)
    manifest = request["manifest"]
    sync = request["config"].get("sync", {})
    prefixes = sync.get("exclude", []) + sync.get("generated", [])
    removals = []
    for relative in previous.keys() - manifest.keys():
        relative_path(relative)
        if protected(relative, prefixes):
            continue
        target = inspect_path(source, relative)
        if target.exists():
            if not target.is_file():
                raise RunnerError("Managed file became a directory: " + relative)
            removals.append(target)
    removed_files = set(removals)
    removed_parents = set()
    for target in removals:
        parent = target.parent
        while parent != source:
            removed_parents.add(parent)
            parent = parent.parent
    removed_directories = set()
    for relative, entry in manifest.items():
        staged = inspect_path(incoming, relative, allow_missing=False)
        if file_hash(staged) != entry["sha256"]:
            raise RunnerError("Incoming hash mismatch: " + relative)
        target = inspect_path(source, relative, pending_removals=removed_files)
        if target.is_dir():
            if target not in removed_parents:
                raise RunnerError("Unmanaged directory collision: " + relative)
            for current, directories, files in os.walk(target, followlinks=False):
                current = Path(current)
                if current not in removed_parents:
                    raise RunnerError("Unmanaged directory collision: " + str(current))
                removed_directories.add(current)
                for name in directories + files:
                    child = current / name
                    if child.is_symlink() or child not in removed_files | removed_parents:
                        raise RunnerError("Unmanaged directory content: " + str(child))
        elif target.exists() and (relative not in previous or not target.is_file()):
            raise RunnerError("Unmanaged source collision: " + relative)
    for target in removals:
        target.unlink()
    for target in sorted(removed_directories, key=lambda path: len(path.parts), reverse=True):
        target.rmdir()
    for relative, entry in manifest.items():
        target = source / relative
        if (previous.get(relative) == entry and target.is_file() and not target.is_symlink()
                and file_hash(target) == entry["sha256"]
                and stat.S_IMODE(target.stat().st_mode) == entry["mode"]):
            continue
        copy_file(incoming / relative, target, entry)


def incoming_directory(base, request):
    relative = "incoming" + ("/" + request["incomingId"] if "incomingId" in request else "")
    return inspect_path(base, relative, allow_missing=False)


def seed_generated(base, request):
    incoming = incoming_directory(base, request)
    source = base / "source"
    for relative, entry in request.get("seedManifest", {}).items():
        staged = inspect_path(incoming, relative, allow_missing=False)
        if file_hash(staged) != entry["sha256"]:
            raise RunnerError("Generated seed hash mismatch: " + relative)
        target = inspect_path(source, relative)
        if not target.exists():
            copy_file(staged, target, entry, overwrite=False)
        target = inspect_path(source, relative, allow_missing=False)
        if not target.is_file():
            raise RunnerError("Generated seed destination is not a regular file: " + relative)


def clean_incoming(base, request):
    """Remove only this transaction's named files and their empty directories."""
    if "incomingId" not in request:
        return
    incoming = incoming_directory(base, request)
    parents = {incoming}
    for relative, entry in {**request["manifest"], **request.get("seedManifest", {})}.items():
        path = inspect_path(incoming, relative)
        if path.exists():
            if file_hash(path) != entry["sha256"]:
                raise RunnerError("Incoming file changed before cleanup: " + relative)
            path.unlink()
        parent = path.parent
        while parent != incoming:
            parents.add(parent)
            parent = parent.parent
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except (FileNotFoundError, OSError):
            pass


def command_environment(ports, schemes=None):
    environment = os.environ.copy()
    environment["REDEV_REMOTE"] = "1"
    for name, port in ports.items():
        suffix = name.upper().replace("-", "_")
        environment["REDEV_PORT_" + suffix] = str(port)
        environment["REDEV_URL_" + suffix] = (schemes or {}).get(name, "http") + "://localhost:" + str(port)
    return environment


def shell_argv(command_text, source):
    return ["bash", "-lc", "cd -- " + shlex.quote(str(source)) + " || exit\n" + command_text]


def command(command_text, source, environment, lock_descriptor):
    argv = command_text if isinstance(command_text, list) else shell_argv(command_text, source)
    process = subprocess.Popen(
        argv, cwd=source, env=environment,
        stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock_descriptor,),
    )
    try:
        while True:
            try:
                result = process.wait(timeout=ACTIVITY_INTERVAL)
                return result if result >= 0 else 128 - result
            except subprocess.TimeoutExpired:
                print("[redev] Remote command is still running.", flush=True)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # A terminated group leader can leave children that ignore TERM.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def process_details(pid):
    """The start identity prevents a stale record from stopping a reused PID."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        if sys.platform.startswith("linux"):
            fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
            return {"identity": "linux:" + fields[19], "group": int(fields[2]), "zombie": fields[0] == "Z"}
        response = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "stat="],
            capture_output=True, text=True, check=False,
        )
        fields = response.stdout.split()
        if response.returncode or len(fields) < 7:
            return None
        return {"identity": "ps:" + " ".join(fields[:5]), "group": int(fields[5]), "zombie": fields[6].startswith("Z")}
    except (OSError, ValueError, IndexError):
        return None


def service_alive(service):
    details = process_details(service.get("pid"))
    return bool(details and details["identity"] == service.get("identity")
                and details["group"] == service.get("pid") and not details["zombie"])


def process_group_active(group):
    try:
        response = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=False,
        )
    except OSError:
        return True
    if response.returncode:
        return True
    for line in response.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return True
        if fields[0] == str(group) and not fields[1].startswith("Z"):
            return True
    return False


def signal_process_group(group, requested_signal):
    try:
        os.killpg(group, requested_signal)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can deny signaling a group containing only a zombie.
        if process_group_active(group):
            raise
        return False


def stop_services(base, state):
    targets = []
    for service in state.get("services", []):
        details = process_details(service.get("pid"))
        if not details or details["identity"] != service.get("identity") or details["group"] != service.get("pid"):
            continue
        if signal_process_group(service["pid"], signal.SIGTERM):
            targets.append(service)
    deadline = time.monotonic() + 3
    while any(service_alive(service) for service in targets) and time.monotonic() < deadline:
        time.sleep(0.05)
    for service in targets:
        details = process_details(service.get("pid"))
        # The verified group can outlive its leader. A new leader must still match.
        if details is None or (details["identity"] == service.get("identity") and details["group"] == service["pid"]):
            signal_process_group(service["pid"], signal.SIGKILL)
    if targets:
        time.sleep(0.05)
    atomic_json(base, "state.json", state)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def start_services(base, state, config, ports, environment, lock_descriptor):
    state["services"] = []
    atomic_json(base, "state.json", state)
    for settings in config.get("services", []):
        log_path = inspect_path(base, "logs/" + settings["name"] + ".log")
        record = {"name": settings["name"], "log": str(log_path)}
        state["services"].append(record)
        try:
            if settings.get("when"):
                condition_exit = command(settings["when"], base / "source", environment, lock_descriptor)
                record["conditionExit"] = condition_exit
                if condition_exit == 3:
                    record["skipped"] = True
                    atomic_json(base, "state.json", state)
                    continue
                if condition_exit:
                    raise RunnerError("Service condition failed with exit " + str(condition_exit))
            if "port" in settings:
                record["port"] = ports[settings["port"]]
                if port_open(record["port"]):
                    raise RunnerError("Service port is already in use: " + str(record["port"]))
            descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as log:
                process = subprocess.Popen(
                    shell_argv(settings["command"], base / "source"), cwd=base / "source", env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            record["pid"] = process.pid
            details = process_details(process.pid)
            if details:
                record["identity"] = details["identity"]
            atomic_json(base, "state.json", state)
            deadline = time.monotonic() + settings.get("readyTimeout", 60)
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    raise RunnerError("Service " + settings["name"] + " exited with " + str(exit_code))
                if "port" in record and port_open(record["port"]):
                    break
                if "port" not in record:
                    time.sleep(0.1)
                    if process.poll() is None:
                        break
                    continue
                if time.monotonic() >= deadline:
                    raise RunnerError("Service " + settings["name"] + " did not become ready")
                time.sleep(0.05)
            if not service_alive(record):
                raise RunnerError("Cannot verify service identity: " + settings["name"])
        except BaseException as error:
            record["error"] = str(error) or "Service start interrupted"
            atomic_json(base, "state.json", state)
            stop_services(base, state)
            raise RunnerError("Service " + settings["name"] + ": " + record["error"] + "; log: " + str(log_path)) from error
    atomic_json(base, "state.json", state)


def status(state):
    services = []
    for record in state.get("services", []):
        service = dict(record, active=service_alive(record))
        if not service["active"] and not service.get("skipped") and state.get("desiredServices"):
            service.setdefault("error", "Service is not running")
        services.append(service)
    return {"snapshotId": state.get("snapshotId"), "desiredServices": state.get("desiredServices", False), "services": services}


def export_generated(base, config, previous):
    source = base / "source"
    output = base / "generated"
    directory(output)
    manifest = {}
    excluded = config.get("sync", {}).get("exclude", [])
    for prefix in config.get("sync", {}).get("generated", []):
        target = inspect_path(source, prefix)
        if not target.exists():
            continue
        candidates = [target]
        if target.is_dir():
            candidates = []
            for current, directories, files in os.walk(target, followlinks=False):
                for name in directories + files:
                    child = Path(current) / name
                    if child.is_symlink():
                        raise RunnerError("Symlink in generated files: " + str(child))
                candidates.extend(Path(current) / name for name in files)
        for path in candidates:
            relative = path.relative_to(source).as_posix()
            if protected(relative, excluded):
                raise RunnerError("Protected generated file: " + relative)
            entry = {"sha256": file_hash(path), "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644}
            destination = inspect_path(output, relative)
            if destination.exists() and not destination.is_file():
                raise RunnerError("Generated destination collision: " + relative)
            copy_file(path, destination, entry)
            manifest[relative] = entry
    for relative in previous.keys() - manifest.keys():
        relative_path(relative)
        if protected(relative, excluded):
            continue
        destination = inspect_path(output, relative)
        if destination.exists():
            if not destination.is_file():
                raise RunnerError("Generated destination collision: " + relative)
            destination.unlink()
    return manifest


def run_transaction(base, request, state, lock_descriptor):
    metadata = request if isinstance(request, dict) else {}
    result = {"snapshotId": metadata.get("snapshotId"), "transactionId": metadata.get("transactionId"),
              "checkExit": None, "checks": [], "success": False, "generated": {}}
    try:
        config = validate_request(request)
        selected = selected_checks(request)
        changed = state.get("snapshotId") != request["snapshotId"] or state.get("manifest") != request["manifest"]
        setup_manifest = {
            relative: entry for relative, entry in request["manifest"].items()
            if any(within_prefix(relative, prefix) for prefix in config.get("setupInputs", []))
        }
        digest = hashlib.sha256(json.dumps([
            SETUP_EXECUTION_VERSION, config, setup_manifest, request.get("ports", {})
        ], sort_keys=True).encode()).hexdigest()
        setup_changed = state.get("setupDigest") != digest
        state["desiredServices"] = (not request.get("checkOnly", False)
                                    and (state.get("desiredServices", False) or request.get("startServices", False)))
        prepared = state.get("preparedSnapshotId") == request["snapshotId"]
        desired_alive = not state["desiredServices"] or (
            {service.get("name") for service in state.get("services", [])} == {service["name"] for service in config.get("services", [])}
            and all(service.get("skipped") or service_alive(service) for service in state.get("services", []))
        )
        if (not changed and not setup_changed and prepared and state.get("transactionReady")
                and not selected and desired_alive and not request.get("startServices", False)):
            result["generated"] = state.get("generated", {})
            result["success"] = True
            clean_incoming(base, request)
            atomic_json(base, "state.json", state)
            atomic_json(base, "result.json", result)
            return 0
        live_sync = (config.get("sync", {}).get("mode") == "live" and state["desiredServices"]
                     and desired_alive and state.get("transactionReady") and not setup_changed
                     and not selected and not request.get("startServices", False))
        if live_sync:
            update_source(base, request, state.get("manifest", {}))
            seed_generated(base, request)
            state.update(manifest=request["manifest"], snapshotId=request["snapshotId"],
                         preparedSnapshotId=request["snapshotId"])
            result["generated"] = export_generated(base, config, state.get("generated", {}))
            state["generated"] = result["generated"]
            result["success"] = True
            clean_incoming(base, request)
            atomic_json(base, "state.json", state)
            atomic_json(base, "result.json", result)
            return 0
        state["transactionReady"] = False
        stop_services(base, state)
        update_source(base, request, state.get("manifest", {}))
        seed_generated(base, request)
        state["manifest"] = request["manifest"]
        state["snapshotId"] = request["snapshotId"]
        atomic_json(base, "state.json", state)
        environment = command_environment(request.get("ports", {}), config.get("portSchemes", {}))
        if setup_changed:
            exit_code = command(config.get("setup", ""), base / "source", environment, lock_descriptor)
            if exit_code:
                raise RunnerError("setup failed with exit " + str(exit_code))
            state["setupDigest"] = digest
            atomic_json(base, "state.json", state)
        if changed or setup_changed or not prepared or selected:
            state.pop("preparedSnapshotId", None)
            atomic_json(base, "state.json", state)
            if config.get("prepare"):
                exit_code = command(config["prepare"], base / "source", environment, lock_descriptor)
                if exit_code:
                    raise RunnerError("prepare failed with exit " + str(exit_code))
            state["preparedSnapshotId"] = request["snapshotId"]
            atomic_json(base, "state.json", state)
        if selected:
            result["checkExit"] = 0
            for name in selected:
                argv, cwd = check_invocation(config["checks"][name], request.get("checkArgs", []))
                source = base / "source"
                working_directory = source if cwd == "." else inspect_path(source, cwd, allow_missing=False)
                record = {"name": name, "argv": argv, "cwd": cwd, "exit": None, "startedAt": time.time()}
                result["checks"].append(record)
                atomic_json(base, "result.json", result)
                if "checks" in request:
                    verify_source(source, request["manifest"])
                    print("[redev] Running check: " + name, flush=True)
                invocation = config["checks"][name] if isinstance(config["checks"][name], str) else argv
                record["exit"] = command(invocation, working_directory, environment, lock_descriptor)
                record["finishedAt"] = time.time()
                result["checkExit"] = result["checkExit"] or record["exit"]
                atomic_json(base, "result.json", result)
                if "checks" in request:
                    verify_source(source, request["manifest"])
        if state["desiredServices"] and config.get("servicePrepare"):
            exit_code = command(config["servicePrepare"], base / "source", environment, lock_descriptor)
            if exit_code:
                raise RunnerError("servicePrepare failed with exit " + str(exit_code))
        result["generated"] = export_generated(base, config, state.get("generated", {}))
        state["generated"] = result["generated"]
        atomic_json(base, "state.json", state)
        if state["desiredServices"]:
            start_services(base, state, config, request.get("ports", {}), environment, lock_descriptor)
        result["success"] = result["checkExit"] in (None, 0)
        if result["success"]:
            clean_incoming(base, request)
        state["transactionReady"] = True
        atomic_json(base, "state.json", state)
        atomic_json(base, "result.json", result)
        return result["checkExit"] or 0
    except (Exception, KeyboardInterrupt) as error:
        state["transactionReady"] = False
        result["success"] = False
        stop_services(base, state)
        result["error"] = str(error) or "Transaction interrupted"
        atomic_json(base, "result.json", result)
        print(result["error"], file=sys.stderr)
        return FAILURE


def verify_source(source, manifest):
    for relative, entry in manifest.items():
        path = inspect_path(source, relative, allow_missing=False)
        if not path.is_file() or file_hash(path) != entry["sha256"] or stat.S_IMODE(path.stat().st_mode) != entry["mode"]:
            raise RunnerError("Check changed source in the requested snapshot: " + relative)


def interrupted(signum, frame):
    raise RunnerError("Transaction interrupted by signal " + str(signum))


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("run", "status", "stop"):
        print("Usage: runner.py BASE run|status|stop", file=sys.stderr)
        return FAILURE
    base = Path(sys.argv[1])
    if not base.is_absolute() or base == Path("/"):
        raise RunnerError("Base directory must be an absolute private directory")
    directory(base)
    for name in ("source", "incoming", "logs"):
        directory(inspect_path(base, name))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    lock_path = inspect_path(base, "transaction.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        action = sys.argv[2]
        request = None
        if action == "run":
            try:
                request = json.load(sys.stdin)
                state = read_state(base)
                return run_transaction(base, request, state, descriptor)
            except (Exception, KeyboardInterrupt) as error:
                metadata = request if isinstance(request, dict) else {}
                atomic_json(base, "result.json", {
                    "snapshotId": metadata.get("snapshotId"), "transactionId": metadata.get("transactionId"),
                    "checkExit": None, "success": False, "generated": {},
                    "error": str(error) or "Transaction interrupted",
                })
                raise
        state = read_state(base)
        if action == "stop":
            state["desiredServices"] = False
            stop_services(base, state)
        print(json.dumps(status(state)))
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(str(error) or "Interrupted", file=sys.stderr)
        sys.exit(FAILURE)
