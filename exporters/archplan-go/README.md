# archplan-go

archplan-go reads a Go module and writes its architecture as an `archmodel/1`
JSON file for archplan. It works on any Go module.

```bash
go run github.com/vloothuis/archplan/exporters/archplan-go@latest -o arch.archmodel.json
go run /path/to/archplan/exporters/archplan-go -C ~/src/myservice -o myservice.archmodel.json
```

To keep a binary, build it with an explicit name:

```bash
go build -o archplan-go github.com/vloothuis/archplan/exporters/archplan-go
```

`go install github.com/vloothuis/archplan/exporters/archplan-go@latest` installs it as `archplan-go`.

## What it emits

| Section | Source |
|---|---|
| `source` | `go.mod`, and the git remote, commit and dirty state when the module is in a git repository |
| `components` | One component per package, named by its module-relative path. With `-depth n`, packages are grouped by their first `n` path segments. With a go-arch-lint config, its components and vendors. |
| `layers` | Section comments in the `components` block of the go-arch-lint config; none without it |
| `dependencies` | Imports between components, counted per file, with up to three `file:line` sites each |
| `policy` | Without go-arch-lint: default `allow` and no rules. With it: `deps`, `commonComponents` and `commonVendors` as allow rules over a default of `deny`. |
| `files` | Per source file, the other module files it refers to: the files that declare the identifiers it uses, from its own package and from the packages it imports |
| `types` | `go/types`: structs, interfaces, enums, function types and aliases, with fields, methods, `implements` and `uses` |
| `signatures` | Every function and method except `init`, with its parameters, results and whether it is exported |
| `literals` | String values the code compares (`==`, `!=`), switches on (`case`) or assigns to a field or key |
| `routes` | Routes registered on a `net/http` `ServeMux` with `Handle` or `HandleFunc`, and the function that serves each one |
| `sequences` | seqtrace `steps.json` files passed with `-steps` |
| `vocabulary` | `GLOSSARY.md` per component, one concept per vendor, where code names another component's concept, and suggested terms |

With a go-arch-lint config, a package that no component matches still gets a
component, in the layer `unmapped`, and the tool prints a warning.

Type IDs are the module-relative package path, a dot and the type name, such
as `internal/store.Store`. Types in the module's root package use the name
alone.

Signature IDs follow the same rule. A method has its receiver, the way Go
writes it: `internal/store.(*Store).Get` for a pointer receiver,
`internal/store.Value.String` for a value receiver, and `internal/store.New`
for a function. `public` is true when the function, and the receiver type of
a method, are exported.

`source.capabilities` lists the sections the model holds.

### File references

An import of a module package refers to the files of that package that
declare the identifiers the file uses. When none of its identifiers can be
traced to a file, as with a blank import, the import refers to every file
of the package. Uses of identifiers that another file of the same package
declares count too. `line` is the line of the first use. Test files have no
entry.

### Literals

The exporter keeps values without whitespace, of at most 64 characters, and
at most 200 per file. `field` is the name the value is compared with or
assigned to: `status` in `o.status == "paid"`, the switch tag, or the key in
`Order{State: "draft"}` and `m["state"] = "draft"`. Test files are left out.

### Routes

The route pattern must be a constant, such as `"GET /api/v1/orders/{id}"`
or `"GET " + prefix + "/orders"` with `prefix` a constant. The method is `*`
when the pattern has none, and the host is dropped. The handler is the
function or method passed, also through a conversion such as
`http.HandlerFunc(a.get)`. `file` and `line` are where the handler is
declared. For an anonymous function, the route has no handler, and `file`
and `line` are where it is registered. Other routers, such as chi or gorilla,
are not read.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `-C` | `.` | Module root (the directory with `go.mod`) |
| `-o` | stdout | Output file |
| `-archlint` | `.go-arch-lint.yml` if it exists | go-arch-lint v3 config, relative to `-C`; `none` ignores it |
| `-exclude` | | Module-relative directories to skip; comma-separated and repeatable. Added to go-arch-lint's `exclude`. |
| `-depth` | `0` | Group packages without a go-arch-lint component by this many leading path segments; `0` is one component per package |
| `-steps` | | seqtrace `steps.json` to add as sequences; repeatable |
| `-no-vocab` | false | Skip the `vocabulary` section |
| `-tags` | | Build tags for `go list` |
| `-unexported` | false | Also export unexported types and methods |

