# archplan exporter for Elixir

This exporter reads a compiled Mix project and writes an `archmodel/1` file.
It can also record call traces from tests and turn them into sequences.

It ships with archplan, in [`src/archplan/elixir`](../../src/archplan/elixir):

- `archplan_export.exs` writes the model.
- `archplan_trace.ex` is an ExUnit formatter that records one trace file per test.
- `archplan_common.ex` holds the shared code (config, components, JSON).
- `archplan-elixir` is a small shell wrapper around both. `archplan export-elixir`
  and `archplan trace-elixir` run it.

Nothing is added to the project. The scripts are loaded from the command line.
`archplan init` picks this exporter when it finds a `mix.exs` in the root or in a
subdirectory. With several subdirectories, it takes the one with the most `.ex`
files under `lib/`.

## Prerequisites

- Elixir 1.17 or newer. Elixir 1.18 has a built-in JSON module. On older versions the project needs Jason.
- The project compiles. The exporter reads the BEAM files and their debug info, so do not strip debug info.
- `mix xref` works. It adds compile-time references.
- Tracing runs the test suite, so the test database and other test services must be available.

## Export

Run this from the repository root. `-C` names the directory with `mix.exs`.

```sh
archplan export-elixir -C core -o .archplan/model.archmodel.json
```

Without `-o` the model goes to stdout. Progress messages go to stderr.

Options:

| Option | Meaning |
| --- | --- |
| `-o FILE`, `--output FILE` | Write the model to FILE. |
| `--config FILE` | Use this config file. |
| `--sequences DIR` | Read trace files from DIR. Default: `<repo root>/.archplan/sequences`, if it exists. |
| `--no-sequences` | Do not read trace files. |
| `--root DIR` | Repository root for relative paths. Default: the git root. |

The wrapper runs `mix eval`, not `mix run`. Phoenix projects often alias `run` to start the server, so `mix run` would hang. `mix eval` compiles and loads the project but does not start it. Without the wrapper:

```sh
cd core
ARCHPLAN_EXPORT_SCRIPT=/path/to/archplan_export.exs \
  mix eval 'Code.eval_file(System.fetch_env!("ARCHPLAN_EXPORT_SCRIPT"))' -o ../.archplan/model.archmodel.json
```

### archplan config

`archplan init` writes the export command to `.archplan/config.json`. archplan runs it from the repository root:

```sh
archplan export-elixir -C core -o .archplan/model.archmodel.json
```

The wrapper sets `ERL_COMPILER_OPTIONS=[nowarn_deprecated_catch]` unless you set it yourself. OTP 29 deprecates `catch`, and older dependencies such as meck then fail to compile when warnings are errors.

## Config

The exporter reads `.archplan/elixir.json`. It looks in the Mix project first, then in the repository root. All keys are optional. Objects merge with the defaults. Lists replace them.

```json
{
  "depth": 2,
  "min_modules": 2,
  "components": [
    { "prefix": "Systems", "depth": 2 },
    { "prefix": "Fabric", "id": "Frameworks.Fabric" }
  ],
  "exclude_paths": ["test/**", "deps/**"],
  "exclude_modules": ["Mix.Tasks"],
  "layers": [
    { "id": "web", "name": "Web", "components": ["CoreWeb"] },
    { "id": "core", "name": "Core", "components": ["Core"] }
  ],
  "types": { "modules": false },
  "compile_refs": true,
  "trace": {
    "exclude_modules": [],
    "exclude_functions": ["*.__*", "*.impl_for", "*.impl_for!", "*.Endpoint.config"],
    "max_calls": 50000,
    "max_messages": 400,
    "max_depth": 12,
    "include_test_process": false
  }
}
```

| Key | Meaning |
| --- | --- |
| `depth` | Number of module name segments that make a component. `Systems.Assignment.Model` with depth 2 belongs to `Systems.Assignment`. |
| `min_modules` | A group with fewer modules joins its parent namespace. A group in its own directory stays. |
| `components` | Rules per module prefix. The longest matching prefix wins. A rule has `id` (all matching modules go to that component) or `depth`. |
| `exclude_paths` | Source path globs to skip. |
| `exclude_modules` | Module prefixes to skip. |
| `layers` | Optional layers. Each lists component prefixes. |
| `types.modules` | `true` adds every module as a type. Default: only modules that implement a behaviour. |
| `compile_refs` | Add compile-time references from `mix xref`. |
| `trace.exclude_modules` | Module prefixes that are not traced. |
| `trace.exclude_functions` | `Module.function` globs that are not traced. |
| `trace.max_calls` | Calls recorded per test. Later calls are dropped. |
| `trace.max_messages` | Messages kept per sequence. |
| `trace.max_depth` | Nesting depth kept per sequence. |
| `trace.include_test_process` | Record all calls of the test process itself, not only the requests it makes. |

