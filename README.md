# archplan

Plan architecture changes together with an agent.

archplan exports a model of your code: components, dependencies, types, call
sequences and a glossary. You open the model in a browser editor and change it
into the architecture you want. The editor turns the difference into a change
plan. You comment on any component, type, dependency, call or plan task, and
Claude answers in the same thread. Claude can propose changes to the target
model, and after it changes the code the plan shrinks by what it did.

## Install

### As a Claude Code plugin (recommended)

The plugin includes the `archplan` command, the MCP server, a skill, a monitor
and hooks. It needs Python 3.9 or later and nothing else.

```bash
claude plugin marketplace add vloothuis/archplan
```

```bash
claude plugin install archplan@archplan
```

From a local checkout, use the path instead: `claude plugin marketplace add
/path/to/archplan`. Add `--scope project` to both commands to share the plugin
with everyone who works in a repository.

### As a command only

```bash
pipx install git+https://github.com/vloothuis/archplan
```

Or use `uv tool install`, or put `bin/archplan` from a checkout on your PATH.
To give a Claude Code session the MCP tools without the plugin:

```bash
claude mcp add archplan -- archplan mcp
```

### With OpenAI Codex

Codex uses the same command, MCP server, skill and hook. It has no monitors,
so the agent learns about new comments from the hook at each prompt, or by
calling `wait_for_comments`.

1. Install the `archplan` command as described above, and clone this
   repository for the skill:

   ```bash
   git clone https://github.com/vloothuis/archplan ~/.local/share/archplan
   ```

2. Add the MCP server. `wait_for_comments` can block for up to 10 minutes,
   so raise the tool timeout from the default of 60 seconds:

   ```bash
   codex mcp add archplan -- "$HOME/.local/share/archplan/bin/archplan" mcp
   ```

   Then add this to `~/.codex/config.toml`, under the new
   `[mcp_servers.archplan]` table:

   ```toml
   tool_timeout_sec = 660
   ```

3. Link the skill into your Codex skills folder:

   ```bash
   mkdir -p ~/.agents/skills && ln -s ~/.local/share/archplan/skills/archplan ~/.agents/skills/archplan
   ```

4. Add the hook to `~/.codex/hooks.json`. It is silent outside archplan
   projects:

   ```json
   {
     "hooks": {
       "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/share/archplan/bin/archplan hook", "timeout": 10}]}],
       "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.local/share/archplan/bin/archplan hook", "timeout": 10}]}]
     }
   }
   ```

   Codex runs a new hook only after you trust it. Run `/hooks` in Codex to
   review and trust it.

In Codex, start with `$archplan` instead of `/archplan`. Run `git pull` in
`~/.local/share/archplan` to update the skill.

### Updates

Every push to `develop` is a release, so archplan changes often. Each running
process knows its build id: the version plus a hash of the package's code
files. The editor server reports it at `/api/status`.

- **The editor server** restarts itself when its own code changes on disk, for
  example after a `git pull` in a checkout. It waits until the files have been
  unchanged for about 2 seconds and the new code imports without errors. A
  broken or half-written checkout does not stop the running server. It keeps
  its port, and it lets running requests finish first.
- **`archplan up`** replaces a running server for the same project when that
  server runs other code, for example after a plugin update. It keeps the
  port.
- **The editor tab** reloads itself when the server runs new code. When you
  are typing, it shows a bar with a Reload button instead.
- **The MCP server** does not restart itself. Claude Code owns its connection,
  and a new process cannot take over that session safely. When newer code is
  on disk, the next tool result says so once. Restart Claude Code to load it.

Set `ARCHPLAN_NO_RELOAD=1` to turn off the restart and the MCP notice.

## Start

In Claude Code, run `/archplan` in a repository. The skill sets up the project,
exports the model and opens the editor. By hand:

```bash
archplan init      # creates .archplan/config.json; picks an exporter
archplan export    # code -> .archplan/model.archmodel.json
archplan up        # starts the editor in the background and prints its URL
```

## How you and Claude work together

