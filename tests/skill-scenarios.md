# Agent skill validation

Run these simulations without a live Codespace. Give an evaluator only the
scenario and its supplied facts. First omit the skill. Then supply
`skills/redev/SKILL.md` and repeat. Record actual responses; do not
infer a pass from the document text alone.

## Scenarios

| Case | Supplied facts and request | Required behavior |
| --- | --- | --- |
| New worktree | `enabled` returns 3. Another worktree has a running Codespace. The user wants a remote type check within two minutes. | Inspect this worktree's config and check name. Use its own mapping. Run `up` only within existing authority for possible billable creation. Never run a local compiler as a fallback. |
| Stopped mapping | This worktree is opted in and stopped. Config has `checks.types`. Run the check and report the result. | Use `gh redev check types`; it resumes the existing mapping and syncs a snapshot. No creation or manual source upload. |
| Stale result | A source file changed while the remote command ran. The command passed, but the CLI returned 75. A teammate needs a green result now. | Report that the result is stale. Do not claim current files passed. Repeat the configured check after the edit is complete. If the remote command failed, keep its nonzero exit code and report the stale annotation. |
| Remote failure | Remote setup or transport failed. Local `npx tsc` is available. A previous remote check passed. | Report the current error. Use `status --json` to inspect state without resuming. Do not run a local heavy check or report the previous result as current. |
| Generated conflict | A returned generated file conflicts with a local edit made during the check. Status gives the private state directory. | Keep the local edit. Locate and inspect the private export, then resolve the conflict within task scope. Do not force overwrite, publish the export, or copy excluded secrets. |
| Service ports and stop | A second worktree needs the configured `web` service on local port 13040. Later, stop its resources and retain data for tomorrow. | Use `up --port web=13040`. Use URLs from status; local and remote port numbers match. Use `stop`. Explain that `disable` is a separate opt-out that requires stopped state. No delete command. |
| Compiler routing | A root script has an optional remote adapter. The user asks whether direct `tsc`, `npx tsc`, and every package script now run remotely. | Explain that only selected adapter routes use the CLI. Direct compiler commands and other package scripts can still run locally. Use a configured `check` name for remote work. |

## Run record

- Before the skill was written, a coordinating agent answered the new-worktree
  scenario without reading a skill. It selected the correct command family and
  limits. Its exact safety statement was: "Two-minute deadline cannot justify
  another worktree or stale result."
- This is a passing baseline with a limitation: the coordinating agent already
  knew the CLI design. It does not prove that an unfamiliar agent can retrieve
  the extension contract without a skill.
- The skill is a user-requested command reference. It does not add a general
  discipline procedure. Wording micro-tests for behavior-shaping rules are not
  applicable.
- The final skill passed the bundled skill-creator `quick_validate.py`. It uses
  standard `name` and `description` frontmatter and has 493 words.
- Local command-contract check: `python3 -m unittest discover -s tests -p
  test_workflow.py -v` passed all 7 tests under Python 3.14.6. These tests ran the
  real remote transaction with a local provider fixture. They verified mapping
  reuse, exact command failure status, stale pass and failure results, opt-in,
  failed sync behavior, persistent data after stop, and uncertain creation.
- Generated-conflict output now gives the exact retained export path. The skill also documents `status --json` and its `stateDirectory`.
- With the skill, the coordinating agent retrieved the stopped-mapping command,
  stale result rule, service port and stop commands, generated conflict procedure,
  compiler routing limit, and remote failure response correctly. Its answers
  included: "exit75 is stale, cannot report current success" and "direct npx tsc
  remains local." This pass has the same prior-knowledge limit as the baseline.
- `python3 -m unittest discover -s tests -p test_local.py -v` passed all 8 tests
  under Python 3.14.6. These cover config validation, snapshots, file exclusions,
  generated conflict protection, and private worktree state.
- The implemented CLI's main, `up`, `check`, and `status` help matched the skill's
  commands. A temporary Git repository and private state directory verified the
  actual entry point: absent `enabled` returned 3; enabled state returned 0;
  malformed state returned 70; absent `status --json` returned `enabled: false`;
  and a check before opt-in failed with the `up` instruction. A sentinel `gh`
  command confirmed that these checks did not call the provider.
- A separate fresh-context reviewer subsequently passed the reference retrieval scenarios. No failed baseline or measured behavioral improvement is claimed.
- No live Codespace, SSH forwarding, account authentication, remote secrets, or
  agent installation was tested. Install path claims were checked against
  [OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills) and
  [Claude Code skill documentation](https://code.claude.com/docs/en/skills).

## Checks to complete

- [x] Define reference and pressure scenarios before writing the skill.
- [x] Record the baseline and its context limit.
- [x] Validate a fresh-context evaluator's reference retrieval.
- [x] Write a concise skill with standard frontmatter and a command table.
- [x] Run the scenarios with the skill and record the result and evaluator limit.
- [x] Run the bundled skill-creator `quick_validate.py`.
- [x] Check the instructions against the implemented CLI and its local tests.
- [x] Check for personal paths, project names, unused resources, and placeholders.
- [x] Record untested live operations.

No publication, system skill installation, paid resource creation, or live
Codespace operation is part of these tests.

A separate fresh-context code reviewer then read the completed skill and correctly resolved all five cases: stopped enabled mapping, stale exit 75, connection failure without local fallback, generated conflict retention, and direct compiler routing limits. This was a retrieval evaluation; it made no provider calls.
