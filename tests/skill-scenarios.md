# Agent skill validation

These scenarios test reference retrieval for the two supported modes. Use no
live Codespace, provider mutation, secret access, or local heavy application
check. Give an evaluator the scenario and supplied facts, first without the
skill, then with `skills/redev/SKILL.md`. Record actual answers. Do not infer a
behavioral pass from document text or command tests alone.

## Scenarios

| Case | Supplied facts and request | Required behavior |
| --- | --- | --- |
| New validation worktree | `enabled` returns 3; config has `checks.types` and `checks.unit`. User authorized Codespace use and wants those checks, with no application deployment. | Inspect validation hooks, then use `gh redev check types unit --stop`. A check can create a mapping. Do not require `up`, start services, borrow another worktree's mapping, or run local compilers. |
| Scoped test | Config defines `unit` as an `argv` check. User wants only `test_parser` and wants the resource stopped afterward. | Use `gh redev check unit --stop -- -k test_parser`. If the check were a shell string, add/select a suitable configured check instead of claiming argument passthrough works. |
| Repair loop | `types` passed, `unit` failed. Output identifies a local source defect. User wants the defect fixed and checks completed. | Read the private log, edit locally, and rerun applicable checks with `--stop`. Report current results and confirmed shutdown. Do not report the earlier pass for changed inputs or edit remote source. |
| Stale result | Source changed during a passing batch; the CLI returned 75. The user needs a current result. | Mark it stale and rerun after edits settle. Do not claim current files passed. A failed stale batch keeps its failure code. |
| Infrastructure or shutdown failure | Setup/transport failed, or `stop.requested` is true but `stop.confirmed` is false with an error. Local compilers are available. | Inspect result/status, repair within scope, and verify shutdown separately. Report an unresolved cause. Never fall back to a local heavy check or say the Codespace stopped without confirmation. |
| Interactive testing | User wants to test a web service and will make edits for an hour. Config uses `sync.mode: live`; `web` port exists. | Use `up`, give status URLs, and keep the environment running until testing ends. Ordinary edits use live sync. Do not run `--stop` after each edit or impose a new shutdown time. |
| Validation during testing | Services are running. User explicitly wants isolated validation and shutdown. | Explain the mode effect briefly, then use selected `check ... --stop` under existing authority. It stops the interactive session and uses validation source. Do not claim services stay running. |
| Passive status | Provider state is `Available`; readiness says `not observed`, `checkedLive: false`. Status includes payer, machine, idle timeout, and retention. | Separate provider availability from application readiness. Report known metadata without claiming live checks passed or that missing metadata means no cost. |
| Optional service | A `when` command returns 3. Other services are ready. User asks whether all configured services run. | State that the service was skipped. Do not label it running or failed. Exit 0 starts it; other nonzero exits fail startup. Conditions run on service start, not every live edit. |
| Generated files | Validation needs tracked generated types. Interactive config enables `seedGenerated`; remote generated output already differs. | Treat tracked generated files as validation inputs with no return. Interactive seeds fill missing files only. Preserve existing remote files; use the separate return conflict protocol for local edits. |
| Environment values | User has local `.env` files and native personal Codespaces secrets with repository access. Env-file sync is not supported. | Use the remote provider values through project adapters. Keep local env files excluded; do not invent an upload flag, read their values, or print secrets. |
| End testing | User is done but needs remote development data tomorrow. A pull request was closed. | Run `stop` and confirm shutdown. Keep mapping/data/opt-in. `disable` is a separate opt-out after stop. Do not delete the Codespace or claim PR closure caused deletion. Explain retention/storage metadata if relevant. |
| Creation settings | An existing mapping has a different idle timeout from current config. A new pull request number is available. | Do not claim sync updates provider timeout or renames the existing mapping. Creation settings apply to new environments. New display names use a PR-number prefix when found, otherwise a branch label. |
| Compiler routing | A root script has a remote adapter. User asks whether direct `tsc`, `npx tsc`, and all package scripts now run remotely. | Only selected adapters route to the CLI. Direct compilers and other scripts can remain local. Use a configured remote check. |

## Evidence for this revision

The earlier skill described a CLI where checks could not create a mapping. Its
old retrieval runs do not validate this revision. The current entry point,
configuration validator, runner, and local test cases were read before editing.

The reference changes address observable differences in the implemented CLI:
check batches and literal arguments, validation shutdown, separate source
directories, service preparation, live sync, optional services, generated seeds,
and passive provider metadata. No failed baseline or measured improvement in
agent behavior is claimed.

- Skill frontmatter: Ruby's YAML parser accepted the document; allowed keys,
  name syntax/length, and description type/length passed. The bundled
  `quick_validate.py` was attempted but could not import PyYAML from the available
  Python runtimes. No package was installed to change the test environment.
- All 16 shell command examples in README/configuration parsed through the real
  CLI parser. The configuration JSON passed `validate_config`; batch selection,
  literal runner arguments, and the skill's relative reference link passed.
- `test_cli_checks.py`: 4 tests passed. `test_provider_metadata.py`: 8 tests
  passed. These use local fixtures, not live provider operations.
- The final full Python suite passed all 128 tests, including optional services,
  generated seeds, login-shell directory handling, old setup-cache recovery,
  and source edits during shutdown. The primary agent repeated this suite.
  This is command-test evidence, separate from agent retrieval evaluation.
- Independent review confirmed that validation requests select `check --stop`
  with local fixes and retries, while browser testing selects `up` and keeps
  services running until testing ends.
- Fresh-context scenario evaluation: not run for this revision.
- Live Codespace creation, container build, SSH forwarding, native secret
  injection, and application authentication: not tested by this revision.

The test scope excludes publication, system skill installation, paid resource
creation, and all live provider writes.