| You, in the editor | Claude |
|---|---|
| Edit the target: move types, add components, forbid dependencies, rename terms | Sees the plan with `get_plan`, and each item with `get_item` |
| Comment on an item, or shift-click several items and comment on the group | Is told at once: the plugin's monitor sends each comment, and a hook lists waiting threads at every prompt |
| Read replies: orange **decision** threads need your choice (click an option); **feedback** threads only need reading | Replies with `reply`, marks a reply as a decision only when it is blocked on you |
| Preview a suggestion and accept all, some or none of its changes | Proposes a refactor of the target with `edit_target`; learns the outcome from the monitor |
| Review the architecture profile: units, file roles and the rules between them | Proposes a profile with `update_profile`; checks code against it with `get_architecture` |
| Work through the findings: discuss one, dismiss it with a reason, or accept it as is | Lists them with `get_findings`; labels the ones a heuristic cannot settle with `label_finding` |
| Ask Claude to draft the per-component glossaries | Reads ranked candidate terms with `get_vocabulary_candidates`; proposes concepts, definitions, `uses` and `avoid` with `propose_glossary` |
| Ask for refactor proposals, or switch on auto mode; then plan, discuss or reject each one | Reads ranked opportunities with `get_refactor_brief`; proposes small refactors with `propose_refactor`, which archplan verifies |
| Look at hotspots and components that change together | Finds hidden coupling in the git history with `get_hotspots` and opens a thread on it |
| Lock in a settled thread as a planned refactor, then go on with the open discussions | Leaves planned refactors alone; they do not wait on it |
| Order the planned refactors and send them all as one handover: all in one go, or ask for a split first | Reads them with `get_handover`; for a split, proposes batches with `propose_batches` |
| Approve or edit the proposed batches | Works refactor by refactor: `set_refactor_state`, code, `refresh_model`, a reply in the thread |
| Watch the plan shrink | Implements a task, then calls `refresh_model`; the editor reloads the code model and rebases your target onto it |

Everything is stored in `.archplan/` in the repository:

| File | What it holds |
|---|---|
| `config.json` | export command, model path, paths to exclude, components and layers, port, auto suggestion mode |
| `model.archmodel.json` | the as-is model, written by the exporter |
| `target.archmodel.json` | the target model |
| `base.archmodel.json` | the as-is model the target was last edited against, for rebasing |
| `state.json` | target revision and the plan's intent |
| `plan.md`, `plan.json` | the change plan, written by the editor |
| `threads.json` | the comment threads |
| `suggestions.json` | target changes the agent proposed, and what you decided, with your reason for a rejected refactor |
| `handovers.json` | planned refactors you sent to the agent, its batches and progress |
| `architecture.json` | the architecture profile: units, roles, rules and layers |
| `baseline.json` | findings you accepted or dismissed, Claude's labels on findings, and the ratchet |
| `vocabulary.json` | terms you told archplan to ignore as glossary candidates |
| `vocab-cache.json` | the cached word counts per file |
| `activity.json` | what the agent did last, shown in the editor |
| `hotspots.json` | the cached git history analysis |
| `suggest-state.json` | when archplan last asked Claude for refactor proposals |

Commit `.archplan/` to share the target, plan and threads with your team.
`server.json`, `activity.json`, `hotspots.json`, `vocab-cache.json`,
`suggest-state.json` and the logs are ignored.

In a message, `@[Label](anchor)` mentions a model item, for example
`@[Orders](comp:shop.orders)`. The editor and the agent both use this form.

## Commands

| Command | What it does |
|---|---|
| `archplan init [--export CMD]` | set up `.archplan/` in this repository |
| `archplan export` | run the export command |
| `archplan trust` | approve a cloned repository's export command |
| `archplan up [--open]`, `archplan down` | start or stop the editor in the background |
| `archplan serve` | run the editor in the foreground |
| `archplan status` | editor URL, model size, plan size, threads waiting |
| `archplan threads [--waiting agent]` | print threads |
| `archplan mcp` | the MCP server, on stdio |
| `archplan watch` | one line per human comment, plan change or decided suggestion |
| `archplan hotspots [--since DAYS] [--json]` | components that change most, and pairs that change together without a dependency |
| `archplan blast [--diff REF] [FILE ...] [--json]` | what traced entry points reach, the entry points a change touches, and the security tests to add |
| `archplan profile [infer\|show] [--json]` | propose an architecture profile, or show how the code conforms to it |
| `archplan profile list`, `archplan profile use NAME` | the framework profiles that match, with their fit; propose one of them |
| `archplan check [--format github] [--all] [--update-baseline] [--tighten] [--loosen REASON] [--export] [--min-weight N] [--json]` | new findings and the ratchet; exit 1 when there are new findings or a ratchet metric got worse, 2 on errors |
| `archplan detectors [--json]` | each detector: group, needs, whether it is on, missing data and its finding count |
| `archplan detectors enable\|disable\|trust\|run ID [--json]` | switch a detector on or off, approve an external one, or run one |
| `archplan annotators [--json]` | which annotator reads and writes each declaration key, and whether it is approved |
| `archplan annotators trust ID`, `archplan annotators read [--json]` | approve a project annotator; list the declarations in the code |
| `archplan annotators write FILE\|- [--apply] [--json]` | write changes to declarations as a patch, checked by reading it back; `--apply` applies it |
| `archplan schema [show\|tables] [--json] [--refresh]` | the database tables, their owner components, and foreign keys across components |
| `archplan schema --sqlite FILE`, `archplan schema --postgres --url-env VAR` | read the tables from a live database, only when you ask |
| `archplan glossary candidates [--component ID] [--top N] [--json]` | the words that characterise each component, ranked by tf-idf |
| `archplan hook` | hook: report threads that wait on the agent |
| `archplan export-python` | the built-in Python exporter |
| `archplan export-elixir`, `archplan trace-elixir` | the Elixir exporter and test tracer |
| `archplan export-ts [-C DIR]` | the TypeScript and JavaScript exporter (needs `node`) |
| `archplan export-lite [DIR] [--language L]` | the regex-based exporter for Java, Kotlin, C#, Ruby, PHP, Rust, Swift and Scala |
| `archplan exporters [--json]` | the exporters that match this repository, whether they are installed, and install hints |
| `archplan export-check FILE [--root DIR]` | check a model file against the exporter contract |

