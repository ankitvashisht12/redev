---
name: worktree-cloud
description: Use when a local coding agent needs remote checks or development services through gh worktree-cloud, or a repository has customizations.worktree-cloud in its devcontainer configuration. Applies to Claude Code and Codex using GitHub Codespaces.
---

# Worktree Cloud

Edit locally. Run configured heavy checks in this worktree's mapped Codespace.

Use Python 3.11 or later, authenticated official GitHub CLI (`gh`), the installed
`worktree-cloud` extension, rsync, and OpenSSH. Run commands from the worktree
root. Inspect `.devcontainer/devcontainer.json`, including
`customizations.worktree-cloud`. The CLI validates schema version `1`. Read its
shell commands before execution in a new repository; configuration is executable
code. Choose check names from `checks`.

First run `gh worktree-cloud enabled`. Exit `0` means this worktree is opted in;
`3` means absent or disabled. Other nonzero exits are errors. Each worktree has
its own mapping and private state outside the repository.

| Command | Use |
| --- | --- |
| `gh worktree-cloud up` | Create or reuse the mapping, sync source, start configured services, and start the background watcher and private loopback forwarding. |
| `gh worktree-cloud up --port web=13040` | Set the configured `web` port to 13040 locally and remotely. |
| `gh worktree-cloud check types` | Run the configured `types` check. Resume an existing opted-in mapping and sync a snapshot first. |
| `gh worktree-cloud sync` | Explicitly sync current files. |
| `gh worktree-cloud status --json` | Read state, errors, and service URLs without resuming. |
| `gh worktree-cloud stop` | Stop watcher, forwarding, and Codespace. Keep data, mapping, and opt-in. |
| `gh worktree-cloud disable` | Opt out after the Codespace is stopped. |

Use `up` only when the user's scope permits possible billable Codespace creation.
Reuse existing authorization. If creation is outside that scope, report that
setup is required. A check without opt-in fails with an `up` instruction; it does
not create a Codespace. There is no delete command.

The check streams the remote command's output and exit code. If local source
changes during a check, report the result as stale. A passing remote command then
returns `75`; a failing command keeps its nonzero exit code with a stale
annotation. Run the check again after edits are complete before reporting current
success. On setup, sync, or transport failure, inspect status and report the
error. Never fall back to local heavy checks.

Only configured `sync.generated` files return. The CLI compares local content
with its pre-run state before replacement. If a conflict occurs, retain the local
edit. Use status's `stateDirectory` to locate the private export. Do not force
overwrite or publish the export.

Remote source excludes `.git`, environment files, keys, and known database directories. Configure
required secrets remotely through a user-approved provider path. Services pause
during sync transactions and checks. Use status URLs; distinct worktrees use
distinct ports, with matching local and remote numbers for browser and SSR use.

Repository adapters can route selected scripts. They do not intercept compilers:
direct `tsc`, `npx tsc`, and other package scripts can still execute locally.
