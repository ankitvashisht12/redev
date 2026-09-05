# redev

Keep your editor, source files, and coding agents local. Run configured checks and development services in a GitHub Codespace dedicated to each local Git worktree.

This is an independent GitHub CLI extension. Repositories supply their own commands and environment recipe in `.devcontainer/devcontainer.json`. The extension has no application dependencies. Source is available at [ankitvashisht12/redev](https://github.com/ankitvashisht12/redev); installation below uses a local checkout.

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

## Validate selected changes

From a repository worktree with `customizations.redev` configured:

```sh
gh redev check types unit --stop
gh redev status --json
```

`types` and `unit` are example keys. Select the checks that apply to your change from the repository's `checks` object. They run in order against one verified source snapshot. Each check result is recorded; an ordinary check failure does not prevent the remaining selected checks from running. The batch returns its first nonzero check exit code.

`check --stop` creates or reuses this worktree's Codespace, stops any interactive session, and uses a separate validation source directory. It runs `setup`, `prepare`, and the selected checks. It does not start configured services, run `servicePrepare`, or allocate browser ports. Tracked generated files are validation inputs; no generated files return to the local worktree. Keep deployment and service initialization commands out of validation hooks.

After success, failure, or a handled interruption, `--stop` attempts to stop the Codespace and records whether GitHub confirmed shutdown. Read the result before reporting completion. For a failure, fix source locally and repeat the selected check; each retry syncs the new source. No local heavy check runs as a fallback.

Both `check` and `up` can create a billable environment. Use an existing Codespace with `--codespace NAME`, or supply `--branch BRANCH` when the published container recipe is on a specific branch. Existing mappings are reused.

One structured `argv` check can receive test runner arguments after `--`:

```sh
gh redev check unit --stop -- -k test_name
```

Arguments are passed literally. This form requires one check defined with an `argv` array; it is not available for a batch or a shell command string.

## Keep services running for testing

```sh
gh redev up
gh redev status
# Edit locally and test through the printed URLs.
gh redev stop
```

`up` syncs source, runs the configured service preparation, starts services in order, and starts a local watcher plus private loopback forwarding. Keep the environment running during interactive testing. Use `stop` when testing ends. `stop` stops the watcher, forwarding, and Codespace; it keeps data and opt-in. `disable` removes opt-in after a successful stop. The extension has no delete command and does not delete an environment when a pull request closes.

`check` without `--stop` keeps the current workspace mode. After `up`, it uses the interactive source and restarts desired services after the checks. After a validation run, it keeps using the validation source and leaves the Codespace running. Use `--stop` when you want the complete validation-and-shutdown workflow.

Useful controls:

```sh
gh redev up --port web=13040
gh redev up --branch remote-development-recipe
gh redev up --no-watch
gh redev sync
gh redev status --json
gh redev enabled
```

Each named port uses the same number locally and remotely. This makes browser and server-side localhost URLs consistent. `portSchemes` can select HTTPS for a service that already supplies TLS; forwarding does not create certificates. `--no-watch` starts remote services but omits the local watcher and forwarding; use `sync` manually. Initial port allocation avoids local conflicts. Saved assignments stay stable; use `up --port NAME=NUMBER` if another application takes one.

The devcontainer recipe must exist on a published branch before GitHub can build it. Branch, machine, and idle timeout settings apply at creation; source sync does not change an existing Codespace's provider settings or rebuild its image. Later edits and checks use local snapshots without commits or pushes. Display names start with the pull request number when one is found, otherwise a branch label, followed by a stable worktree marker.

Use native personal Codespaces secrets with access to this repository for remote environment values. Environment-file sync is not supported: local `.env` files remain excluded. Application adapters decide which remote environment values their services need.

## What a check means

A check receives one staged source snapshot containing tracked and nonignored untracked files, including local modifications and deletions, subject to the built-in exclusions. Local and remote locks serialize sync and checks. Services stop for check transactions. The remote transfer uses a distinct staging directory and a verified manifest, so a lost connection cannot let a retry overwrite a running check's input. rsync uses the existing source as a copy basis; it does not use a broad `--delete` option. For interactive edits, `sync.mode: "live"` keeps service watchers running when setup inputs are unchanged.

The check streams stdout/stderr and saves private output and result files. The result identifies each command, working directory, exit code, source snapshot, and shutdown outcome. If local source changes while it runs, the result is stale. A successful stale batch returns `75`; a failed stale batch retains its failure code. Retry after edits settle before reporting current success. Configuration, setup, sync, connection, and shutdown failures can return `70`; invalid command syntax returns `2`; an interrupted local call returns `130`. `enabled` returns `0` for opt-in, `3` for absent/disabled, or an error if private state cannot be read. A configured check can return the same numbers; use the result file and error text to identify the cause.

`status` is passive. It reports provider state, payer, machine, idle timeout, retention metadata, and the last observed setup/service state when available. It does not resume the Codespace or prove that an application is currently ready.

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