## Hotspots

`archplan hotspots` reads the year of git history up to the HEAD commit
(`--since DAYS` to change it). A hotspot is a component that changes in many commits. Two
components are coupled when they change in the same commits. The degree is
the number of shared commits divided by the commits of the less active of the
two. A coupled pair with no dependency in the model is hidden coupling: the
code's imports do not explain why they change together.

The analysis maps each changed file to a component with the model's `match`
patterns and `path`. Commits that touch more than 50 files are skipped as bulk
changes. Only git runs, with hooks, fsmonitor and signature checks switched
off. The result is cached per HEAD in `.archplan/hotspots.json`.

The same pass also counts co-change per file, for the files in the model's
`files` section. Lockfiles, vendored and generated code, migrations and
translations are left out. A commit with more than 25 files, not counting those, is skipped.
A file pair is kept when it shares 3 or more commits and a degree of 0.3 or
more; the 600 strongest pairs are cached with their commits and the commit
subjects. The `history.modularity_violation` detector reads them.

## Architecture profile

The profile in `.archplan/architecture.json` describes the shape every unit
of the code should have. A unit is a folder that matches a glob, such as
`core/systems/*` or a Django app. A role is a kind of file inside a unit,
found by file name patterns such as `*_view.ex` or `models/**`. Rules say
which roles may call which (`calls`), or that one file needs a partner
(`pair`, for example `<stem>_view.ex -> <stem>_view_builder.ex`). Layers
order the components from top to bottom.

`archplan profile infer` proposes a profile. It first tries the framework
profiles in [`src/archplan/contrib/profiles`](src/archplan/contrib/profiles):
Django, FastAPI, Flask, Phoenix, Rails, Laravel, Spring, ASP.NET Core, Go
(standard and hexagonal layouts), React, Next.js, Angular, Vue, NestJS and
Express. Each one says which files and manifest dependencies identify the
framework. When none fits, or the inferred profile fits much better, it looks
for file name shapes that recur in at least 30% of the units, and keeps the
call rules that hold for at least 90% of the file references.
`archplan profile list` shows the matches and their fit, and
`archplan profile use NAME` proposes one. A project can add or replace
profiles in `.archplan/profiles/`; see
[`src/archplan/contrib/README.md`](src/archplan/contrib/README.md). A proposal never overwrites a locked
profile. `archplan profile` shows the role coverage and the rule breaks.

Rules need the file references in the model's `files` section. The Python,
Elixir, TypeScript and lite exporters write it.

## Findings and `archplan check`

A finding is something archplan notices in the model. Each one has a
strength (strong, medium or weak) and a basis, such as "19 of 20 units
follow it".

