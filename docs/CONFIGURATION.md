# Configuration and operation

The extension reads JSON or JSONC from `.devcontainer/devcontainer.json` and validates only `customizations.redev`. Other tools use their own namespaces and the standard devcontainer fields. Unknown extension settings fail validation. Configuration contains shell commands: use configuration from repositories you trust.

A small generic example:

```json
{
  "image": "mcr.microsoft.com/devcontainers/python:1-3.12-bookworm",
  "features": { "ghcr.io/devcontainers/features/sshd:1": {} },
  "customizations": {
    "redev": {
      "version": 1,
      "setup": "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
      "setupInputs": ["requirements.txt"],
      "checks": {
        "types": ".venv/bin/python -m mypy src",
        "unit": { "argv": [".venv/bin/python", "-m", "pytest"], "cwd": "." }
      },
      "services": [
        { "name": "web", "command": ".venv/bin/python -m http.server $REDEV_PORT_WEB --bind 127.0.0.1", "port": "web", "readyTimeout": 30 }
      ],
      "ports": { "web": 8080 },
      "sync": { "exclude": ["private-data"], "generated": ["generated/client"] },
      "codespace": { "minimumCpus": 2, "minimumMemoryGb": 8, "idleTimeout": "30m" }
    }
  }
}
```

Ensure rsync is present in the selected image or install it through its normal build/setup recipe. This example assumes a Python repository whose `requirements.txt` includes mypy and pytest. Replace the commands to suit the repository.

| Field | Contract |
| --- | --- |
| `version` | Required integer `1`. |
| `setup` | Optional nonempty Bash command, run in the synchronized remote source directory. |
| `setupInputs` | Path prefixes that affect setup, such as dependency lockfiles and package manifests. Include all relevant manifests and patches. |
| `prepare` | Optional bounded Bash command for check prerequisites. It runs before selected checks and during full source transactions that need preparation. Live sync skips it. |
| `servicePrepare` | Optional bounded Bash command when services are desired, after selected checks and before generated export/service startup. Use for service initialization that validation must omit. Live sync skips it. |
| `checks` | Required nonempty map from names to Bash command strings or objects containing `argv` and optional `cwd`. `argv` is a nonempty array of strings. `cwd` is `.` or a relative source directory. |
| `services` | Ordered list with unique `name`, Bash `command`, optional Bash `when`, optional named `port`, and optional `readyTimeout` of 1–600 seconds. |
| `ports` | Unique names and preferred integer ports from 1024–65535. Names use lowercase letters, digits, `_`, or `-`, starting with a letter. Names must remain unique after uppercase and dash-to-underscore conversion. |
| `portSchemes` | Optional map from configured port names to `http` or `https`. Defaults to `http`. It sets printed and injected URLs; the service must supply TLS for HTTPS. |
| `sync.exclude` | Additional excluded relative path prefixes. These are literal prefixes, not glob patterns. |
| `sync.generated` | Relative path prefixes permitted on interactive generated-source return. Excluded from ordinary interactive upload; tracked files are inputs in validation mode. |
| `sync.seedGenerated` | Optional boolean, default `false`. In interactive mode, send tracked files under `sync.generated` as initial seeds. Create each remote file only if missing; preserve existing remote files. |
| `sync.mode` | `restart` (default) or `live`. Live mode keeps healthy service watchers running for ordinary source updates when setup inputs are unchanged. Checks and setup changes still use a full stop/update/start transaction. |
| `codespace` | Optional creation settings: `machine`, `branch`, `idleTimeout`, `minimumCpus`, `minimumMemoryGb`. Default minimum is 2 CPUs/8 GiB. The CLI selects the smallest allowed machine meeting the minima unless a machine is specified. `idleTimeout` is 5–240 minutes, written as minutes or whole hours; default `30m`. |

Setup reruns when configuration, assigned ports, or content under `setupInputs` changes. Include setup scripts themselves in `setupInputs`. Validation and interactive workspaces have separate setup state and dependencies. Image changes need a rebuild. Keep `setup`, `prepare`, and checks non-deploying for validation. Put service-specific deployment or initialization in `servicePrepare`, and long-running processes in `services`. All hooks must finish without interactive input.

## Selected checks and workspace modes

```sh
gh redev check types unit --stop
gh redev check unit --stop -- -k test_name
gh redev check types --stop --codespace EXISTING_NAME
```

