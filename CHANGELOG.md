# Changelog

Notable changes to this repository, newest first. Each PR adds a Changie
fragment under `.changes/unreleased/`; merging to `main` releases them.

## v0.2.1 - 2026-11-15

### Security

* bin/privacy-check reads the denylist in two forms: the plain list, and one opaque line that `bin/privacy-check --encode` makes from it. A value that reads as neither stops the check with exit 2. Hits print as "hit: <where>" with every term in it replaced by "[term]", case is ignored outside ASCII too whatever the locale, paths print as they are rather than as escapes, no term number and no count, and a failed search stops the check instead of passing it. The same files as the next app-repo-template release.

## v0.2.0 - 2026-11-12

### Changed

* The tool test checks that the tools that are on by default and that the config denies (code_execution, file_fetch, file_write, dir_list, dir_fetch) are not available to a turn

### Removed

* The Claude CLI runtime test and its test layer, the sandbox_exec plugin spike and the bridge spike: the image holds no Claude Code and no plugin for a shell tool, and the model bridge has its own repository

## v0.1.0 - 2026-11-11

### Added

* The derived image of the agent platform: OpenClaw 2026.11.3 with the OpenShell 0.1.7 CLI and the openshell and signal plugins, the image contract, and the image test, the tool test and the trace test that gate each release