| Kind | What it reports |
|---|---|
| `profile.rule` | a unit breaks a calls rule of the architecture profile |
| `profile.pair` | a unit has files without their partner, such as a view without a view builder |
| `structure.cycle` | two or three components that depend on each other in a cycle, with the cheapest cut: the dependency with the fewest references |
| `structure.tangle` | four or more components that all reach each other, as one finding named after the hub. The evidence lists a small set of cuts that breaks every cycle. When the tangle grows past its baselined size, it is new again |
| `layers.upward` | a component depends on a higher layer of the profile |
| `layers.policy` | a dependency the target's policy forbids |
| `vocabulary.leak` | a component uses a term of a concept it should not (needs glossaries) |
| `types.fields` | a type with 25 or more fields, 3 or more groups of related fields, or 6 or more boolean flags |
| `types.synonyms` | types in different components that share 70% or more of their field names |
| `history.coupling` | components that change together without a dependency (from the cached `archplan hotspots`) |
| `history.modularity_violation` | files in different components that change together while neither refers to the other: no import, call or type reference, also not through a package's `__init__.py` or `index.ts` (needs `files` and the cached `archplan hotspots`). After Xiao, Cai and Kazman (DRSpaces, DV8). One finding per component pair, or per file name repeated across components (such as `api/schema.py`). It weighs shared commits, degree and bug-fix commits on the files; `fix_words` lists the words that mark a commit subject as a bug fix. Unlike `history.coupling`, it works per file and also reports files whose components do depend on each other |
| `concepts.state_flags` | types that keep their state in 3 or more boolean flags (`is_*`, `has_*`, a flag next to a `*_at` timestamp); one finding per component |
| `concepts.data_clump` | the same 3 or more field names in types of 2 or more components, such as street, city and zip |
| `concepts.raw_ids` | fields such as `invoice_id` that hold another component's concept as a plain string or number |
| `concepts.untyped_bag` | map, dict, `Any`, `interface{}` or JSON fields on types that other components use |
| `concepts.units_in_names` | one quantity named with different units across components, such as `amount` and `amount_cents` |
| `concepts.string_enum` | a field compared with several string values across components (needs `literals`) |
| `concepts.bool_params` | components whose public functions take boolean flags (needs `signatures`) |
| `boundaries.stability` | components in the zone of pain (stable and concrete) or of uselessness (abstract and unused), from Martin's metrics |
| `boundaries.balance` | dependencies out of balance, after Khononov's balanced coupling: strong coupling (`min_strength`, default `functional`: the two change together; `intrusive`: the downstream writes the upstream's tables or reads them with SQL; `model`: it uses the upstream's concrete types or queries its tables through their ORM types) over a long distance (`far` folder steps, plus `owner_distance` for different owners) on a volatile upstream (among the `volatile_share` most changed components, with `min_commits` or more). Needs the cached `archplan hotspots`. The strength, distance and volatility of every dependency are in the result's extras |
| `boundaries.unused_public` | files with public types in a component others depend on, which only that component uses (needs `files`) |
| `evolution.ownership` | components with many active authors and no clear owner, and authors spread over many components (cached hotspots) |

Each finding has a weight from 0 to 100: how much it matters. A detector
starts from a base for its kind and adds factors: how many components,
files and sites are involved, one occurrence or a series, where a term
occurs, how many components depend on the ones involved, how often they
change (from the cached `archplan hotspots`) and whether a dependency points
up the layers. `weight_basis` lists each factor with its effect. The
strength follows from the weight: strong from 65, medium from 40, else weak.
For a vocabulary leak, a term that only shows up in comments weighs least,
then strings, local names, field names, function names, type names, and the
public API (exported declarations and routes) most. Findings are listed by
weight. The weight is not part of the finding's id, so a dismissed or
accepted finding stays so when its weight changes.

Each finding also says where the editor shows it, in `focus`: a list of
views, the first one the default.

```json
"focus": [{"view": "types", "nodes": ["shop.Order", "billing.Invoice"],
           "fields": {"shop.Order": ["street", "city"]}},
          {"view": "components", "nodes": ["shop", "billing"], "edges": [["shop", "billing"]]}]
```

`view` is `types`, `components`, `roles`, `sequences` or `glossary`.
`nodes` are the model's ids of that view's elements: type, component,
unit, sequence or concept ids. `edges` are pairs of node ids, `fields`
marks fields per type, `files` marks files in the Roles view, and `label`
names the link. A data clump opens the Types view with its types selected
and the shared fields marked; a cycle opens the Components view with its
edges. When a finding has more than one entry, its card has "Show in"
links.

In the editor you discuss a finding, dismiss it with a reason, or accept it
as is. Accepted and dismissed findings go into `.archplan/baseline.json`.
Commit that file. From then on only new findings show.

`archplan check` lists the new findings. It exits with 1 when there are any,
0 when there are none, and 2 on errors. `--update-baseline` accepts all
current findings, for a first run on an existing codebase. `--min-weight N`
leaves out new findings that weigh less than N; set a default with
`"check": {"min_weight": 40}` in `.archplan/config.json`. A GitHub Actions
step for a Python project:

