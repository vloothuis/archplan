# archmodel v1

`archmodel` is the interchange format between language-specific extractors and
archplan. An extractor reads a codebase and writes one JSON file. archplan
reads it as the as-is model. The edited target model is saved in the same
format, so it can live in the repository and be diffed in review.

The format describes architecture, not syntax. Every language maps its own
concepts onto six sections: components, dependencies, policy, types,
sequences and vocabulary. Each section is optional. An extractor fills what its language and
tooling can provide.

JSON Schema: [`archmodel.schema.json`](archmodel.schema.json).

## Top level

```json
{
  "format": "archmodel/1",
  "source": { ... },
  "layers": [ ... ],
  "components": { "<id>": { ... } },
  "dependencies": [ ... ],
  "files": { "<path>": { ... } },
  "policy": { ... },
  "types": { "<id>": { ... } },
  "sequences": { "<id>": { ... } },
  "signatures": [ ... ],
  "literals": [ ... ],
  "enums": [ ... ],
  "routes": [ ... ],
  "schema": { ... },
  "vocabulary": { ... },
  "declarations": [ ... ],
  "notes": { "<target>": "text" }
}
```

## IDs

IDs are opaque strings. They must be unique within their section and stable
across extractions of the same code: archplan diffs two models by ID, so an ID
that changes between runs shows up as a removal plus an addition.

Recommended IDs:

| Section | Recommended ID | Example |
|---|---|---|
| component | name used by the project's own boundary tool, else the module-relative path | `dataentry`, `internal/dataentry` |
| type | module-relative package or module path, a dot, the type name | `internal/store.Store`, `MyApp.Accounts.User`, `app/models/user.User` |
| sequence | slug of the scenario name | `read-task-as-manager` |
| message | `from>to:label#n`, where `n` counts repeats of the same call in that sequence | `acl>store:AtFaces#1` |
| concept | slug of the concept name | `cart-item` |

## source

Where the model came from, and how to check work against it.

```json
"source": {
  "repo": "sourcehaven-bv/rela",
  "ref": "6dec192e1",
  "dirty": false,
  "languages": ["go"],
  "module": "github.com/Sourcehaven-BV/rela",
  "generator": "rela tools/archexport",
  "generatedAt": "2026-10-10T09:00:00Z",
  "commands": {
    "build": "go build ./...",
    "test": "go test ./{path}/...",
    "rules": "just arch-lint",
    "sequences": "just seqtrace-compare {ref}"
  }
}
```

`commands` are what the plan puts into acceptance checks. `{path}` is replaced
with a component path and `{ref}` with the base ref. All are optional.

