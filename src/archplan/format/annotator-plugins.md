# Annotator plugins

An annotator reads and writes *declarations*: facts about the code that a
person decides, such as "this component is in the internal zone" or "this
function is a guard". Declarations live in the repository, next to the code
they describe. archplan reads them after every export and adds them to the
model. When the human or the agent changes one, the annotator writes the
change back into the code, as a patch the human reviews.

archplan ships a built-in annotator that uses marker comments and the front
matter of a component's `GLOSSARY.md`. A project can replace it, one key at a
time, with its own plugin. The plugin chooses the form: comments, the
language's own annotations, database column comments, or a file another tool
reads.

## Keys

A key names a kind of target and a property of it.

| Key | Target | Value | Meaning |
|---|---|---|---|
| `component.zone` | a component id | a zone id: lower case letters, digits, `_` and `-` | the trust zone the component is in, such as `internet` or `internal` |
| `component.gateway` | a component id | a list of 1 to 20 component ids | the components through which others must reach this one, such as the repository in front of a store |
| `component.gated_by` | a component id | a list of 1 to 20 component ids | the components that wrap this one at wiring, so its consumers get it through them, such as a visibility decorator around a base reader |
| `function.guard` | a function or type | `true`, `authn` or `authz` | the function or type checks who the caller is (`authn`), what the caller may do (`authz`), or both (`true`) |
| `function.sink` | a function or type | `write` or `read` | the function changes (`write`) or returns (`read`) data that only some callers may change or see |
| `function.validates` | a function or type | `true` or an input kind | the function checks or parses outside input, so that what comes after gets checked values: all kinds (`true`) or one, such as escaping for `html` |
| `function.interprets` | a function or type | an input kind | the function runs or resolves its input as that kind, so unchecked input can change what it does |
| `function.source` | a function or type | `true` | the function brings in outside input other than a request, such as a webhook body, a queue message or an upload |
| `function.trusts` | a function or type | `true` | the function assumes its caller checked the input, and does not check it itself |
| `entrypoint.auth` | a function that handles outside input | `required`, `authz` or `none` | whether the entry point needs an authenticated caller (`required`), also a check of what that caller may do, such as read the record the request names (`authz`), or neither (`none`) |

The input kinds are `sql` (a query), `shell` (a command), `path` (a file
path), `html` (markup), `url` (an address to fetch or redirect to), `code` (a
script, an expression or a template) and `deserialize` (serialized objects).
A plain `guard` no longer means "validates input": it means both authn and
authz. Declare a validator with `validates`.

A declaration can have a `reason`: one line on why, at most 300 characters,
of letters, digits, spaces and `. , ; ' ( ) / ? ! & + % # @ -`. Without `:` and
`=`, a reason cannot become a magic comment such as `coding:`. archplan keeps it next
to the value. Give a reason for `entrypoint.auth` `none`, such as "load
balancer health check".

### Target ids

- A component target uses the model's component id.
- A function target uses the model's id of the function or type, from
  `signatures` or `types`. When the model has none for it, the id is
  `<file>:<name>`, such as `ops/views.py:health`.
- An entry point target uses the id of the function that handles the input:
  the same id as the function, and the same id a sequence's `entry` names.

An annotator does not need to know the model's ids. It can name a function
target by its place in the code, `{"kind": "function", "file", "line",
"name"}`, and archplan finds the id: the function or type in `signatures` or
`types` with that file and name, nearest to that line.

## Entries

An annotator is a JSON file in `.archplan/annotators/`:

```json
{"id": "django-db",
 "name": "Django column comments",
 "keys": ["entrypoint.auth", "function.guard"],
 "read": "python3 tools/archplan_annotations.py read {ctx}",
 "write": "python3 tools/archplan_annotations.py write {ctx}",
 "params": {"settings": "shop.settings"},
 "timeout": 120}
```

- `id`: lower case letters, digits, `_` and `-`. `builtin` is taken.
- `keys`: the keys the annotator owns.
- `read`: required. The command that prints the declarations.
- `write`: optional. Without it, archplan reports every change to the
  annotator's keys as `unsupported`.
- `params`: optional values passed to both commands.
- `timeout`: seconds per command, 1 to 600; the default is 120.

