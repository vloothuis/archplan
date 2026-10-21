---
name: archplan
description: "Work with the human on this repository's architecture in archplan: set it up, open the editor, answer comment threads, model refactors in the target, and implement the plan. Use when the user invokes /archplan, mentions archplan or its comments, or asks to plan or review architecture changes together in the editor."
argument-hint: "[init | up | status | watch]"
allowed-tools: Bash(archplan:*), Bash(${CLAUDE_PLUGIN_ROOT}/bin/archplan:*)
---

# archplan

archplan is a browser editor for a repository's architecture. The human edits a
target model (components, types, dependency rules, call sequences, glossary),
and the editor turns the difference with the code into a change plan. Both of
you comment on items in threads. You read and answer them with the `archplan`
MCP tools; the human sees your replies in the editor within a second.

Everything lives in `.archplan/` in the repository root. The `archplan`
command is on your PATH (from this plugin's `bin/`); if it is not, use
`${CLAUDE_PLUGIN_ROOT}/bin/archplan`.

Arguments: $ARGUMENTS

## 1. Set up (once per repository)

Run `archplan status`. If it says there is no project:

1. `archplan init` in the repository root. It picks an exporter for Go
   (`go.mod`), Python (`pyproject.toml`, `setup.py`), Elixir (`mix.exs`) and
   TypeScript or JavaScript (`package.json`; run `npm install` first, since
   `archplan export-ts` uses the project's `typescript`). Java, Kotlin, C#,
   Ruby, PHP, Rust, Swift and Scala get `archplan export-lite`, which is
   less precise. `archplan exporters` shows the choice and install hints.
   For other languages, tell the user that `export` in
   `.archplan/config.json` must name a command that writes an archmodel/1
   file (`src/archplan/format/exporter-plugins.md` in this plugin), and stop.
2. `archplan export`. Report the counts. If it fails, show the error. If it
   says the command is not approved (always the case for the TypeScript,
   Elixir and Go exporters, which run code from the repository), ask the
   user to read the command and run `archplan trust` in a terminal. Never
   try to approve it yourself.
3. Ask whether `.archplan/` should be committed. Committing it shares the
   target, plan and threads with the team; `server.json` and logs stay
   ignored.

## 2. Open the editor

Run `archplan up`. It starts the editor in the background (or finds the
running one) and prints its URL. Give the user the URL. If you have a browser
pane, open it there.

## 3. Stay in the conversation

The human works in the editor while you work here. You learn about their
comments three ways:

- The plugin's `archplan` monitor runs in every session and sends one event
  per new human comment, resolved thread, plan change, suggestion the human
  accepted, partly accepted or rejected, and handover the human sent,
  approved or cancelled. Act on each event.
- At each user prompt a hook lists threads that wait on you.
- `wait_for_comments` blocks until the human writes, when you have nothing else
  to do and the user asked you to keep watching.

If the monitor is not running (for example in a session without monitors),
start `archplan watch` with the Monitor tool yourself.

## 4. Answer a thread

1. `read_thread` gives the messages and the model item the thread is about.
2. Investigate in the code. Be concrete: name files, types and call sites.
3. `reply` in that thread. Keep it short and plain.
   - Set `needs_decision=true` with two to four short `options` only when you
     cannot continue without the human's choice. End with one clear question.
   - Everything else (findings, reports, answers) is feedback: leave
     `needs_decision` false.
   - Set `resolve=true` when the question is settled or the work is done.
4. To raise something yourself, `open_thread` on the item (see the anchor
   formats in the tool description). Use a `group:` anchor for a finding about
   several items at once, such as an import cycle.

To point at a model item inside a message, write `@[Label](anchor)`, for
example `@[Orders](comp:shop.orders)`. The editor shows it as a link. The
human's mentions reach you as `@Label (anchor)`. A thread the human started
from a graph has a `snapshot` line: the view, mode, focus and the items that
were on screen.

## 5. Find coupling worth discussing

`get_hotspots` reads the git history. It lists the components that change
most, and pairs that change in the same commits. Pairs under
`hidden_coupling` have no dependency in the model. They often share a
concept, a data format or a duplicated rule. Check a few in the code, then
`open_thread` on `group:comp:<a>|comp:<b>` with what ties them together.
`archplan hotspots` prints the same in a terminal.

### The architecture profile

`get_architecture` returns the profile (units, file roles, rules, layers)
and how the code conforms: role coverage, unmatched files, and rule breaks
with examples. Use a break as evidence in a thread, with the file and line.
To change a proposed profile, send the whole profile with
`update_profile(profile, note)`. It stays proposed until the human locks it
in the editor; a locked profile is refused. `archplan profile` prints the
same in a terminal.

### Findings

`get_findings` lists what archplan notices: profile rule breaks, dependency
cycles with the cheapest cut, tangles (one finding for four or more
components that all reach each other, with cuts that break every cycle),
upward layer dependencies, glossary leaks,
large types, types that look like synonyms, and hidden coupling. By default
it lists the new ones, those the human has not accepted or dismissed.
`get_item("finding:<id>")` gives the full evidence.

- When a heuristic cannot settle a finding alone, check the code and give
  your judgment with `label_finding(id, label, note)`, for example "likely
  the same" or "intended". The human sees it as a label, not a verdict.
- When the human discusses a finding, its thread has the anchor
  `finding:<id>` and the evidence as a snapshot. Answer it like any thread;
  propose a fix with `edit_target`.
- Never accept or dismiss findings yourself. That is the human's call.

`archplan check` prints the new findings in a terminal and exits with 1 when
there are any. In CI, use `archplan ci --base <base branch> --format github`.
It fails only on findings that the pull request adds.

Detectors make the findings. `archplan detectors` lists them, with the data
each one needs and what is missing. When a detector cannot run, tell the
human what would make it work (for example `archplan hotspots`), but do not
switch detectors on or off, and never run `archplan detectors trust`: a
person approves an external detector's command.

When the repository has a database schema (Django models, Ecto schemas,
`db/schema.rb`, `schema.prisma`, `schema.sql` or `structure.sql`), the model
has a `schema` section and the `data.*` detectors run: tables written by
several components, foreign keys across components, JSON columns, flag
columns and more. `archplan schema` shows the tables per owner. Never read a
live database on your own: `--sqlite` and `--postgres` are for the human to
ask for.

### Glossaries

When the human asks for glossaries, or a thread is anchored on `glossary`:

1. Call `get_vocabulary_candidates()` for all components, or with a
   component for its top 30 terms.
2. Pick the domain concepts. Skip technical words, framework names and
   noise. Merge terms that name the same thing.
3. Write a one-line definition for each concept. Ground it in the samples,
   the sites and the types of the term; read the code when unsure.
4. Decide the owner, `uses` and `avoid` from the counts in other
   components. The owner is usually the component with the most uses.
5. Call `propose_glossary(components, note, thread)` in batches of about 10
   concepts. The human reviews it as a suggestion. On approval, write the
   `GLOSSARY.md` files as the handover asks.

### Refactor proposals

A thread anchored `suggest` asks you for refactor proposals. The human opens
it, or archplan does in auto mode.

1. Call `get_refactor_brief` (with the thread's focus, if any).
2. Read the code around the top opportunities. Check the evidence sites.
3. Propose concrete, small refactors with `propose_refactor`: one concern
   each, ops on the target model, the finding ids it fixes, effort and risk,
   and a rationale that names the evidence (files, counts, finding ids).
   Pass the request thread as thread. Check `verified` in the result; when a
   proposal introduces findings, rethink it.
4. Never repeat an idea under `rejected_before`, or one of an opportunity's
   `rejected` reasons.
5. Reply in the request thread with a short list of what you proposed.

## 6. Model before you build

When the human asks you to model a change, use `edit_target` (add or move
components and types, forbid or permit dependencies). It does not change the
target. It saves a suggestion that the human previews in the editor and
accepts in full, in part, or not at all. Pass `thread` so the suggestion is
linked to the thread it answers, and a one-line `note`.

You learn the outcome from the monitor (a `suggestion ... accepted` line) or
from `status`. After an accept, `get_plan` shows the tasks the change implies;
summarise them in the thread and ask whether to implement. After a reject,
ask in the thread what to change, or let it go.

## 7. Planned refactors and the handover

When a discussion is settled, the human locks the thread in as a planned
refactor. It moves to a stack of planned refactors and no longer waits on you.
Do not implement it yet, even when the human replies in it.

When the human is happy with all of them, they send the whole stack as one
handover. The monitor prints `handover ho-... sent`, and the prompt hook says
so too. Call `get_handover`. It lists each refactor in order with its title,
summary, anchor, mentioned items and a digest of the discussion, plus the
current plan.

- Mode `all`: implement the refactors in order.
- Mode `batches`: the human wants a split first. Group the refactors into
  batches of work that you can finish and check one at a time, each a branch
  or pull request. Every refactor goes in exactly one batch. Send them with
  `propose_batches`, then wait. The monitor prints `batches approved` when the
  human agrees (they may have edited the batches; read them again with
  `get_handover`). Then work batch by batch.

For each refactor:

1. `set_refactor_state(thread, "in-progress")`.
2. Change the code (see Implement below).
3. `refresh_model`.
4. `set_refactor_state(thread, "done", note)`.
5. `reply` in its thread: what changed, and what is left.

The handover is done when all its refactors are. If the monitor prints
`handover ... cancelled`, stop and ask the human what to do with the work in
progress.

## 8. Implement

- Work on a new branch or in a git worktree, never on the user's current
  branch without asking. Commit only when the user agreed; never push without
  asking.
- Follow the plan task's acceptance checks (build, tests, dependency rules).
- After a change, call `refresh_model`. The editor reloads the code's model,
  rebases the target onto it, and the plan shrinks by what you did. Report in
  the thread what changed and what is left.
- To declare a component's trust zone, a guard function, an input check
  (`validates`, `interprets`, `source`, `trusts`) or whether an entry
  point needs auth, call `annotate` instead of editing marker comments by
  hand. It writes in the project's own form and checks the result. Make a
  change it reports as unsupported, conflict or failed by hand.
- Before you change code on a path to a guard or a sink, call
  `get_blast_radius` with `diff` (or `files`). It lists the entry points
  the change touches and the security tests they lack. Add a test that makes
  the guard refuse a caller where `plan` asks for one, and run it with
  tracing, so `security.untested_denial` sees the refusal. When an entry
  point has unchecked input, its `review` lists the calls that pass that
  input on: read those first when they change.

## Commands

| Command | What it does |
|---|---|
| `archplan status` | editor URL, model size, plan size, threads waiting |
| `archplan up` / `down` | start or stop the editor in the background |
| `archplan export` | re-export the as-is model from the code |
| `archplan threads --waiting agent` | threads that wait on you, as text |
| `archplan hotspots` | hotspots and hidden coupling from the git history |
| `archplan blast [--diff REF]` | what traced entry points reach, what a change touches, the security tests to add |
| `archplan profile` | the architecture profile and its rule breaks |
| `archplan profile list` | the framework profiles that match, with their fit |
| `archplan check` | new findings; exit 1 when there are any |
| `archplan ci [--base REF]` | check for a pull request: findings new against the base, a summary and SARIF |
| `archplan detectors` | the detectors, their needs, missing data and finding counts |
| `archplan schema [tables]` | database tables, their owners, and foreign keys across components |
| `archplan glossary candidates` | glossary candidate terms per component |