```yaml
- run: pip install git+https://github.com/vloothuis/archplan
- run: archplan export-python -o .archplan/model.archmodel.json
- run: archplan check --format github
```

`--export` runs the configured export command first. It runs only once a
person approved that command with `archplan trust`, so in CI run the
exporter yourself, as above.

### The ratchet

The baseline also keeps a few project-level numbers that may only get
better: the weight of the open findings per group, the size of the largest
tangle, the dependencies inside cycles, the upward layer dependencies and
the propagation cost. `archplan check` fails when one of them got worse,
and names the findings behind it. `--tighten` writes the numbers that got
better; `--update-baseline` does that too. Only `--loosen REASON` writes
worse numbers, and the baseline keeps the reason. See
[docs/ci.md](docs/ci.md) for the metrics, the tolerances and a GitHub
Actions workflow.

### Detectors

Each kind comes from a detector. A detector needs some data in the model,
such as `fields`, `files`, `signatures` or `literals`, or the cached git
history. When the data is not there, the detector does not run, and
`archplan check` says why under "Not checked". `archplan detectors` lists
every detector with its needs and its finding count.

Switch a detector off, or change its settings, in `.archplan/config.json`:

```json
"detectors": {
  "boundaries.unused_public": {"enabled": false},
  "types.fields": {"params": {"large_type": 40}}
}
```

`archplan detectors enable ID` and `archplan detectors disable ID` edit that
file. The editor uses `GET /api/detectors` and `PUT /api/detectors/ID`. The
metrics behind `boundaries.stability` (Ca, Ce, I, A, D per component) are at
`GET /api/metrics`.

An external detector is a command. Put a JSON file in
`.archplan/detectors/`:

```json
{"id": "concepts.money", "name": "Money as float", "group": "concepts",
 "needs": ["fields"], "command": "python3 tools/money.py {ctx}",
 "params": {"limit": 3}, "timeout": 60}
```

archplan writes the model, the settings and the files per component to a
temporary JSON file and puts its path in place of `{ctx}`. The command
prints `{"findings": [{"key", "title", "detail", "strength", "basis",
"components", "files", "evidence", "focus", "weight", "weight_basis"}]}`.
Only `key` and `title` are required. archplan keeps at most 200 findings,
shortens long text, drops paths outside the repository and focus entries
with an unknown view, keeps at most 5 focus entries and 12 weight factors,
limits the weight to 0 to 100, and makes each id from the detector id and
the key. Without a weight, the strength sets it. The command comes from the repository, so
it runs only after a person approves it: `archplan detectors trust ID` in a
terminal. `archplan check` and `archplan detectors run ID` run it; the
editor shows the last result.

## Database schema

After an export, archplan adds a `schema` section to the model when it
finds a database schema in the repository. It needs no database. It reads
Django models, Ecto schemas and `priv/repo/migrations`, `db/schema.rb`,
`schema.prisma`, and `schema.sql` or `structure.sql` (also
`pg_dump --schema-only` output).

Each table gets an owner component: the component of its ORM type, or else
the component whose code writes it most. archplan also records which
components use each table through the ORM, which ones write it, and raw SQL
reads and writes.

`archplan schema` shows the tables per owner and the foreign keys across
components. `archplan schema tables` lists the columns. The editor reads the
same data from `GET /api/schema`.

To read a live database instead, ask for it:

- `archplan schema --sqlite app.db` opens the file read-only.
- `archplan schema --postgres --url-env DATABASE_URL` runs `psql` with the
  URL from that environment variable. archplan never stores or prints the
  URL.

The result is kept in `.archplan/schema-live.json`, so later exports keep
it. `archplan schema --drop-live` forgets it.

The data detectors read the schema:

| Kind | Finds |
|---|---|
| `data.shared_table` | a table that two or more components write |
| `data.cross_fk` | a foreign key from one component's table to another's, with the layer direction when the profile has layers |
| `data.polymorphic` | type and id column pairs, such as `subject_type` and `subject_id` |
| `data.json_bag` | JSON columns, weighted by how many components use the table |
| `data.flag_columns` | three or more boolean or nullable timestamp columns that hold a state |
| `data.repeated_columns` | the same group of columns in three or more tables |
| `data.enum_mismatch` | allowed values in the database that differ from the code's enum or literals |
| `data.orphan_table` | a table without an owner, or without use in the code |

## Refactor proposals

