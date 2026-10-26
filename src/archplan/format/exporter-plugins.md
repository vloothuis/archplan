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
- When `export` is a list, the approval covers the whole list, and the
  files named in each command. A changed, added or removed entry, or a
  changed prefix, needs a new approval.

- The command writes the model to the path given for `{out}`, usually
  `.archplan/model.archmodel.json`. When it has no output option, it may
  write to standard output and the command can redirect it.
- `{root}` or `{chdir}` name the folder to read when the code sits in a
  subfolder, such as `frontend/`.
- Progress and warnings go to standard error.
- archplan does not pass the `exclude` list from `.archplan/config.json` to
  plugin exporters. After the export, it removes the excluded paths from the
  model: files, refs, types, signatures, literals, enums, components whose
  `path` is excluded, and the dependencies and evidence that came from them.
  An exporter that reads the list itself gives exact dependency weights.
- In the same step, archplan applies `components` and `layers` from
  `.archplan/config.json`: it moves files to the configured components and
  adds up their dependencies again. A `files` section with `refs` makes those
  weights exact. Optional `module` ownership and `moduleImports: [{to, line}]`
  retain logical import-only dependencies without inventing file refs.
  Module identities and targets are repository-relative, not component IDs;
  prefixes and grouping leave them unchanged. An import fans out to every
  current component owning included files in its target module. File refs
  and module imports are deduplicated by referring file per component edge.
  Partial exclusions retain imports to remaining module files; fully removed
  modules lose their imports. Unknown targets remain validation errors.
  See [files](archmodel.md#files) for the contract and Swift limitations.
- The command must not change the source files, and must not need the network
  after its install step.

### Several exporters

A project can have parts in more than one language, such as a Go server and
a TypeScript app in `frontend/`. Then `export` in `.archplan/config.json`
is a list. Each entry is a command, or an object with the command and a
prefix:

```json
"export": [
  "go run github.com/vloothuis/archplan/exporters/archplan-go@latest -steps steps.json -o {out}",
  { "cmd": "archplan export-ts -C frontend -o {out}", "prefix": "web" }
]
```

- archplan runs the commands in order, from the repository root.
- In each command, archplan replaces `{out}` with a file of its own:
  `.archplan/parts/1.archmodel.json` for the first entry, and so on. A
  command without `{out}` prints its model on standard output instead.
- archplan merges the parts into `.archplan/model.archmodel.json`. Paths in
  every part are relative to the repository root, as in a single model.
- Component ids of different parts must differ. A `prefix` renames every
  component id of its part to `<prefix>/<id>`, wherever the part names a
  component. Use it when two exporters would use the same ids. When two
  parts still use the same component, type or sequence id, the export
  stops and names the id and both commands.
- Layers with the same id become one layer. Lists such as `dependencies`,
  `signatures` and `routes` are joined. The `source` section comes from the
  first part, with the languages, capabilities and generators of all parts.
- When a part's policy denies by default, the merged policy does too. Each
  component of a part that allows by default then gets a rule that allows
  its dependencies, so its own policy keeps its meaning.
- After the merge, archplan joins calls between processes: a browser
  sequence's HTTP call with the server code it reaches. See
  [Calls between processes](archmodel.md#calls-between-processes).
- `.archplan/parts/` is not committed.

`archplan init --export CMD --export CMD` writes such a list.
`archplan exporters` suggests one when exporters match different folders.

A string `export` works as before. `{out}` in it becomes the model path.

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
- `routes`: the HTTP routes and their handlers, so archplan can join a
  browser's call to the server code.
- `policy`, `layers` and `vocabulary`.

archplan computes the vocabulary itself when a model has none.

The built-in Python and TypeScript exporters also read test-recorded sequences
from `.archplan/sequences.json` in the project root. The file has format
`archplan-sequences/1` and a `sequences` object using the model's sequence
shape. Each sequence needs `participants` and `messages` lists. Keep argument
values out of labels. A missing file means no sequences; a malformed file stops
the export with an error. When the file has sequences, the model lists the
`calls` capability. The Python exporter also reads the trace files that
`archplan trace-python` writes to `.archplan/sequences`; a sequence in
`sequences.json` replaces a traced one with the same id.

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
  component `path` and `match`, `files` keys and refs, type and route
  `file`, and the file part of `evidence`.
- No path may be absolute, start with `~`, or contain a `..` segment.
- Leave out paths outside the repository, such as dependencies in a package
  cache. Model third-party code as external components (`"kind": "external"`)
  without a file path.
- Every id a model refers to must exist in that model:
  - dependency `from` and `to` name components;
  - a `files` entry's `component` names a component;
  - type `component`, field `ref`, `implements` and `uses` name types;
  - a route's `component` names a component;
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
