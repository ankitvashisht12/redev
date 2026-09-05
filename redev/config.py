"""Read and validate this extension's devcontainer customization."""
import json
from pathlib import PurePosixPath
import re


class ConfigError(ValueError):
    pass


def decode_jsonc(text):
    output = []
    position = 0
    in_string = False
    escaped = False
    while position < len(text):
        char = text[position]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            position += 1
        elif char == '"':
            in_string = True
            output.append(char)
            position += 1
        elif text.startswith('//', position):
            end = text.find('\n', position)
            position = len(text) if end < 0 else end
        elif text.startswith('/*', position):
            end = text.find('*/', position + 2)
            if end < 0:
                raise ConfigError('Unclosed JSONC comment')
            output.append(' ')
            position = end + 2
        else:
            output.append(char)
            position += 1
    text = ''.join(output)
    # Strings are matched first, so commas inside commands are unchanged.
    text = re.sub(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
                  lambda match: match[1] if match[1] else match[0], text)
    return json.loads(text)


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ConfigError(f'Invalid relative path: {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ConfigError(f'Path must stay inside the source tree: {value!r}')
    return value


def named(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', value)


def known_fields(value, allowed, label):
    if not isinstance(value, dict):
        raise ConfigError(f'{label} must be an object')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f'Unknown {label} settings: {", ".join(sorted(unknown))}')


def validate_config(config):
    known_fields(config, ['version', 'setup', 'setupInputs', 'prepare', 'checks', 'services', 'ports', 'sync', 'codespace'], 'redev')
    if type(config.get('version')) is not int or config['version'] != 1:
        raise ConfigError('redev.version must be 1')
    for key in ('setup', 'prepare'):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise ConfigError(f'{key} must be a nonempty shell command')
    checks = config.get('checks')
    if not isinstance(checks, dict) or not checks:
        raise ConfigError('checks must contain at least one named command')
    for name, command in checks.items():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9:_-]*', name) or not isinstance(command, str) or not command.strip():
            raise ConfigError('Each check needs a valid name and shell command')
    ports = config.setdefault('ports', {})
    if not isinstance(ports, dict) or any(not named(name) or type(port) is not int or not 1024 <= port <= 65535 for name, port in ports.items()):
        raise ConfigError('ports must map names to integers from 1024 to 65535')
    if len({name.upper().replace('-', '_') for name in ports}) != len(ports):
        raise ConfigError('Named ports must produce distinct environment variable names')
    if len(set(ports.values())) != len(ports):
        raise ConfigError('Each named port must have a distinct number')
    services = config.setdefault('services', [])
    if not isinstance(services, list):
        raise ConfigError('services must be an array')
    names = set()
    for service in services:
        known_fields(service, ['name', 'command', 'port', 'readyTimeout'], 'service')
        name = service.get('name')
        if not named(name) or name in names:
            raise ConfigError('Services need unique names')
        names.add(name)
        if not isinstance(service.get('command'), str) or not service['command'].strip():
            raise ConfigError(f'Service {name} needs a command')
        if 'port' in service and service['port'] not in ports:
            raise ConfigError(f'Service {name} refers to an unknown port')
        if type(service.get('readyTimeout', 60)) not in (int, float) or not 1 <= service.get('readyTimeout', 60) <= 600:
            raise ConfigError('readyTimeout must be between 1 and 600 seconds')
    sync = config.setdefault('sync', {})
    known_fields(sync, ['exclude', 'generated'], 'sync')
    for paths in [config.setdefault('setupInputs', []), sync.setdefault('exclude', []), sync.setdefault('generated', [])]:
        if not isinstance(paths, list):
            raise ConfigError('Path lists must be arrays')
        for path in paths:
            relative_path(path)
    codespace = config.setdefault('codespace', {})
    known_fields(codespace, ['machine', 'branch', 'idleTimeout', 'minimumCpus', 'minimumMemoryGb'], 'codespace')
    for key in ('machine', 'branch', 'idleTimeout'):
        if key in codespace and (not isinstance(codespace[key], str) or not codespace[key].strip() or codespace[key].startswith('-')):
            raise ConfigError(f'codespace.{key} must be a nonempty value')
    for key in ('minimumCpus', 'minimumMemoryGb'):
        if key in codespace and (type(codespace[key]) is not int or codespace[key] < 1):
            raise ConfigError(f'codespace.{key} must be a positive integer')
    return config


def read_config(root):
    path = root / '.devcontainer/devcontainer.json'
    try:
        data = decode_jsonc(path.read_text())
        return validate_config(data['customizations']['redev'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f'{path}: {error}') from error