Choose distinct names from `checks`; the batch runs in the given order against one source snapshot. A failed command does not skip later selected checks. The first nonzero check exit is the batch result. A source-integrity or infrastructure error stops the transaction. Arguments after `--` require exactly one structured `argv` check. They are appended as literal arguments, without shell expansion. For a different shell command or a batch with different options, add named checks to the configuration.

`check --stop` selects `/workspaces/.redev/<worktree-id>/validation/source`. It stops any local watcher and interactive services, runs setup/preparation and selected checks, then attempts to stop the Codespace. It omits `services`, `servicePrepare`, port allocation, and generated return. Tracked generated files are included as source inputs. A check does not verify that those generated files are fresh; provide a non-deploying check when that is required.

`up` selects `/workspaces/.redev/<worktree-id>/source` for interactive testing. It starts desired services, automatic source sync, and private forwarding. Leave this mode running until testing ends. A check without `--stop` uses the current mode, or validation mode for a new mapping, and leaves the Codespace running. In interactive mode, a check restarts desired services after it completes. `--stop` always selects validation mode and ends the interactive session.

Both `check` and `up` can create or reuse a Codespace. Their `--codespace`, `--replace`, `--branch`, and `--machine` options have the same mapping/creation meaning. Branch and machine overrides affect creation only. The configured idle timeout also applies only at creation; the CLI does not update it on an existing Codespace.

## Service environment and browser URLs

Commands run in the selected source directory, separate from the Codespace's initial Git checkout. They inherit the remote environment and receive `REDEV_REMOTE=1`. Interactive mode also supplies variables for each configured port:

```text
REDEV_REMOTE=1
REDEV_PORT_WEB=8080
REDEV_URL_WEB=http://localhost:8080
```

Port names become uppercase, with dashes replaced by underscores, in `REDEV_PORT_<NAME>` and `REDEV_URL_<NAME>`. The URL uses `portSchemes` when supplied. Service adapters must use the assigned port numbers, including API and HTTP action listeners. Browser URLs, server-side URLs, allowed origins, content security policies, and authentication redirects must agree. Private forwarding uses `gh codespace ports forward`; it does not enable TLS or change public port visibility.

A named readiness port is a TCP startup check, not an application health check. Configure the service process to stay alive. At each service start, an optional `when` command runs first: exit `0` starts the service, `3` skips it, and every other nonzero exit fails startup and stops the service group. Status identifies a skipped service explicitly. `when` must finish and should explain a skip without printing secrets. Live sync does not re-evaluate conditions; use `up` to retry service selection after changing private configuration.

In default `restart` mode, service groups stop before source updates and checks, then desired services restart. With `sync.mode: "live"`, ordinary edits reach existing watchers without restarting services or running either preparation hook. A setup-input/configuration change, an explicit `up`, or a check uses the full transaction. Select live mode only when the services' own watchers can handle those edits. Checks are serialized for a worktree; separate worktrees have separate Codespaces.

## Remote secrets

Use native personal Codespaces secrets and grant access to the repository. They are supplied by GitHub to the remote environment; project commands and adapters decide which values they use. A private file created inside the Codespace is also available if the project explicitly supports it. Restart an existing Codespace as required after changing its secrets.

Environment-file synchronization is not implemented. Local `.env` files, provider credentials, and database state remain excluded from source transfer, including generated seeds. There is no opt-in flag to copy them. The CLI does not read or print secret values for setup. Commands can print their own environment, so keep secret values out of their output and private logs.

## Source and generated files

Git's inventory supplies tracked files plus nonignored untracked files. A tracked file deleted locally is absent from the manifest. Ignored untracked source is not sent. Git submodules and symlink source entries are not supported; a source symlink fails instead of following it. Symlinks in protected directories are excluded. The source copy uses file hashes and a second local scan; if source keeps changing, snapshot creation fails after three attempts.

Built-in protection excludes Git metadata, environment files other than `.env.example`, credential data, hidden credential directories, key files, dependencies, caches, build output, local databases, and local coding-agent configuration. A visible directory named `credentials` can contain application source: files with recognized source extensions such as `.ts`, `.tsx`, `.js`, `.py`, and `.go` can sync. Extension matching is case-insensitive. Bare credential files and data files such as JSON, YAML, INI, and text stay excluded there. Nested protected paths and explicit `sync.exclude` entries still take priority. This rule also applies to generated seeds and returned source.

See `CREDENTIAL_SOURCE_EXTENSIONS` and the other path rules in `snapshot.py` for the complete lists. Built-in exclusions apply even to tracked files and cannot be disabled by repository settings. They are path rules, not a secret scanner: do not commit secrets inside ordinary source files.

