# Releasing

Every push to `develop` is released by the `Release` workflow
(`.github/workflows/release.yml`). A merged pull request counts, and so does
a direct push. Do not tag by hand.

The workflow:

1. runs the tests on Python 3.9;
2. computes the next version with `scripts/next-version.sh`;
3. writes it into `pyproject.toml`, `src/archplan/__init__.py` and
   `.claude-plugin/plugin.json` with `scripts/set-version.sh`;
4. commits `Release vX`, tags it and pushes both to `develop`;
5. creates the GitHub release with generated notes.

The release commit is pushed with `GITHUB_TOKEN`. Pushes made with that
token do not start workflows, so a release does not release itself again.
Runs wait for each other, and a run with nothing new since the last release
does nothing.

Running editor servers and editor tabs pick up a release by themselves; see
"Updates" in the README. MCP servers need a restart of Claude Code.

## Versions (CalVer)

Versions follow rela's scheme: `vYY.M.BUILD`.

| Part    | Meaning                                              |
| ------- | ---------------------------------------------------- |
| `YY`    | Two-digit UTC year                                   |
| `M`     | Month, no leading zero (`1` to `12`)                 |
| `BUILD` | `0` for the month's first release, then `1`, `2`, … |

`v26.10.0` is the first release of October 2026, and `v26.10.1` the second.
The month has no leading zero because semver and PEP 440 forbid one.
Releases before this scheme were `v0.1.0` to `v0.8.1`; they sort below every
CalVer version.

## The Go exporter

The Go exporter is a separate Go module in `exporters/archplan-go`. Go only
accepts major version 0 or 1 for its module path, so its tag keeps major 0
and puts the year and month in the minor version: release `v26.10.0` also
tags `exporters/archplan-go/v0.2610.0`. `archplan init` in archplan
`v26.10.0` writes `go run ...@v0.2610.0` into `config.json`, so a project
keeps the exporter it was set up with. A development build, whose version
does not follow the scheme, writes `@latest`.