`capabilities` lists what the model contains: `components`, `deps`, `files`,
`types`, `fields`, `calls`, `signatures`, `literals` and `schema`. See
[`exporter-plugins.md`](exporter-plugins.md#capabilities).

## layers

Ordered groups of components, outermost first. Used for grouping and colour.

```json
"layers": [{ "id": "web-ui", "name": "Web / UI layer" }]
```

## components

The unit of dependency rules: a package, module, namespace, app or directory.

```json
"dataentry": {
  "name": "dataentry",
  "path": "internal/dataentry",
  "match": ["internal/dataentry"],
  "layer": "web-ui",
  "kind": "internal",
  "doc": "HTTP data-entry API and SPA host."
}
```

- `path` is where a developer finds the code.
- `match` lists the patterns the component covers (`**` matches any depth).
- `kind` is `internal` (default) or `external` for third-party code.

## dependencies

Observed dependencies between components. Extractors must report all of them,
not only those explained by the types section.

```json
{ "from": "dataentry", "to": "store", "weight": 14,
  "evidence": ["internal/dataentry/handler.go:12"] }
```

`weight` is the number of files (or import sites) that cause the edge.
`evidence` lists some of them as `file:line`. `typeOnly`, when present, is
how many of those files import only types along the edge (TypeScript's
`import type`); such an edge disappears at run time.

## files

Optional. File-level references, for architecture profiles: which project
files each source file imports or calls. Keys are paths relative to the
repository root, like type `file` values.

```json
"internal/dataentry/handler.go": {
  "component": "dataentry",
  "refs": [{ "to": "internal/store/store.go", "line": 12 }]
}
```

- `refs` lists each other project file once, with the line of the first
  reference when known. References to the file itself and to code outside
  the project are left out.
- A model without `files` still works. archplan then knows a file's role in
  a profile, but cannot check who calls whom.

## policy

Allowed and forbidden dependencies.

```json
"policy": {
  "default": "deny",
  "rules": [
    { "from": "cmdServer", "to": "dataentry", "effect": "allow" },
    { "from": "*", "to": "entity", "effect": "allow", "note": "common component" }
  ],
  "enforcer": { "tool": "go-arch-lint", "config": ".go-arch-lint.yml", "command": "just arch-lint" }
}
```

- With `default: "allow"`, a dependency is a violation when a `deny` rule
  matches it.
- With `default: "deny"`, a dependency is a violation unless an `allow` rule
  matches it.
- `*` matches any component.
- `enforcer` names the tool that checks the policy in CI. The plan then edits
  that tool's config instead of generating a new test.

## types

Data types, classes, interfaces and their relations.

```json
"internal/store.Store": {
  "name": "Store",
  "component": "store",
  "kind": "interface",
  "file": "internal/store/store.go",
  "line": 41,
  "doc": "Store is the persistence port.",
  "fields": [
    { "name": "db", "type": "*pgxpool.Pool" },
    { "name": "cache", "ref": "internal/cache.Cache", "ptr": true },
    { "name": "items", "ref": "internal/order.LineItem", "many": true }
  ],
  "methods": [{ "name": "Get", "sig": "(ctx context.Context, id string) (entity.Entity, error)" }],
  "implements": ["internal/store.Reader"],
  "uses": ["internal/entity.Entity"]
}
```

- `kind` is one of `struct`, `class`, `interface`, `record`, `enum`, `alias`,
  `function`, `module`.
- A field with `ref` points at another type in the model. A field with only
  `type` points outside it. `many` marks a collection.
- `implements` lists interfaces the type satisfies, whether declared or
  structural.
- `uses` lists types that appear in method signatures but not in fields. For
  an `alias`, it names the aliased type when that type is in the model.
- `sig` is free text in the source language.

## sequences

Ordered calls for one scenario, usually from traces.

```json
"read-task-as-manager": {
  "title": "read task as manager",
  "entry": "internal/dataentry.(*App).handleV1GetEntity",
  "source": "seqtrace",
  "participants": ["Caller", "dataentry", "acl", "store"],
  "fragments": { "L1": { "kind": "loop", "label": "×12", "parent": null } },
  "messages": [
    { "id": "Caller>dataentry:(*App).handleV1GetEntity#1", "from": "Caller", "to": "dataentry",
      "label": "(*App).handleV1GetEntity(w, r)", "depth": 0 },
    { "id": "dataentry>acl:FromContext#1", "from": "dataentry", "to": "acl",
      "label": "FromContext()", "depth": 1 },
    { "id": "acl>store:AtFaces#1", "from": "acl", "to": "store",
      "label": "AtFaces(faces)", "depth": 2, "fragment": "L1", "async": true, "via": "go" }
  ]
}
```

- A participant is a type ID, a component ID, or a free name for an outside
  actor (`Caller`, `Browser`, `Cron`).
- `depth` is the call depth. A message at depth `d + 1` directly after one at
  depth `d` is a call made inside it.
- `fragment` names the innermost loop or alternative the message is in.
- `async` marks a call on another process, goroutine, task or queue. `via`
  says how.
- `outcome` marks a call that may have refused: `raise`, `error`, `false`,
  `halt`, or `status:<code>` for a 4xx response. Leave it out for any other
  return. It is the kind of the return value, never the value.
- `route` and `span` mark a call to another process over HTTP. See
  [Calls between processes](#calls-between-processes).
- Labels must not contain argument values. Traces can carry secrets.

### Calls between processes

A browser app calls its server over HTTP. A sequence shows that call as one
message, the hop:

```json
{ "id": "web/api>Server:fetch#1", "from": "web/api", "to": "Server", "label": "fetch()",
  "depth": 1, "async": true, "via": "http",
  "route": { "method": "GET", "path": "/api/v1/orders/{id}" }, "span": "4f2a9c" }
```

- `async` is true and `via` is `"http"`.
- `route.method` is the HTTP method in upper case. Leave it out when it is
  not known.
- `route.path` is the path pattern, not the requested URL. Write a path
  parameter as `{id}`. archplan also reads `:id`, `${id}` and `[id]`. A
  literal path, such as `/api/v1/orders/T-1`, also works, but it can hold
  ids and should be avoided.
- `to` is the server's component when the client knows it. Otherwise it is
  a free name, such as `Server`.
- `span` is an id that the client and the server both record for this
  request, for example from an `X-Seqtrace-Span` header. It is only needed
  when the two traces are separate.

A hop can reach the server's code in three ways.

1. **One trace.** The trace already holds the server's calls: they follow
   the hop, one level deeper. The first of them calls the handler.
2. **Two traces, joined by span.** The server's trace is a sequence of its
   own. Its first message has the same `span` as the hop. After the export,
   archplan moves the server's messages under the hop and removes the
   server's sequence. The client's sequence gets `"stitched": ["<server
   sequence id>"]`. The server's first message now comes from the hop's
   `to`, and message ids are numbered again.
3. **The route only.** There is no server trace. archplan finds the route
   in [`routes`](#routes) that matches the hop's method and path. It adds a
   message under the hop that calls the route's handler, marked
   `"joined": "route"`.

In each case, archplan sets the hop's `to` to the route's component when it
was a free name, and adds `"handler": "<handler id>"` to the hop. The
security detectors and the blast radius then treat the browser call as a
call of the server's entry point: its components and files count as part
of the entry point's path.

A participant that names a folder or file of the repository, such as
`frontend/src/api`, becomes the component that owns it.

## signatures

Optional. Functions and methods with their parameters, for detectors that
look for concepts the code passes around without a type of their own.

```json
"signatures": [
  { "id": "internal/order.Service.Ship", "component": "order", "file": "internal/order/service.go",
    "line": 40, "name": "Ship", "params": [{ "name": "status", "type": "string" }],
    "returns": "error", "public": true }
]
```

- `id` is the type or module ID, a dot, the function name (with the arity in
  Elixir: `MyApp.Orders.ship/2`).
- `type` and `returns` are free text in the source language, or `null` when
  unknown.
- `public` is false for private functions and methods (`defp`, a leading
  underscore, `private`, or a function a TypeScript module does not export).

## literals

Optional. String values that act like enum members: the code compares them,
switches on them, assigns them to a field, uses them as a default or passes
them as a keyword argument. In Elixir, atoms count too, written without the
colon.

```json
"literals": [
  { "value": "paid", "component": "billing", "file": "billing/models.py", "line": 12,
    "context": "compare", "field": "status" },
  { "value": "draft", "component": "orders", "file": "orders/views.py", "line": 30,
    "context": "arg", "field": "status", "target": "filter" }
]
```

- `context` is `compare` (`==`, `===`, `!=`, `in` a literal list or set),
  `case` (switch, case or match arms), `head` (a value an Elixir function
  head matches on, such as a message or event name), `assign` (a field,
  attribute, object or map key), `default` (a parameter default) or `arg` (a
  keyword argument).
- `field` names what the value is compared with or assigned to: a variable,
  field or key name.
- `target` names the function a default, keyword argument or function head
  belongs to, such as `handle_event/3`.
- Exporters keep only values without whitespace and at most 64 characters,
  at most 200 per file, and leave out test files.

## routes

Optional. The HTTP routes the server serves, and the function that serves
each one. archplan uses them to join a browser's call to the server code
(see [Calls between processes](#calls-between-processes)).

```json
"routes": [
  { "method": "GET", "pattern": "/api/v1/orders/{id}", "handler": "internal/api.(*App).getOrder",
    "component": "api", "file": "internal/api/orders.go", "line": 40 }
]
```

- `method` is the HTTP method in upper case, or `*` when the route takes
  any method.
- `pattern` is the path pattern. `{id}` matches one path segment and
  `{rest...}` the rest of the path. A pattern that ends in `/` matches
  every path below it, as in Go's `ServeMux`. `:id`, `*` and `**` work too.
- `handler` is the id of the function that serves the route, as in
  `signatures` or `declarations`. Leave it out when the handler has no id,
  such as an anonymous function.
- `file` and `line` are where the handler is declared, or where the route is
  registered when the handler is not known.
- When several routes match a call, the most specific one wins: literal
  segments count more than parameters, and a route with a method more than
  one with `*`.

## enums

Optional. Declared value sets: enums, Django choices, TypeScript unions of
string literals, Ecto.Enum fields and Elixir `@type` unions of atoms.

```json
"enums": [
  { "name": "OrderStatus", "component": "orders", "file": "orders/models.py", "line": 8,
    "values": ["draft", "paid", "sent"] }
]
```

A union on a field or parameter is named after its owner: `Order.status`.

## schema

Optional. The database tables, which component owns each table, and which
components read or write it.

```json
"schema": {
  "sources": [{ "kind": "django" }, { "kind": "sql", "path": "db/structure.sql" }],
  "tables": {
    "orders": {
      "columns": [
        { "name": "id", "type": "bigint", "null": false, "default": "autoincrement" },
        { "name": "state", "type": "varchar(10)", "null": false, "default": "draft",
          "values": ["draft", "paid"] },
        { "name": "customer_id", "type": "bigint", "null": false }
      ],
      "pk": ["id"],
      "fks": [{ "columns": ["customer_id"], "to": "customers", "to_columns": ["id"],
                "on_delete": "CASCADE" }],
      "indexes": [{ "columns": ["customer_id", "state"], "unique": false }],
      "checks": ["state_ok: state IN ('draft', 'paid')"],
      "comment": "One row per checkout."
    }
  },
  "enums": { "order_state": ["draft", "paid"] },
  "maps": { "orders": { "type": "shop.models.Order", "component": "shop", "via": "type" } },
  "access": [
    { "component": "billing", "table": "orders", "mode": "write", "weight": 2,
      "evidence": ["billing/pay.py:30", "billing/refund.py:12"] }
  ]
}
```

- `sources` lists where the tables came from. `kind` is `django`, `ecto`,
  `rails`, `prisma`, `sql`, `sqlite` or `postgres`. A source that was found
  but not used has a `skipped` reason, such as a `structure.sql` that is
  older than the migrations.
- A table key is the table name. A table outside the default schema keeps
  its schema prefix: `audit.events`.
- `type` is the column type as the source writes it, in lower case.
  `values` lists the allowed values: a database enum, a check constraint
  such as `IN ('a', 'b')`, Django `choices`, `Ecto.Enum` or Rails `enum`.
- `maps` gives each table an owner `component`. `type` is the ORM type that
  maps the table, when the model has it. `via` says how the owner was found:
  `type` (the ORM type's component), `file` (the component of the file that
  declares the table), `writes` (the component whose code writes it most) or
  `join table` (the owner of the table that declares the many-to-many
  relation). A table without an owner has no entry.
- `access` lists who uses a table. `mode` is `orm` (a file refers to the
  table's ORM type), `write` (that file also creates, updates or deletes
  rows of the type) or `read` and `write` from raw SQL (`FROM`, `JOIN`,
  `INSERT INTO`, `UPDATE`, `DELETE FROM`). `weight` is the number of files.
  Test factories, seeds and fixtures do not count as writers.

When an exporter writes no `schema` section, archplan adds one after the
export, from the sources it finds in the repository without a database:
Django models, Ecto schemas (with `priv/repo/migrations` for the columns
the schemas leave out), `db/schema.rb`, `schema.prisma`, and `schema.sql`
or `structure.sql`, which can be `pg_dump --schema-only` output. A live
database is read only on request, with `archplan schema --sqlite FILE` or
`archplan schema --postgres --url-env VAR`.

## vocabulary

The domain language of the code: which component owns which concept, and
where code names a concept it does not own.

```json
"vocabulary": {
  "concepts": {
    "cart-item": { "name": "Cart item", "owner": "cart", "terms": ["cart item", "basket"],
      "kind": "domain", "definition": "One product line in a cart.", "source": "glossary" },
    "stripe": { "name": "stripe", "owner": "stripe", "terms": ["stripe"], "kind": "vendor", "source": "vendor" }
  },
  "components": {
    "cart": { "file": "internal/cart/GLOSSARY.md", "uses": ["product"], "avoid": ["stripe"],
      "suggested": [{ "term": "voucher", "score": 4.2 }] }
  },
  "usage": [
    { "component": "cart", "concept": "stripe", "idents": 3, "strings": 1, "comments": 2,
      "evidence": ["internal/cart/pay.go:12"] }
  ]
}
```

- A concept has one `owner`, a component ID. `terms` are the words code uses
  for it; the first is usually the name.
- `kind` is `domain`, `vendor` (named after third-party code) or `technical`.
- `source` says where the concept came from: `glossary` (written by a
  developer), `vendor` (derived from an external component) or `suggested`
  (proposed by a tool, not yet accepted).
- `components` holds per-component glossary data. `uses` lists concepts of
  other components the component may name. `avoid` lists concepts it must not
  name. `suggested` lists frequent words that are not a concept yet, with a
  relevance score.
- `usage` counts how often a component names a concept it does not own, per
  channel: identifiers, string literals and comments. A term matches a run of
  words, so `cartItems` and "cart items" both match the term `cart item`.
  `evidence` lists some sites as `file:line`.

When an exporter writes no `vocabulary` section, archplan adds one after the
export. It reads each component's `GLOSSARY.md`, ranks `suggested` terms by
tf-idf over the component's files, and counts `usage`.

An extractor reports usage; the reader decides which usage is a leak. A
component's use of a concept is:

1. fine when the component owns the concept;
2. a leak when the concept is in the component's `avoid`;
3. fine when the concept is in the component's `uses`;
4. otherwise a leak when the policy does not allow a dependency from the
   component to the concept's owner.

## declarations

Facts a person decides, read from the code by the annotators after every
export (see [`annotator-plugins.md`](annotator-plugins.md)). Exporters do
not write this section.

```json
"declarations": [
  { "target": { "kind": "component", "id": "billing" }, "key": "zone", "value": "internal",
    "file": "billing/GLOSSARY.md", "line": 2, "annotator": "builtin" },
  { "target": { "kind": "entrypoint", "id": "ops.views.health" }, "key": "auth", "value": "none",
    "reason": "load balancer health check", "at": { "file": "ops/views.py", "line": 9, "name": "health" },
    "file": "ops/views.py", "line": 8, "annotator": "builtin" }
]
```

- `target.kind` and `key` form the key: `component.zone`, `function.guard`
  or `entrypoint.auth`.
- A function or entry point id is the model's id from `signatures` or
  `types`, or `<file>:<name>` when the model has none.
- `at` is where the target is declared. `file` and `line` are where the
  declaration is written.
- `annotator` is the annotator that read it.

## notes

Review notes keyed by target: `type:<id>`, `comp:<id>`, `msg:<sequence>:<id>`.
Notes in the target model become open questions in the plan.

## Mapping per language

| | Go | Elixir | Python | TypeScript |
|---|---|---|---|---|
| component | package, or a go-arch-lint component | OTP app, or a `boundary` | package | directory under `src`; npm packages as external components |
| dependencies | `go list -json` imports | `mix xref graph` | `grimp` / import graph | TypeScript compiler API: imports, re-exports, `import()` |
| policy | `.go-arch-lint.yml`, depguard | `boundary` deps and exports | import-linter contracts | dependency-cruiser `forbidden` rules |
| types | `go/types` | structs, `@type`, behaviours as interfaces | classes, dataclasses, `Protocol` and ABCs as interfaces | classes, interfaces, type aliases, enums, exported functions; React components as functions, Vue and Svelte files as modules |
| implements | structural (`types.Implements`) | `@behaviour` | base classes, Protocol conformance | `extends` and `implements` |
| sequences | seqtrace, OpenTelemetry | `:telemetry`, OpenTelemetry | OpenTelemetry, `sys.setprofile` | OpenTelemetry |
| routes | `net/http` `ServeMux` `Handle` and `HandleFunc` | | | |

The `lite` exporter (`archplan export-lite`) covers Java, Kotlin, Scala, C#,
PHP, Ruby, Rust and Swift with regular expressions:

- **Components** are the declared package or namespace (Java, Kotlin, Scala,
  C#, PHP), else the folder (Ruby, Rust, Swift).
- **Types** are class, interface, record, enum, struct, trait, protocol,
  object and module declarations. Fields come from simple member
  declarations and constructor parameters.
- **File references** come from imports resolved to project files, and from
  type names used in a file that its own package or an imported package
  declares.
  - Ruby: `require`, `require_relative` and Zeitwerk naming.
  - PHP: `use` and the PSR-4 map in `composer.json`.
  - Rust: `mod` and `use crate::`, `self::`, `super::` and workspace crate
    names.
  - Swift: types of the same target, and of targets the file imports.
- **Limits:**
  - It misses fully qualified names in code, reflection, configuration-based
    wiring, macros and generated code, and Ruby constants found only through
    nesting.
  - A local name that equals a type name in scope counts as a reference.
  - It has no methods and no sequences.

Use it to get started. Use a compiler-based exporter where precision
matters.

OpenTelemetry spans map to sequences in every language: a span is a message
from the parent span's service or component to its own, and the span tree
gives the depth.
