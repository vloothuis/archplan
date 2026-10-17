# Exporter plugins

An exporter reads a repository and writes one archmodel file. archplan ships
exporters for Go, Python, Elixir, TypeScript and a regex-based `lite` exporter.
Any other program can be an exporter. Register it with a JSON entry in
`.archplan/exporters/` (see [`../contrib/README.md`](../contrib/README.md)),
or set `export` in `.archplan/config.json`.

## Running

archplan runs the command from the repository root, through the shell, after
a person has approved it with `archplan trust`.

### Approval

An export command runs code. A cloned repository can be hostile, so archplan
asks a person before a command runs for the first time in a directory.

- `archplan init` approves the command it writes only when the exporter reads
  the code without running any of it. Of the built-in exporters, that is
  `python` (it parses files with Python's `ast` module) and `lite` (regular
  expressions).
- An entry with `"runs_repo_code": true` runs code from the repository. The
  built-in `typescript` exporter loads the project's own `typescript`
  package from `node_modules`. The `elixir` exporter compiles `mix.exs` and
  the project. The `go` exporter runs the go toolchain in the module. For
  these, `archplan init` says that approval is needed, and a person runs
  `archplan trust` in a terminal after reading the command.
- A command from a project entry in `.archplan/exporters/` always needs
  `archplan trust`.
- The approval covers the command text and the content of each argument
  that names a file in the repository, such as `tools/export.py` in
  `python3 tools/export.py -o {out}`. archplan stores a sha256 hash of each
  such file. When one changes, for example after a `git pull`, the command is
  not approved anymore, and archplan names the file that changed. The output
  file after `-o` does not count. Files the command reads on its own, such as
  `node_modules` or `mix.exs`, are not checked: approve such an exporter only
  for code you trust.
- The agent, the editor and `archplan check --export` never ask. They run
  only an approved command.

- The command writes the model to the path given for `{out}`, usually
  `.archplan/model.archmodel.json`. When it has no output option, it may
  write to standard output and the command can redirect it.
- `{root}` or `{chdir}` name the folder to read when the code sits in a
  subfolder, such as `frontend/`.
- Progress and warnings go to standard error.
- The command must not change the source files, and must not need the network
  after its install step.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | The model was written. Warnings, such as files that did not parse, go to standard error. |
| 1 | The export failed, for example because the code does not compile. No model was written. |
| 2 | Wrong arguments, or a tool the exporter needs is missing. Say which one and how to install it. |

## The model

Write format `archmodel/1`, described in [`archmodel.md`](archmodel.md) and
[`archmodel.schema.json`](archmodel.schema.json).

Required:

- `format`: `"archmodel/1"`.
- `source.generator`: the exporter's name.
- `source.languages`.
- `source.capabilities`: what this model contains (see below).
- `components`, with at least one internal component.

Optional, in order of value to archplan:

- `dependencies` between components, with `weight` and `evidence`.
- `files`: the file-level references that architecture profiles check.
- `types` with `fields`, `implements` and `uses`.
- `sequences`, from traces.
- `policy`, `layers` and `vocabulary`.

archplan computes the vocabulary itself when a model has none.

The Python exporter also reads test-recorded sequences from
`.archplan/sequences.json`. The file has format `archplan-sequences/1` and a
`sequences` object using the model's sequence shape. Keep argument values out
of labels. The repository's sequence tests regenerate this file from calls
observed while exercising representative workflows.

## Capabilities

`source.capabilities` lists what the model contains:

| Flag | The model has | archplan uses it for |
|---|---|---|
| `components` | internal components with `path` and `match` | the diagram, hotspots, all findings |
| `deps` | `dependencies` between components | cycles, tangles, layer findings, refactor briefs |
| `files` | the `files` section | profile rules (who calls whom), glossary leaks per file |
| `types` | `types` | the types view, field and synonym findings |
| `fields` | `fields` on types, with `ref` where a field holds another type | field cluster and synonym findings, type relations |
| `calls` | `sequences` | the sequences view and sequence diffs |
| `signatures` | `signatures` of functions and methods | detectors that read signatures |
| `literals` | `literals`, such as status strings | detectors that read literal values |
| `schema` | `schema` with at least one table | the data detectors: shared tables, foreign keys across components |

A model without a capability still works. archplan reports the checks it
could not run as "not checked", with the reason.

## Paths and ids

- Every path is relative to the repository root and uses `/`. This covers
  component `path` and `match`, `files` keys and refs, type `file`, and the
  file part of `evidence`.
- No path may be absolute, start with `~`, or contain a `..` segment.
- Leave out paths outside the repository, such as dependencies in a package
  cache. Model third-party code as external components (`"kind": "external"`)
  without a file path.
- Every id a model refers to must exist in that model:
  - dependency `from` and `to` name components;
  - a `files` entry's `component` names a component;
  - type `component`, field `ref`, `implements` and `uses` name types;
  - a component's `layer` names a layer.
- Ids must be stable between runs (see [IDs](archmodel.md#ids)).

## Checking an exporter

```sh
archplan export-check .archplan/model.archmodel.json --root .
```

It checks the shape of each section, references to unknown ids, and paths
that leave the root. It lists the capabilities it found next to the declared
ones. It exits with 0 when the model is valid, 1 when it is not, and 2 when
the file cannot be read.