Both commands must contain `{ctx}`. archplan replaces it with the quoted path
of a JSON file that holds the context.

### Who owns a key

Each key has exactly one owner, so a fact is read from one place only.

- A key that no project annotator lists belongs to the built-in annotator.
- A key that one project annotator lists belongs to that annotator.
- When two project annotators list the same key, archplan reads neither for
  that key and reports the conflict. Choose the owner in
  `.archplan/config.json`:

```json
"annotators": {"owners": {"function.guard": "django-db"}, "builtin": true}
```

`owners` can also name `builtin`. `"builtin": false` switches the built-in
annotator off, so keys without a project owner are not read at all.

### Approval

An annotator's commands come from the repository, so they run only after a
person approved them for this directory: `archplan annotators trust <id>` at
a terminal. The approval covers the `read` and `write` command text and the
content of each repository file they name, as for exporters. When one of
those files changes, the annotator needs a new approval. Until then, the
export leaves its keys out and says why. The built-in annotator needs no
approval.

## Reading

archplan runs `read` from the repository root after every export, and when
you run `archplan annotators read`. The command must read the files under the
context's `root`, not under its working directory: to check a write, archplan
runs it from the repository root with `root` set to the scratch copy. The
context file holds:

```json
{"version": 1, "action": "read", "annotator": "django-db", "root": "/abs/repo",
 "keys": ["entrypoint.auth", "function.guard"], "params": {...}, "model": {...}}
```

`model` is the as-is model of the export, without `declarations`. The command
prints one JSON object on standard output:

```json
{"declarations": [
  {"target": {"kind": "function", "file": "ops/views.py", "line": 9, "name": "health"},
   "key": "auth", "value": "none", "reason": "load balancer health check",
   "file": "ops/views.py", "line": 8},
  {"target": {"kind": "component", "id": "billing"}, "key": "zone", "value": "internal",
   "file": "billing/GLOSSARY.md", "line": 2}
]}
```

- `target.kind` and `key` together form the full key. `entrypoint` with
  `auth` is `entrypoint.auth`.
- `file` and `line` say where the declaration is written. Give them when
  the declaration has one place, so the writer and the editor can find it.
- archplan drops declarations for keys the annotator does not own, values
  that do not fit the key, components the model does not have, and paths
  that leave the repository. It keeps at most 5000 declarations per
  annotator.
- When the same target has a key twice with different values, archplan
  keeps the first one and reports the conflict.

## Writing

The agent and `archplan annotators write` send changes:

```json
{"changes": [
  {"op": "set", "target": {"kind": "component", "id": "billing"}, "key": "zone", "value": "internal"},
  {"op": "set", "target": {"kind": "function", "id": "accounts.auth.require_user"}, "key": "guard",
   "value": true},
  {"op": "remove", "target": {"kind": "entrypoint", "id": "ops/views.py:health"}, "key": "auth"}
]}
```

archplan gives each change to the annotator that owns its key. Before it
does, it adds two fields where it can:

- `at`: where the target is declared, `{"file", "line", "name"}`, from the
  model or from the current declarations.
- `current`: the declaration that is there now, with its `file` and `line`.

### The scratch copy

archplan never runs `write` in your working tree. It runs it in a scratch
copy:

1. It makes a temporary git worktree with your tracked files as they are
   now, uncommitted changes included, plus untracked files that git does not
   ignore. Of an untracked `.archplan/`, only `.archplan/annotators/` comes
   along. Ignored files, such as `node_modules` or a virtual environment,
   are not in it. A command that needs them can find the real repository in
   the context's `repo` field. It must only read there.
2. It runs `write` with the scratch copy as the working directory. The
   context's `root` is the scratch copy:

   ```json
   {"version": 1, "action": "write", "annotator": "django-db", "root": "/tmp/archplan-scratch-…/repo",
    "repo": "/abs/repo", "keys": [...], "params": {...}, "model": {...}, "changes": [...]}
   ```

   The command changes files in the scratch copy and prints one result per
   change, in the same order:

   ```json
   {"results": [{"status": "applied"}, {"status": "unsupported", "note": "no column comment for views"}]}
   ```

   `status` is `applied`, `unsupported` or `conflict`. `note` says why, in
   one line.