archplan ranks refactor opportunities from what it already knows: new
findings, churn, dependency fan-in and misplaced glossary terms. Findings
about the same component pair or cycle form one opportunity. The value of an
opportunity is its severity times the churn and the fan-in of its
components. Each comes with one to three hints, such as "invert this
dependency" or "split this type by its field groups".

Ask Claude for proposals in the editor. Claude reads the opportunities, reads
the code, and proposes small refactors as target edits with a rationale.
archplan applies each proposal to a copy of the target and recomputes the
cycles, upward layer dependencies and policy breaks. It shows which findings
the proposal fixes and which it introduces. Other fixes are claims.

For each proposal you can:

- **plan** it: the edits go into the target, and a planned refactor holds the
  title and rationale;
- **discuss** it in a thread first;
- **reject** it with a reason. Claude sees the reason and does not propose the
  same idea again.

In auto mode, archplan asks Claude for proposals by itself: after an export
that makes new findings, and when no proposal is pending any more. It asks
at most once an hour, and never while a request is still open.

## Glossaries

Each component can have a `GLOSSARY.md` next to its code. It names the
concepts the component owns, the concepts of other components it may use,
and those it must avoid. archplan reads these files after every export and
adds a `vocabulary` section to the model, unless the exporter already wrote
one. It also counts where a component names a concept it does not own. The
editor shows these uses as leaks.

```markdown
---
owns:
  Cart item: [cart item, basket]
  Payment webhook: [webhook]
uses: [product]
avoid: [stripe]
technical: [Payment webhook]
---
**Cart item**: one product line in a cart.
```

Owned concepts are domain concepts unless `vendor` or `technical` lists them.

archplan helps you write the first glossaries. It counts the words in the
identifiers, strings and comments of each component's files. Words that are
frequent in one component and rare in the others rank highest (tf-idf).
Words in type names get a boost. Claude reads these candidates with their
sample identifiers and the other components that use them. It then proposes
concepts with one-line definitions as a suggestion you can review. Words you
mark as noise go into `.archplan/vocabulary.json`.

## Declarations in the code

Some facts are decisions, not something an exporter can find: the trust zone
a component is in, the components through which others must reach it (its
gateways), which functions are guards (they authenticate, authorize
or validate), which functions are sinks (they write or read data that needs
a permission check first), and which entry points need an authenticated
caller (`auth=required`) or also a check of what that caller may do, such as
read the record the request names (`auth=authz`). These
declarations live in the repository, next to the code. After every export,
archplan reads them into the model's `declarations` section.

By default they are marker comments above a declaration, and `zone:` and
`gateway:` lines in the front matter of a component's `GLOSSARY.md`:

```python
# archplan: auth=none reason="load balancer health check"
def health(request): ...
```

A project can read and write them in its own form instead, such as its
language's annotations or database column comments, with an annotator
plugin in `.archplan/annotators/`. Each key has one owner.
`archplan annotators write` and the agent's `annotate` tool write changes in
a scratch git worktree, read them back to check them, and return a patch;
`--apply` applies it. The contract is in
[`annotator-plugins.md`](src/archplan/format/annotator-plugins.md).

The security detectors read the declarations:

| Kind | Finds |
|---|---|
| `security.unguarded_entry` | a traced entry point whose request passes no guard: strong when it is declared `auth=required`, or `auth=authz` and the request passes no authorization guard; weak when it has no auth declaration. A test that makes several requests has each judged on its own. Needs sequences. |
| `security.unchecked_entry` | an entry point declared `auth=authz` whose code, with the functions of its file that it calls and the file's plugs and filters, names no authorization guard. Static, so it covers entry points no test runs; a traced request that passes the guard clears it. Medium. |
| `security.unguarded_sink` | a trace that calls a write sink before any authorization guard (`guard` or `guard=authz`), strong; or a read sink with no such guard anywhere in the trace, medium. Calls inside a guard are its own. A `guard=authn` guard does not count. Needs sequences. |
| `security.untested_denial` | an entry point declared `auth=required` or `auth=authz` whose traced requests pass the guard it needs, but none shows the guard or the entry point refusing a caller (a call `outcome` such as `raise`, `error`, `halt` or `status:403`, or a test title such as "rejects another user"). Names the guard to make fail and the sinks the request reaches. Medium; weak when the traces record no outcomes. Needs sequences. |
| `security.risk_hotspot` | a component in the top quarter by commits that lies on the traced path of entry points that reach sinks. Its weight follows the riskiest of those entry points (see Blast radius). Needs sequences and the cached `archplan hotspots`. |
| `security.untraced` | a declared guard or entry point that no trace calls, in a component the traces do visit: a guard wired nowhere, or a route nothing reaches. Weak. Needs sequences. |
| `security.open_entry` | an entry point declared `auth=none` without a reason |
| `security.zone_dependency` | a dependency from a more trusted zone to a less trusted one, or one that skips a declared zone |
| `security.gateway_bypass` | a dependency on a component that declares gateways (`gateway: [a, b]`) from a component that is none of them and does not implement its interfaces. Weak; strong when a trace shows the call. |
| `security.unguarded_path` | a dependency path from an entry point's component, through other components, to a `restricted` zone component, on which no component is a guard's, depends on one or is a declared gateway of the target. Static, so it covers code no trace runs. Weak. |

