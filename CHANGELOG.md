# Changelog

Notable changes to this repository, newest first. Each PR adds a Changie
fragment under `.changes/unreleased/`; merging to `main` releases them.

## v0.4.1 - 2026-11-16

### Fixed

* Dependency updates: fix(container): update image ghcr.io/nvidia/openshell/sandbox to v0.1.8 (#16)

## v0.4.0 - 2026-11-15

### Changed

* The Brave plugin is a verified official install, so that web_search works in a sandboxed session: the image holds it in an npm cache, not at /opt/openclaw/plugins/node_modules/@openclaw/brave-plugin, and the new official plugins step (install-official-plugins, an init container before doctor) installs it in the state directory with no network, again at each start, and stops the start for any other plugin in the state. This changes the image contract. Remove the Brave path from plugins.load.paths. The tool test runs a web search in a sandboxed session

## v0.3.0 - 2026-11-15

### Added

* The Brave plugin (@openclaw/brave-plugin, at the release of the product) for the web_search tool; it reads its key from BRAVE_API_KEY, and the image test runs one search against a fake endpoint

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