If the project uses the `boundary` library, its boundaries become the components and the policy. This path is not tested yet.

## Tracing

Tracing turns test runs into sequences. Run the tests you want as sequences, then export.

```sh
archplan trace-elixir -C core test test/systems/pool/onboarding_page_test.exs
archplan trace-elixir -C core test --only feature
archplan export-elixir -C core -o .archplan/model.archmodel.json
```

`trace` runs a Mix task (default `test`) with the `ArchplanTrace` formatter and the normal CLI formatter. It writes one JSON file per passed or failed test. Without the wrapper:

```sh
MIX_ENV=test elixir -r /path/to/archplan_trace.ex -S mix test \
  --formatter ArchplanTrace --formatter ExUnit.CLIFormatter
```

How it works:

- At start it sets a call trace pattern on every exported function of the project's own modules.
- It turns on call tracing for all processes for the whole run. It does not wait for ExUnit's test events: ExUnit sends them without waiting for formatters, so a test can be running before its start event arrives.
- It gives each process to a test: the test process by its label, other processes through their `$callers` (tasks, LiveView tests) or `$ancestors`. A process with no test there, such as one the application started, belongs to the only running test. While several tests run, its calls are dropped.
- It records module, function and arity only. Argument and return values are never recorded.
- It keeps calls into another module. Calls within the same module are not kept, with one exception: when a Phoenix controller's `call/2` applies an action, the step is named after the action, such as `Systems.Storage.Controller.export/2`. The trace's `entry` is the first such action.
- The export keeps only calls that cross a component boundary.

Each process gets an actor, which becomes the first participant:

| Process | Actor |
| --- | --- |
| LiveView, controller, endpoint or other web process | `browser` |
| Oban job | `oban` (async) |
| Test process | `browser` for the requests it makes: a call into a controller, endpoint, router or plug, with everything below it, as `Phoenix.ConnTest` runs them in the test process. Its other calls, such as factories, are not recorded, unless `ARCHPLAN_TRACE_TEST=1`. |
| Any other process | `process` (async) |

Environment variables:

| Variable | Meaning |
| --- | --- |
| `ARCHPLAN_TRACE_DIR` | Output directory. Default: `<repo root>/.archplan/sequences`. |
| `ARCHPLAN_CONFIG` | Config file. Default: `.archplan/elixir.json`. |
| `ARCHPLAN_TRACE_TEST=1` | Record all calls of the test process. |

### Parallel tests

Tests may run in parallel. A process the application started, such as a GenServer or a PubSub subscriber, cannot be given to one test while several run, so its calls are dropped. Use `--max-cases 1` to keep them.

### Cost

- Setup takes a few seconds on a large project. On eyra core (1,295 traced modules) it takes 8 to 9 seconds.
- Each traced call sends a message to the tracer process. This slows down call-heavy tests. On eyra core the measured slowdown was up to about 2 times, but the measurements were noisy.
- Trace files take about 50 to 500 kB per LiveView test.

### Limits

- A process that exits before the tracer handles its first call cannot be inspected. It counts as `process` and is shared: while several tests run, its calls are dropped.
- Calls to modules outside the project (Ecto, Phoenix) are not traced. Calls back into the project from those modules are.
- LiveView function components show up as calls from the page to the component module. This can make sequences long. Exclude noisy modules with `trace.exclude_modules`.
- Long tests are cut at `max_calls` or `max_messages`. The title then ends with "(truncated)". Failed tests end with "(failed)".

## Mapping

| Elixir | archmodel |
| --- | --- |
| Module namespace, at `depth` segments or per rule | component; `path` is the directory with most of its files |
| Remote calls, struct use, aliases in debug info | dependency; `weight` is the number of files, `evidence` is `file:line` |
| `@behaviour` and `defimpl` | dependency |
| Compile-time reference from `mix xref` | dependency without a line |
| Ecto schema | `struct` type; associations as `ref`, `has_many` and `many_to_many` with `many` |
| Struct with `@type t` | `struct` type with typed fields |
| Behaviour or protocol | `interface` type; callbacks as methods |
| Behaviour implementation or protocol `defimpl` | `implements` |
| LiveView, LiveComponent, controller | `class` type |
| Public function | method named `name/arity`, with the `@spec` (without the name) as `sig` |
| No `boundary` library | policy `allow` |
| `boundary` library | policy `deny` with the declared dependencies as rules |
| Traced test | sequence |
| Repeated identical calls | loop fragment, label `×N` |

## Tests

```sh
exporters/elixir/test/run_test.sh
```

The test copies a small Mix project from `test/fixture` to a temporary directory, traces its tests, exports it and checks the result. It needs network access to hex.pm the first time. Schema validation runs when a python with `jsonschema` is found.