Zones are ordered by the `order` setting of `security.zone_dependency`,
from the least trusted: `internet`, `dmz`, `internal`, `restricted` by
default.

### Blast radius

`archplan blast` lists what each traced entry point reaches: the components
its requests pass, its guards, the sinks it reads or writes and the zones it
crosses. Each entry point gets a risk score. A write sink adds the most,
then a read sink, then `auth=authz`. Gaps add more: no guard on its traced
requests, or no test that shows a refusal. The output ends with the
security tests to add, by risk.

`archplan blast --diff develop` (or `archplan blast FILE ...`) lists the
entry points that the change touches, so you can review those paths and run
their tests first. A file touches an entry point when it declares the entry
point, a guard or a sink on its path, or when it is in a component that the
entry point's requests pass. The agent gets the same from `get_blast_radius`.

With the cached `archplan hotspots`, `security.risk_hotspot` marks the
components that change often (the top quarter by commits, at least 5) and
that lie on paths to sinks. Traces only cover what the tests run, so an
entry point that no test runs has no blast radius.

## Exporters

An exporter writes an [archmodel/1](src/archplan/format/archmodel.md) file.
`archplan init` picks one from the files in the repository:

- **Go** (`go.mod`): [`exporters/archplan-go`](exporters/archplan-go), run with
  `go run github.com/vloothuis/archplan/exporters/archplan-go@latest`. It reads
  `.go-arch-lint.yml` when present, and seqtrace call traces with `-steps`.
  It lists file references and the routes registered on a `net/http`
  `ServeMux`.
- **Python** (`pyproject.toml`, `setup.py`): `archplan export-python`, built in.
  It reads import-linter `forbidden` contracts as rules, and Django model
  fields as type fields.
- **Elixir** (`mix.exs`, in the root or a subdirectory):
  `archplan export-elixir`, which needs `mix`. `archplan trace-elixir` runs
  tests and records call sequences. See [`exporters/elixir`](exporters/elixir).
