# redev

Keep your editor, source files, and coding agents local. Run configured checks and development services in a GitHub Codespace dedicated to each local Git worktree.

This is an independent GitHub CLI extension. Repositories supply their own commands and environment recipe in `.devcontainer/devcontainer.json`. The extension has no application dependencies. It is a local initial release; it has not been published to a remote repository or package registry.

## Install from a local checkout

Requirements: macOS or Linux, Python 3.11 or later, the official `gh` CLI, Git, OpenSSH, and rsync. Authenticate with `gh auth login`; Codespaces access and an allowed machine are required. The remote devcontainer must provide Python 3.11+, rsync, Bash, and an SSH server, plus the repository's development tools.

```sh
CLI_DIR=/absolute/path/to/redev
cd "$CLI_DIR"
ln -sfn "$PWD" ../gh-redev
(cd ../gh-redev && gh extension install .)
gh redev --help
```

GitHub CLI requires a local extension source directory name with a `gh-` prefix. Keep the project folder named `redev` and keep the `../gh-redev` symlink while the extension is installed. If a project selects an older `python3`, the launcher also checks versioned Python 3.11–3.14 executables on `PATH`. Set `REDEV_PYTHON` to select another compatible executable. The executable and Python modules must remain together. A local extension installation refers to this checkout. No pip or npm install is required. Run `gh extension remove redev` to remove the local CLI registration after stopping active environments.

## Use

From an opted-in repository worktree:

```sh
gh redev up
gh redev check types
gh redev status
gh redev stop
```

`types` is an example configuration key. Use the keys from your repository's `checks` object. `up` opts in, creates or reuses the Codespace, applies current source, starts configured services, and starts one local watcher plus private loopback port forwarding. It can create a billable environment. `check` resumes an existing mapping and synchronizes a verified snapshot before running a command; it never creates a new environment on its own.

`stop` stops the watcher, forwarding, and Codespace. It preserves remote development data and opt-in. `disable` removes opt-in after a successful stop. The extension has no delete command.

Useful controls:

```sh
gh redev up --port web=13040
gh redev up --branch remote-development-recipe
gh redev up --no-watch
gh redev sync
gh redev status --json
gh redev enabled
```

Each named port uses the same number locally and remotely. This makes browser and server-side localhost URLs consistent. `--no-watch` starts remote services but omits the local watcher and forwarding; use `sync` manually. Port names must exist in the repository configuration. Initial automatic allocation avoids ports reserved by other local worktrees. Existing assignments stay stable; if another application takes a saved port, select a new number explicitly.

The devcontainer recipe must exist on a published branch before GitHub can build it. `--branch` selects that branch only for initial creation. Source editing and subsequent checks use local snapshots and do not require commits or pushes. Changes to the container image/features require a Codespaces rebuild; source sync cannot rebuild a container.

## What a check means

A check receives one staged source snapshot containing tracked and nonignored untracked files, including local modifications and deletions. Local and remote locks serialize sync and checks. Service watchers stop during the transaction. The next remote source transfer uses a distinct staging directory and a verified manifest, so a lost connection cannot let a retry overwrite a running check's input. rsync uses the existing remote source as a copy basis for incremental transfer; it never receives a broad `--delete` option.

The check streams stdout/stderr and preserves its exit code. If local source changes while it runs, the result is marked stale. A successful stale check returns `75`; a failed stale check retains its failure code. The private `lastCheck.remoteExit` records the actual remote code. Retry after edits settle before reporting current success. Configuration, setup, sync, and connection errors return `70`. Invalid command syntax returns `2`; an interrupted local call returns `130`. `enabled` returns `0` for opt-in, `3` for absent/disabled, or an error code if private state cannot be read. A configured check can also return these numbers; use the error text and `lastCheck` to distinguish them.

Only the extension's commands run remotely. A repository can supply adapters for selected scripts. Direct `tsc`, `npx tsc`, and other commands remain local. The CLI and included skill never silently fall back to a local heavy check.

See [configuration](docs/CONFIGURATION.md) for the schema, service contract, source rules, generated output, and diagnostics.

## Agent skill

The generic [redev skill](skills/redev/SKILL.md) teaches the actual CLI workflow. Install it for either local coding agent, preserving an existing skill folder with the same name:

```sh
CLI_DIR=/absolute/path/to/redev
mkdir -p "$HOME/.agents/skills"
ln -s "$CLI_DIR/skills/redev" "$HOME/.agents/skills/redev"
mkdir -p "$HOME/.claude/skills"
ln -s "$CLI_DIR/skills/redev" "$HOME/.claude/skills/redev"
```

The first location is for Codex; the second is for Claude Code. Start a new agent session if the skill is not listed. Invoke `$redev` in Codex or `/redev` in Claude Code. The skill supports the local workflow; it installs no coding agent in a Codespace. Discovery details: [Codex skills](https://learn.chatgpt.com/docs/build-skills), [Claude Code skills](https://code.claude.com/docs/en/skills).

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q redev
```

Tests use temporary repositories and local processes. Loopback service and watcher tests need permission to open local sockets and inspect child processes. Provider fixtures do not make GitHub requests. They execute the real remote runner, actual source updates, command processes, and local rsync.

Live Codespace creation, remote image build, provider forwarding, and application authentication need a separate opt-in smoke test. The local tests do not prove those provider operations. No live Codespace was created as part of initial implementation.

MIT licensed. See [LICENSE](LICENSE).
