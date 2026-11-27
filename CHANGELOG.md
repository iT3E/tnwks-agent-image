# Changelog

Notable changes to this repository, newest first. Each PR adds a Changie
fragment under `.changes/unreleased/`; merging to `main` releases them.

## v0.5.3 - 2026-11-27

### Fixed

* The official plugins install from the npm registry with an "npm:" spec, in the build and in the official plugins step of the pod: from OpenClaw 2026.11.10 the installer takes a bare package name from ClawHub and refuses --pin for it

## v0.5.2 - 2026-11-26

### Fixed

* The host approval plugin runs the browser form tools of promptTools only in a turn of the approver on his channel, with the same requester check as the host tools; a turn of the web UI, of a group or of a job is refused before a card

## v0.5.1 - 2026-11-23

### Fixed

* Dependency updates: fix(container): update image ghcr.io/nvidia/openshell/sandbox to v0.1.9 (#26)

## v0.5.0 - 2026-11-22

### Added

* The host approval plugin (plugins/host-approval) in /opt/openclaw/local-plugins/host-approval, read only, with its unit tests in the build; the agent config loads it with plugins.load.paths and plugins.entries.host-approval. Its update check is in tests/host-approval-check and is switched off in bin/image-test until the host server has a release

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
