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
      "checks": { "types": ".venv/bin/python -m unittest discover" },
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

Ensure rsync is present in the selected image or install it through its normal build/setup recipe. This example assumes a repository with a `requirements.txt` file; replace the commands to suit the repository.

| Field | Contract |
| --- | --- |
| `version` | Required integer `1`. |
| `setup` | Optional nonempty Bash command, run in the synchronized remote source directory. |
| `setupInputs` | Path prefixes that affect setup, such as dependency lockfiles and package manifests. Include all relevant manifests and patches. |
| `prepare` | Optional Bash command before checks and after changed source. Use for bounded code generation and prerequisites. |
| `checks` | Required nonempty map from names to Bash commands. Bound concurrency inside commands; the extension runs one check transaction at a time. |
| `services` | Ordered list with unique `name`, `command`, optional named `port`, and optional `readyTimeout` of 1–600 seconds. |
| `ports` | Unique names and preferred integer ports from 1024–65535. Names use lowercase letters, digits, `_`, or `-`, starting with a letter. Names must remain unique after uppercase and dash-to-underscore conversion. |
| `sync.exclude` | Additional excluded relative path prefixes. These are literal prefixes, not glob patterns. |
| `sync.generated` | Relative path prefixes permitted on the generated-source return path. They are excluded from ordinary upload. |
| `codespace` | Optional `machine`, `branch`, `idleTimeout`, `minimumCpus`, `minimumMemoryGb`. Default minimum machine is 2 CPUs/8 GiB. The CLI selects the smallest allowed machine meeting those minima unless `machine` or `--machine` is specified. |

Setup reruns when configuration, assigned ports, or the content under `setupInputs` changes. Include setup scripts themselves in `setupInputs`. Image changes need a rebuild. Long-running service processes must not be started in `setup` or `prepare`: those commands must finish. Use `services` for them.

## Service environment and browser URLs

Commands run in `/workspaces/.redev/<worktree-id>/source`, separate from the Codespace's initial Git checkout. They inherit the remote environment and receive:

```text
REDEV_REMOTE=1
REDEV_PORT_WEB=8080
REDEV_URL_WEB=http://localhost:8080
```

Each configured port creates the matching `REDEV_PORT_<NAME>` and `REDEV_URL_<NAME>` variables. Service adapters must use the assigned port numbers, including backend and HTTP action listeners. Browser-visible URLs, server-side API URLs, allowed origins, and authentication redirects must agree. Private local forwarding is managed by `gh codespace ports forward`; the extension does not change public port visibility.

A named readiness port is a TCP startup check. Configure services so the process stays alive; the extension starts them in order and logs them separately. It stops all service process groups before changing source or running checks, then restarts desired services. This initial version trades uninterrupted browser sessions for a stable check input. Clients may briefly reconnect during edits. Checks run sequentially for this worktree; separate worktrees have separate Codespaces.

## Source and generated files

Git's inventory supplies tracked files plus nonignored untracked files. A tracked file deleted locally is absent from the manifest. Ignored untracked source is not sent. Git submodules and symlink source entries are not supported; a source symlink fails instead of following it. Symlinks in protected directories are excluded. The source copy uses file hashes and a second local scan; if source keeps changing, snapshot creation fails after three attempts.

Built-in protection excludes Git metadata, environment files other than `.env.example`, credential files and directories, key files, dependencies, caches, build output, local databases such as `.convex`, and local coding-agent configuration. See `snapshot.py` for the complete list. These exclusions apply even to tracked files and cannot be disabled by repository settings. They are a boundary rule, not a secret scanner: do not commit secrets inside ordinary source files. Use remote provider secrets or a private remote setup file. No local environment files are uploaded automatically.

Remote deletions are restricted to paths in the previous managed source manifest. Unmanaged file collisions and symlink paths fail. File/directory replacements are allowed only when every removed entry belonged to the previous manifest; unmanaged files and directories are preserved. Database directories and dependencies are never ordinary sync targets. Remote edits to managed source are not authoritative and can be replaced by the next local snapshot; do not edit that source remotely.

A `prepare` or check command can generate files only under the configured return prefixes. The runner exports a separate snapshot before restarting service watchers. The local CLI verifies hashes and compares generated files with their pre-run local content. Conflicts preserve local edits and retain downloaded output under the private state directory, with its path in the error. The return path does not delete local files. Files removed by a generator may need explicit local removal. The extension serializes its own operations; external editors do not participate in its locks. Do not edit generated files during the short application step.

## State, recovery, and stopping

Local state is stored with private permissions under `${XDG_STATE_HOME:-$HOME/.local/state}/redev/worktrees/<id>`. The ID is the first 32 hexadecimal characters of the SHA-256 of the real worktree root path. State includes the owning GitHub login, Codespace name, assigned ports, sync result, service status, and worker process identity. Tokens remain in `gh`'s credential storage. Moving a worktree creates a new identity; stop it at its old path first.

`up` writes a creation record before contacting GitHub. It can recover a returned Codespace by its deterministic display name. An uncertain creation response is not retried blindly. Inspect `gh codespace list`, then use `up --codespace NAME` to bind the correct environment, or `up --replace` after confirming no usable environment exists. A missing mapped Codespace also requires explicit replacement. Replacement creates new development state; it cannot recover a deleted database. One local mapping cannot reuse a Codespace already assigned to another local worktree.

`status --json` reports mapping, provider state, last sync/check, service state, URLs, and local watcher errors. It does not resume a stopped Codespace. Service records include remote log paths. Local background logs are `worker.log` in the private state directory. Source snapshots with failed commands may remain under the private remote directory for inspection; remove old failed staging directories only after stopping operations.

After a watcher or forwarding error, use `stop` then `up`. A failed `check` does not run a local replacement command. Stopping the Codespace preserves the `/workspaces` data directory; deletion, retention expiry, or loss of the Codespace does not. Review GitHub's retention policy and export important development data. Files outside the persistent workspace can be lost during rebuild. The extension does not automatically back up databases or modify billing rules.

## Provider references

- [Official GitHub CLI extension installation](https://cli.github.com/manual/gh_extension_install)
- [Codespace creation](https://cli.github.com/manual/gh_codespace_create)
- [SSH configuration and rsync integration](https://cli.github.com/manual/gh_codespace_ssh)
- [One-time copying](https://cli.github.com/manual/gh_codespace_cp)
- [Port forwarding](https://cli.github.com/manual/gh_codespace_ports_forward)
- [Devcontainer customization schema](https://github.com/devcontainers/spec/blob/main/schemas/devContainer.base.schema.json)
