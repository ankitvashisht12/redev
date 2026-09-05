# Contributing

Use Python 3.11 or later. Keep runtime dependencies in the standard library. Run `python3 -m unittest discover -s tests -v` before proposing a change. These tests require local process inspection and loopback sockets, and do not need GitHub credentials.

Keep repository-specific commands and data outside the extension. Test provider boundaries with fixtures. Test source changes, locks, and process behavior with real temporary files and child processes. A live Codespace smoke test requires explicit opt-in and must not be part of the default suite.

The supported local installation is `gh extension install .` from a Git checkout. Keep the launcher, module folder, and skill together. Document configuration and command changes together with their tests.