Arguments are package patterns, relative to `-C`. The default is `./...`.

The tool runs `go list -deps -export`, which compiles the packages into the
build cache. The first run on a large module can take minutes.

## Examples

```bash
# Whole module, one component per package.
archplan-go -o model.json

# Group by top-level directory, skip generated code.
archplan-go -depth 2 -exclude internal/gen,tools -o model.json

# Use go-arch-lint components and rules, add traced sequences.
archplan-go -C ~/src/rela -steps steps.json -o rela.archmodel.json

# Only the internal packages, without vocabulary.
archplan-go -no-vocab ./internal/...
```

## Vocabulary

A component can declare its domain language in `GLOSSARY.md` in its directory.
The YAML front matter lists the concepts it owns, the concepts of other
components it may name (`uses`), and the concepts it must not name (`avoid`).
Lines of the form `**Name**: definition` define concepts.

```markdown
---
owns:
  cart: [basket]     # concept name: extra terms
  cart item: []
uses: [product]
avoid: [stripe]
---
**Cart**: the items a shopper intends to buy.
```

Each go-arch-lint vendor becomes a concept named by its ID and the last
segment of its import path. The exporter scans identifiers, string literals
and comments, and counts where a component names a concept it does not own. It
also suggests up to ten terms per component by tf-idf over declared names.

## Sequences

`-steps` reads the `steps.json` that seqtrace writes: a JSON array of
scenarios, each with a `name`, a `handler` and a `diagram` of participants and
nested calls. A participant is a package path; it maps to the component that
holds the package. Argument values are dropped, because traces can hold
secrets.

A scenario with `"guards": "off"` ran with its guards switched off, such as
a test against a server without an access policy. The exporter copies it to
the sequence, and the security detectors that judge guards leave the
sequence out.

A step can have these fields besides `from`, `to`, `fn`, `args` and
`children`. The exporter copies each one to the message.

| Field | Meaning |
|---|---|
| `via` | How the call reached its callee: `go` for a new goroutine, `http` for a request to another process. A step with `via` becomes an `async` message. |
| `outcome` | The kind of a refusing return: `error`, `false`, or `status:<4xx>` on the root step of a request that was refused. Leave it out otherwise. |
| `route` | For a step with `via: "http"`: `{"method": "GET", "path": "/api/v1/orders/{id}"}`. The path is the route pattern, not the URL. |
| `span` | The id the client and the server both record for one request, such as the value of an `X-Seqtrace-Span` header. |

### Client and server in one trace

A trace can follow a request from the browser into the server. Put the
server's call tree under the client's fetch step: the fetch step has
`via: "http"` and a `route`, and its child is the server's root step. The
server's root step comes from the fetch step's `to`.

```json
{"from": "frontend/src/api", "to": "dataentry", "fn": "fetch", "via": "http",
 "route": {"method": "GET", "path": "/api/v1/orders/{id}"}, "span": "4f2a9c",
 "children": [
   {"from": "dataentry", "to": "dataentry", "fn": "(*App).getOrder", "args": ["w=...", "r=..."],
    "children": [ ... ]}
 ]}
```

Client participants, such as `frontend/src/api`, map to no Go package and
keep their name. archplan later maps a name that is a repository folder to
the component that owns it, for example a component of a TypeScript
exporter that runs beside this one.

### Client and server in two traces

When the client and the server record separate traces, give the client's
fetch step and the server's root step the same `span`. Pass the server's
`steps.json` to this exporter. The client's trace can come from another
exporter, such as `archplan export-ts`, which reads
`.archplan/sequences.json`. archplan joins the two after the export (see
"Calls between processes" in
[`archmodel.md`](../../src/archplan/format/archmodel.md#calls-between-processes)).

archplan joins them, not this exporter: the client's participants are
components of the TypeScript exporter, which this exporter does not know.
After the merge of both models, archplan knows the components of both, so
the joined sequence names the right components on both sides. The join
works the same when one exporter writes both sequences.

## Limits

- `implements` is structural: a type implements an interface when its method
  set satisfies it.
- Literals and routes come from the syntax: a value built at run time is not
  seen.
- Generic instantiations point at the generic type.
- Only one go-arch-lint config is read.