- **TypeScript and JavaScript** (`package.json` with `tsconfig.json` or
  sources, in the root or a subdirectory such as `frontend/`):
  `archplan export-ts`, which needs `node` and the project's own `typescript`
  package, so run `npm install` first. It follows `tsconfig.json`, including
  `paths` aliases such as `@/`, and reads the `<script>` blocks of Vue and
  Svelte files. Components are folders under `src`; npm packages are external
  components. It runs the project's installed `typescript`, so `archplan
  init` does not approve it: read the command, then run `archplan trust`.

The Go, Python, TypeScript and Elixir exporters also list function signatures,
string literals and enum-like value sets (`signatures`, `literals` and
`enums` in the [format](src/archplan/format/archmodel.md)). Detectors use
them to find concepts the code has no type for.

- **Java, Kotlin, C#, Ruby, PHP, Rust, Swift and Scala**: `archplan export-lite`,
  built in. It reads the source with regular expressions, not a compiler.
  Components are packages, namespaces or folders. It resolves imports to
  files (Java and Kotlin packages, C# `using`, PHP `use` with PSR-4, Ruby
  `require` and Zeitwerk constants, Rust `mod` and `use`) and lists types
  with simple fields. It misses calls and anything a compiler would infer;
  see [its limits](src/archplan/format/archmodel.md).

### Excluding paths

To leave folders or files out of the model, list glob patterns under
`exclude` in `.archplan/config.json`:

```json
"exclude": ["prototype/**", "research/", "**/fixtures"]
```

Patterns are relative to the project root, the folder that holds
`.archplan` (normally the repository root). `**` matches any number of
folders; `*` and `?` match within one name. A pattern that matches a folder
excludes everything in it, so `prototype`, `prototype/` and `prototype/**`
do the same. Patterns are anchored at the root: use `**/fixtures` for a
folder name at any depth.

Excluded files give no components, types, dependencies, files, signatures,
literals or findings. The built-in Python, TypeScript and lite exporters skip
them while reading. For other exporters, `archplan export` removes them from
the model afterwards. Hotspots, the architecture profile, glossary candidates,
schema detection and `archplan init`'s exporter detection skip them too.
`archplan status` and `archplan exporters` list the active patterns. Changing
`exclude` does not need a new `archplan trust`: it only narrows what is read.

### Your own components and layers

An exporter groups files into components by its own rules, such as one
component per folder. To choose the grouping yourself, list components under
`components` in `.archplan/config.json`:

```json
"components": {
  "probe/core": {"match": ["packages/probe/src/core/**"], "name": "probe core", "layer": "framework"},
  "tools/symbols": {"match": ["tools/symbols/"]}
},
"layers": [
  {"name": "app", "components": ["web", "api/*"]},
  {"name": "framework"}
]
```

- `match` lists glob patterns, with the same rules as `exclude`. A file goes
  to the first component in the list whose pattern matches it. Files that
  match no pattern keep the exporter's component.
- `name`, `layer` and `doc` are optional.
- `archplan export` applies this after any exporter. It moves the files'
  types, signatures, literals and enums along, removes exporter components
  that lost all their files, and adds up the dependencies between the new
  components again, without edges from a component to itself. With a `files`
  section in the model, those weights count importing files, tests left out.
  Without one, archplan splits a dependency by the files in its evidence.
- `layers` lists layers from top to bottom. Each names its components by id
  or by a glob over ids. A component's `layer` puts it in that layer too.
  The layers go into the model and replace the inferred ones when
  `archplan profile infer` proposes a profile; a locked profile keeps its own.

These exporters are entries in a registry,
[`src/archplan/contrib/exporters`](src/archplan/contrib/exporters).
`archplan exporters` shows which ones match and which are installed. Any
program can be an exporter: add an entry in `.archplan/exporters/`, or set
`export` in `.archplan/config.json`. The contract is in
[`exporter-plugins.md`](src/archplan/format/exporter-plugins.md), and
`archplan export-check` tests a model against it. A command from a project
entry still needs `archplan trust`.

A repository with a server and a browser app in different languages can
use several exporters: make `export` a list of commands that each write to
`{out}`. archplan merges their models, and joins the app's HTTP calls with
the server code they reach. See
[Several exporters](src/archplan/format/exporter-plugins.md#several-exporters).

## Security

- The editor listens on 127.0.0.1 only and has no login. It refuses writes from
  pages on other origins.
- The editor answers only requests addressed to localhost, so a web page
  cannot reach it through DNS rebinding.
- The `export` command in `.archplan/config.json` runs in a shell, and a cloned
  repository supplies it. The agent's `refresh_model` and the editor's "From
  code" button run it only after you approved that exact command on this
  machine. `archplan init` approves the command it writes only for the
  `python` and `lite` exporters, which read the code without running it. The
  TypeScript, Elixir and Go exporters run code from the repository (its
  `typescript` package, its `mix.exs`, the go toolchain), so `archplan init`
  leaves them unapproved and says so. Run `archplan trust` (or `archplan
  export`, which asks) in a terminal. There is no flag to skip the question,
  so an agent cannot approve a command. Approvals live in
  `~/.config/archplan/trusted.json`. When `export` is a list, one approval
  covers the whole list, and any change to it needs a new one.
- An approval also stores a sha256 hash of each repository file the command
  names as an argument, such as `tools/check.py` in `python3 tools/check.py
  {ctx}`. This holds for export commands and external detectors. When such a
  file changes, for example after a `git pull`, the command needs a new
  approval, and archplan names the changed file.
- archplan reads repository files as untrusted data. It reads only regular
  files inside the repository: a symlink, a device or a FIFO is skipped, and
  so is a file larger than its limit (about 2 MB for source files). Folder
  walks do not follow symlinked folders. The parsers avoid regular
  expressions that slow down on hostile input.
- `archplan schema --sqlite FILE` opens the database read-only, whatever the
  file name holds.
- Thread and plan text renders as Markdown without raw HTML; links only open
  http(s) and mailto addresses. The editor's libraries ship with the package.

## Development

```bash
python3 -m unittest discover -s tests
```

```bash
claude plugin validate .
```

```bash
claude --plugin-dir .
```

The editor is a single file, `src/archplan/static/index.html`. Add `?sample` to
its URL for the built-in example model.