3. It runs `read` on the scratch copy and checks each applied change. A
   `set` must come back with its new value. A `remove` must not come back.
   When it does not, the change's status becomes `failed`, whatever the
   command printed.
4. When any change given to the command did not work, archplan puts the
   scratch copy back as it was before the command ran. The other changes of
   that run then become `failed` too, so the patch never holds a change that
   did not work. The built-in annotator gets one change at a time, so this
   only affects the change itself.
5. The difference between the scratch copy before and after all commands is
   the patch. It does not depend on your git settings. A command must not
   change `.archplan/` or `.git`; when it does, archplan rejects the whole
   patch. archplan sets `PYTHONDONTWRITEBYTECODE`, so Python caches do not
   end up in it.

With `--apply`, or `apply: true` from the agent, archplan applies the patch
to your working tree. Without it, archplan prints the patch and changes
nothing. Writing needs a git repository.

A change with status `unsupported`, `conflict` or `failed` is not in the
patch. The agent then makes that change by hand and calls `refresh_model`;
the next read shows whether it worked.

## The built-in annotator

The built-in annotator owns every key no project annotator owns.

### Component zones, gateways and gated_by

A component's zone, gateways and `gated_by` are in the front matter of the
`GLOSSARY.md` in its folder, the same file the glossaries use:

```markdown
---
zone: restricted
gateway: [billing.repo, billing.admin]
gated_by: [billing.visibility]
owns:
  Invoice: [bill]
---
```

A gateway is the way in that others must use. `gated_by` names the
components that the wiring wraps around this one, so a static dependency
on it is fine. Only `security.unguarded_path` reads `gated_by`.

The writer adds, changes or removes the `zone:`, `gateway:` and `gated_by:`
lines. It creates the file when there is none, and deletes a file that held
nothing but those lines.

### Marker comments

A marker comment sits directly above a declaration. Decorators, attributes
and other comments may come between the marker and the declaration; a blank
line may not.

```python
# archplan: auth=none reason="load balancer health check"
@require_GET
def health(request): ...
```

```go
//archplan:guard
func RequireUser(next http.Handler) http.Handler { ... }
```

```elixir
# archplan: guard
@doc "Halts the connection without a user."
def require_user(conn, _opts), do: ...
```

- A marker starts with `archplan:` in a line comment: `#` in Python, Ruby
  and Elixir; `//` in Go, JavaScript, TypeScript, Java, Kotlin, Scala, C#,
  Rust, Swift, PHP, Dart, C and C++. In Go the writer uses the directive form
  `//archplan:`, which `gofmt` keeps and `go doc` does not show.
- `guard`, `guard=authn` or `guard=authz` declares `function.guard`.
  `sink=write` or `sink=read` declares `function.sink`. `validates` or
  `validates=<kind>` declares `function.validates`, `interprets=<kind>`
  `function.interprets`, `source` `function.source` and `trusts`
  `function.trusts`. `auth=required`,
  `auth=authz` or `auth=none` declares `entrypoint.auth`. `reason="…"` gives the reason for the keys on
  the same line.
- The writer puts one key on each marker line, with the indentation of the
  declaration, above its decorators. In Go it goes directly above `func`,
  after the doc comment, where `gofmt` puts directives. It keeps the file's
  line endings.
- A declaration is a `def`, `func`, `fn`, `function`, `class` and the like,
  or a name with a modifier or type before it, such as `public User find(`.
  A call such as `find(x)` is not.
- The reader reads files that git tracks, or every file when the repository
  has no git, and leaves out the paths in `exclude`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The command printed its result. |
| 1 | The command failed. archplan reports the error and keeps nothing from it. |
| 2 | Wrong arguments, or a tool the command needs is missing. Say which one. |

Progress and warnings go to standard error. Standard output holds only the
JSON result, at most 4 MB.

## Not yet

- The model holds the declarations, but the editor does not show them or
  send changes to them yet. The agent's `annotate` tool and
  `archplan annotators write` do.
- The security detectors use the declarations (see the README).
  `function.sink` marks the functions that reach sensitive data; a key for
  sensitive types or fields is not there yet.
