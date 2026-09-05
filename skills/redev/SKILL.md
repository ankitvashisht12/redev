---
name: redev
description: Use when a local coding agent needs selected remote checks or ongoing development services through gh redev, or a repository has customizations.redev in its devcontainer configuration. Applies to Codex and Claude Code using GitHub Codespaces.
---

# redev

Keep source edits and coding agents local. Select the remote mode that matches
the request: validation that stops, or interactive testing that stays running.

From the worktree root, inspect `.devcontainer/devcontainer.json` and its
`customizations.redev` commands. Use configured check names. Check `gh redev
--help`, `gh redev enabled`, and `gh redev status --json` as needed. `enabled`
returns 0 for opt-in, 3 for absent/disabled, and another code for an error.

Both `check` and `up` can create a billable Codespace or reuse this worktree's
mapping. Use existing user authorization. `--codespace NAME` selects an existing
environment in the current account/repository; do not borrow another worktree's
mapping. A missing mapping does not require starting services first.

## Selected validation

```sh
gh redev check types unit --stop
```

Select only applicable checks. This uses a separate validation source directory,
runs `setup`, `prepare`, and the selected commands, and attempts shutdown after
success, failure, or a handled interruption. Keep those hooks non-deploying.
Validation omits `servicePrepare`, services, and browser ports. Tracked generated
files are inputs; no generated output returns. `--stop` also ends an existing
interactive session.

A batch runs in order and records every normal check result, returning the first
failure. To select tests within one structured `argv` check, append literal
runner arguments: `gh redev check unit --stop -- -k test_name`. This is not
supported for shell-string checks or multiple check names.

Read the printed private result and output log. Fix failures locally, then rerun
the selected check to sync and verify the fix. Do not edit remote managed source
or substitute a local heavy check. If source changed during execution, the
result is stale: a passing batch returns 75; a failing batch retains its failure
code. Rerun after edits settle before claiming current success. If blocked,
report the cause and shutdown state. Verify `stop.confirmed`; a requested stop
alone does not prove shutdown.

## Interactive testing

```sh
gh redev up
gh redev status
```

Use the printed URLs. `up` starts service preparation, configured services,
automatic local source sync, and private forwarding. Keep them running until the
user ends testing. Then run `gh redev stop`. For an explicit port, use
`up --port web=13040`; local and remote numbers match. `portSchemes` requires the
service to supply the selected HTTP/HTTPS protocol.

`check` without `--stop` keeps the current mode and leaves the Codespace running.
In interactive mode, checks pause and restart desired services. `sync.mode:
"live"` lets ordinary edits reach running watchers; setup changes still restart
services. Optional service conditions can skip a service; report it as skipped.

## State and limits

Status is passive: provider `Available` and last-observed readiness do not prove
live application health. Report the actual payer, machine, timeout, and retention
metadata when relevant. Idle settings apply at creation. `stop` preserves data
and opt-in; `disable` opts out after stop. There is no automatic deletion on pull
request closure and no delete command.

Local environment files, credentials, and databases stay excluded. Use native
personal Codespaces secrets with repository access, or an explicitly supported
private remote config. Do not copy local `.env` files or print secret values.

Interactive generated return is limited to `sync.generated`. Preserve local
edits on conflict and inspect the retained private export. `sync.seedGenerated`
only fills missing remote files from tracked generated inputs. Direct compilers
and package scripts remain local unless a repository adapter routes them.

Read [configuration and recovery](../../docs/CONFIGURATION.md) for hook timing,
source rules, and creation recovery. Do not claim that local fixture tests prove
live Codespaces, forwarding, or application authentication.