Remote deletions are restricted to paths in the previous managed source manifest. Unmanaged file collisions and symlink paths fail. File/directory replacements are allowed only when every removed entry belonged to the previous manifest; unmanaged files and directories are preserved. Database directories and dependencies are never ordinary sync targets. Remote edits to managed source are not authoritative and can be replaced by the next local snapshot; do not edit that source remotely.

In interactive mode, only files under `sync.generated` can return. The runner exports a separate snapshot before starting services; live sync also exports declared output. The local CLI verifies hashes and compares generated files with their pre-run local content. Conflicts preserve local edits and retain downloaded output under the private state directory, with its path in the error. The return path does not delete local files. Files removed by a generator may need explicit local removal. External editors do not participate in CLI locks; avoid editing generated files during the application step.

`sync.seedGenerated: true` supplies tracked generated files before initial preparation in interactive mode. Each seed is copied only if that remote file is missing. An existing remote generated file is preserved even when its local tracked version changes. Untracked generated files are not seeds, and protected paths/symlinks are rejected. Seeds do not join the ordinary managed-source manifest. Validation mode instead treats tracked generated files as regular source inputs, regardless of this setting, and never copies generated output back.

## State, recovery, and stopping

Local state is stored with private permissions under `${XDG_STATE_HOME:-$HOME/.local/state}/redev/worktrees/<id>`. The ID is the first 32 hexadecimal characters of the SHA-256 of the real worktree root path. State includes the owning GitHub login, Codespace name, assigned ports, sync result, service status, and worker process identity. Tokens remain in `gh`'s credential storage. Moving a worktree creates a new identity; stop it at its old path first.

`up` and `check` write a creation record before contacting GitHub. A new display name starts with the pull request number when found, otherwise a sanitized branch label, followed by `redev-<worktree-id>`. This marker supports recovery; the stored provider name remains the identity after a display-name change. An uncertain creation response is not retried blindly. Inspect `gh codespace list`, then use `check NAME --codespace SPACE` or `up --codespace SPACE` to bind the correct environment. Use `--replace` only after resolving the missing or uncertain mapping. Replacement creates new state; it cannot recover a deleted database. One local mapping cannot reuse a Codespace already assigned to another local worktree.

`status --json` reports mapping, provider state, last sync/check, URLs, and watcher errors. Available provider metadata includes display name, payer, machine, idle timeout, retention period, and retention expiry. Missing metadata is not evidence of zero cost or unlimited retention. Status does not connect to or resume a Codespace. Setup/service readiness is the last observation, with `checkedLive: false`; provider `Available` does not prove the application is ready. Skipped services are distinct from running services.

The first SSH connection for a remote operation allows up to 180 seconds for Codespace startup and the SSH handshake. Later connections and file transfers use a 30-second connection timeout. SSH errors remain visible even when the provider's generated configuration requests quiet logging.

Each check prints a private `runs/<run-id>/result.json` path. The record includes selected names, literal argv, working directories, individual exits, the snapshot ID, timestamps, the overall/remote exit, stale state, the output log path, and `stop.requested`/`stop.confirmed`. A requested stop can fail; inspect its error instead of claiming shutdown. Standard output and errors are streamed and saved in `output.log`. Interactive service records include remote log paths; the local watcher uses `worker.log`. Failed staging directories may remain for inspection.

After a watcher or forwarding error, use `stop` then `up`. For a validation failure, fix source locally and rerun the selected `check ... --stop`; transport failures never invoke a local replacement. If the source changed during a check, its result is stale: a passing batch returns `75`, while a failed batch keeps its failure exit. Current success requires a fresh run after edits settle.

`stop` waits for provider shutdown confirmation and preserves the mapping, opt-in, and workspace data. `disable` opts out only after a successful stop. There is no delete command or automatic deletion when a pull request closes. Stopped Codespaces can still incur storage costs and are subject to provider retention. Read the actual payer and retention metadata from status. Deletion or retention expiry can remove development data; the CLI does not back up databases or change billing/retention settings.

## Provider references

- [Official GitHub CLI extension installation](https://cli.github.com/manual/gh_extension_install)
- [Codespace creation](https://cli.github.com/manual/gh_codespace_create)
- [SSH configuration and rsync integration](https://cli.github.com/manual/gh_codespace_ssh)
- [One-time copying](https://cli.github.com/manual/gh_codespace_cp)
- [Port forwarding](https://cli.github.com/manual/gh_codespace_ports_forward)
- [Devcontainer customization schema](https://github.com/devcontainers/spec/blob/main/schemas/devContainer.base.schema.json)
