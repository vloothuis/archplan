"""MCP server on stdio for an agent. It reads and writes the same .archplan
files as the editor server, so the human and the agent talk through comment
threads anchored to model items and plan tasks."""

from __future__ import annotations

import json
import sys

from . import __version__, architecture, hotspots, reload
from .model import ANCHORS, EDIT_OPS, dumps, find_items, get_item, violations
from .project import Project, ProjectError, counts, read_json, write_json
from .handover import Handovers, HandoverMode, handover_mode, progress
from .store import MENTION, Store, find, mentions, stamp, needs, wait, who_next
from .suggestions import Suggestions, propose

DECISION = {"type": "boolean", "default": False,
            "description": "True when you are blocked until the human chooses. The editor then shows the "
                           "thread under Decisions. Leave false for reports, findings and answers that need no choice."}
MENTIONS = ("To point at a model item in the text, write @[Label](anchor), e.g. @[Orders](comp:shop.orders); "
            "the editor shows it as a link. Messages from the human use the same form.")
OPTIONS = {"type": "array", "items": {"type": "string"},
           "description": "With needs_decision: short choices the human can click, e.g. [\"Do it\", \"Not now\"]. "
                          "They can still answer in free text."}

TOOLS = [
    {"name": "status",
     "description": "Overview of the archplan project: editor URL, model size, plan size, and threads waiting on "
                    "you or on the human. Call it first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_threads",
     "description": "List comment threads between the human architect and you. Each has an anchor, "
                    "a status and waiting_on (agent, human or nobody). Start with waiting_on=agent. A planned "
                    "thread is a refactor the human locked in; leave it alone until they send it to you "
                    "(get_handover). " + ANCHORS,
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["open", "planned", "resolved", "all"], "default": "open"},
         "waiting_on": {"type": "string", "enum": ["agent", "human", "any"], "default": "any"}}}},
    {"name": "read_thread",
     "description": "Read one thread with all its messages, plus the model item or plan task it is anchored to. "
                    "When the human started it from a graph, snapshot says what they were looking at. "
                    "Mentions appear as @Label (anchor).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "reply",
     "description": "Reply in a thread. Set resolve=true when the question is settled, for example after "
                    "you implemented what was asked. The human sees the reply in the editor at once. " + MENTIONS,
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "text": {"type": "string", "description": "Markdown"},
         "resolve": {"type": "boolean", "default": False},
         "needs_decision": DECISION, "options": OPTIONS}, "required": ["id", "text"]}},
    {"name": "open_thread",
     "description": "Start a thread on a model item or plan task, to report a finding or ask the human. "
                    + ANCHORS + " " + MENTIONS,
     "inputSchema": {"type": "object", "properties": {
         "anchor": {"type": "string"}, "text": {"type": "string", "description": "Markdown"},
         "needs_decision": DECISION, "options": OPTIONS}, "required": ["anchor", "text"]}},
    {"name": "get_plan",
     "description": "The current change plan as the editor computed it: intent, open questions and tasks. "
                    "Each task in the JSON form has a key; use task:<key> as an anchor. The editor recomputes "
                    "the plan while it is open in a browser.",
     "inputSchema": {"type": "object", "properties": {
         "format": {"type": "string", "enum": ["md", "json"], "default": "md"}}}},
    {"name": "get_item",
     "description": "The target model's entry for an anchor: a type, component (with its glossary: owned "
                    "concepts, uses, avoid and leaks), dependency, message, glossary concept or leak. " + ANCHORS,
     "inputSchema": {"type": "object", "properties": {"anchor": {"type": "string"}}, "required": ["anchor"]}},
    {"name": "find",
     "description": "Search components and types in the target model by id, name or path; returns anchors.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "kind": {"type": "string", "enum": ["any", "component", "type"], "default": "any"}}, "required": ["query"]}},
    {"name": "edit_target",
     "description": "Propose changes to the target architecture. This does not change the target: it saves the ops "
                    "as a pending suggestion. The human previews it in the editor and accepts all ops, some of them, "
                    "or none. You learn the outcome from the archplan monitor (`archplan watch` prints a line) or "
                    "status. Use it to model a refactor before implementing it, when the human asks for it. Every op "
                    "must apply to the current target, or nothing is saved. Pass thread to link the suggestion to "
                    "the thread it answers; a short note there points the human to it. Ops: "
                    + "; ".join(f"{k} {v}" for k, v in EDIT_OPS.items()),
     "inputSchema": {"type": "object", "properties": {
         "ops": {"type": "array", "items": {"type": "object", "properties": {"op": {"type": "string", "enum": list(EDIT_OPS)}},
                                            "required": ["op"]}},
         "note": {"type": "string", "description": "One or two sentences: what the change does and why."},
         "thread": {"type": "string", "description": "Id of the thread this suggestion answers (optional)."}},
         "required": ["ops"]}},
    {"name": "annotate",
     "description": "Change declarations in the code: a component's trust zone (component.zone), the components "
                    "through which others must reach it (component.gateway: a list of component ids), a function "
                    "that authenticates (guard authn), authorizes (guard authz) or both (guard true) "
                    "(function.guard), a function that writes or reads data that needs an authorization "
                    "check first (function.sink: write or read), or whether an entry point "
                    "needs an authenticated caller (entrypoint.auth: required), also a check of what that caller may "
                    "do (authz), or neither (none, with a reason). "
                    "The project's annotator writes them in its own form (marker comments by default) in a "
                    "scratch copy and reads them back; you get one result per change and the patch. With "
                    "apply, the patch goes into the working tree. A change with status unsupported, conflict "
                    "or failed is not in the patch: make it by hand, then call refresh_model. The model's "
                    "declarations section shows the current ones.",
     "inputSchema": {"type": "object", "properties": {
         "changes": {"type": "array", "items": {"type": "object", "properties": {
             "op": {"type": "string", "enum": ["set", "remove"]},
             "target": {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": ["component", "function", "entrypoint"]},
                 "id": {"type": "string", "description": "a component id, or a function's id from the model's "
                                                         "signatures or types, or <file>:<name>"}},
                 "required": ["kind", "id"]},
             "key": {"type": "string", "enum": ["zone", "gateway", "guard", "sink", "auth"]},
             "value": {"description": "zone: an id such as internal; gateway: a list of component ids; "
                                      "guard: true, authn or authz; "
                                      "sink: write or read; auth: required, authz or none"},
             "reason": {"type": "string"}},
             "required": ["op", "target", "key"]}},
         "apply": {"type": "boolean", "default": False}},
         "required": ["changes"]}},
    {"name": "get_hotspots",
     "description": "Hotspots and coupling from git history: the components that change most, and pairs that change "
                    "in the same commits. hidden_coupling lists pairs with no dependency in the model; they often "
                    "share a concept, a data format or a duplicated rule. Use it to find coupling worth discussing "
                    "with the human, then open_thread on a group:comp:<a>|comp:<b> anchor with your finding. "
                    "degree is together / the commits of the less active component.",
     "inputSchema": {"type": "object", "properties": {
         "since_days": {"type": "integer", "default": 365},
         "limit": {"type": "integer", "default": 20, "maximum": 100}}}},
    {"name": "refresh_model",
     "description": "Re-export the as-is model from the code with the project's export command. Call it after you "
                    "change the code, so the editor shows what is left of the plan. Can take a minute.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wait_for_comments",
     "description": "Block until the human adds or changes a thread, then return the threads waiting on you. "
                    "Use it to stay in the conversation while the human works in the editor.",
     "inputSchema": {"type": "object", "properties": {
         "timeout_seconds": {"type": "integer", "default": 120, "maximum": 600}}}},
    {"name": "get_architecture",
     "description": "The architecture profile: the unit kinds (an Eyra system, a Django app), the roles files play "
                    "in a unit (found by file name: _public.ex, *_view.ex, models.py), and rules about who may call "
                    "whom and which files come in pairs. Returns role coverage, rule breaks with examples and the "
                    "least conforming units. Without a saved profile it returns archplan's proposal. While the "
                    "profile is proposed, improve it with update_profile: give roles clear names and descriptions, "
                    "merge roles that are the same thing, drop rules that are noise, and write rules the human "
                    "states in a thread. Discuss breaks worth fixing in a thread.",
     "inputSchema": {"type": "object", "properties": {
         "breaks": {"type": "integer", "default": 5, "maximum": 50, "description": "examples per rule"},
         "full": {"type": "boolean", "default": False, "description": "also return the whole profile, to edit it"}}}},
    {"name": "get_findings",
     "description": "Findings: what archplan notices in the code, such as profile rule breaks, dependency cycles "
                    "(with the cheapest cut), upward layer dependencies, glossary leaks, large types or types with "
                    "groups of related fields, types that look like synonyms, and hidden coupling from git history. "
                    "Each has a strength (strong, medium, weak) and a basis. state=new lists those the human has not "
                    "accepted or dismissed. Give your judgment on one with label_finding; discuss one in a thread "
                    "on its finding:<id> anchor. get_item(finding:<id>) returns the full evidence.",
     "inputSchema": {"type": "object", "properties": {
         "group": {"type": "string", "enum": ["profile", "structure", "layers", "vocabulary", "types", "history"]},
         "state": {"type": "string", "enum": ["new", "accepted", "dismissed", "all"], "default": "new"},
         "limit": {"type": "integer", "default": 30, "maximum": 200}}}},
    {"name": "label_finding",
     "description": "Put your judgment on a finding that a heuristic cannot settle alone, as a short label the "
                    "human sees next to it, e.g. \"likely the same\", \"intended\", \"easy fix\". It is shown as a "
                    "label, never as a verdict: the human still accepts, dismisses or discusses it. An empty label "
                    "removes yours.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "label": {"type": "string", "description": "a few words"},
         "note": {"type": "string", "description": "one or two sentences: why"}}, "required": ["id", "label"]}},
    {"name": "get_vocabulary_candidates",
     "description": "Candidate glossary terms per component, from tf-idf over the code: words and two-word phrases "
                    "a component uses much more than the others. Each has a score, df (how many components use it), "
                    "the other components that use it most, sample identifiers, sites and model types, plus the "
                    "component's existing concepts. Without component: the top 15 of every component, largest "
                    "first. Use it to draft glossaries: keep the real domain concepts and skip technical noise.",
     "inputSchema": {"type": "object", "properties": {
         "component": {"type": "string"}, "top": {"type": "integer", "default": 30, "maximum": 100}}}},
    {"name": "propose_glossary",
     "description": "Propose glossaries for components as a pending suggestion (like edit_target); the human "
                    "accepts it in the editor, and the plan then lists the GLOSSARY.md files to write. components: "
                    "{component id: {concepts: [{name, terms, definition, kind}], uses: [concept names], avoid: "
                    "[concept names]}}. A concept is owned by the component it is listed under; terms are the words "
                    "code uses for it (the name's words by default); definition is one line grounded in the code; "
                    "kind is domain, vendor or technical. uses names concepts of other components this one may "
                    "name; avoid names those it must not. Send about 10 components at a time.",
     "inputSchema": {"type": "object", "properties": {
         "components": {"type": "object"}, "note": {"type": "string"},
         "thread": {"type": "string", "description": "the thread this answers, such as the glossary request"}},
         "required": ["components"]}},
    {"name": "get_refactor_brief",
     "description": "Ranked refactor opportunities from what archplan knows: new findings grouped by component pair "
                    "or cycle, churn hotspots and misplaced glossary terms. Each has an id, a value score (severity x "
                    "churn x fan-in), its components, finding ids, compact facts and 1 to 3 heuristic moves. "
                    "Opportunities with a pending or planned proposal are left out. rejected_before lists proposals "
                    "the human rejected, with the reason: do not repeat them. Call it when a thread anchored "
                    "\"suggest\" asks for proposals.",
     "inputSchema": {"type": "object", "properties": {
         "top": {"type": "integer", "default": 8, "maximum": 30},
         "focus": {"type": "array", "items": {"type": "string"}, "description": "component ids to focus on"},
         "kinds": {"type": "array", "items": {"type": "string"},
                   "description": "finding kinds or groups, e.g. structure.cycle or layers"}}}},
    {"name": "propose_refactor",
     "description": "Propose one small refactor (one concern) as a pending suggestion of kind refactor. ops are "
                    "target edits as for edit_target. archplan applies them to a copy of the target and recomputes "
                    "cycles, upward layer dependencies and policy breaks: verified.fixed and verified.introduced "
                    "in the result. Other fixes are claimed, not verified. The rationale names the evidence (files, "
                    "counts, finding ids). Pass the request thread as thread.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Imperative, under 80 characters."},
         "rationale": {"type": "string", "description": "Why, with the evidence; a few sentences."},
         "ops": {"type": "array", "items": {"type": "object", "properties": {"op": {"type": "string", "enum": list(EDIT_OPS)}},
                                            "required": ["op"]}},
         "fixes": {"type": "array", "items": {"type": "string"}, "description": "finding ids this fixes"},
         "opportunity": {"type": "string", "description": "the opportunity id from get_refactor_brief"},
         "effort": {"type": "string", "enum": ["S", "M", "L"], "default": "M"},
         "risk": {"type": "string", "enum": ["low", "med", "high"], "default": "med"},
         "thread": {"type": "string"}},
         "required": ["title", "rationale", "ops"]}},
    {"name": "update_profile",
     "description": "Replace the proposed architecture profile (the whole object, as get_architecture's profile "
                    "has it; use full=true there to get it). Only while the profile is proposed: once the human "
                    "locked it in, propose changes in a thread instead. Profile: units [{kind, glob}], roles "
                    "[{id, name, patterns, color, description}] where a pattern without a slash matches the file "
                    "name and one with a slash the path inside the unit, rules [{id, text, type: calls, from: role "
                    "| other-units | any, to: role or [roles], allow: true (only these) | false (never these), "
                    "scope: same-unit | other-units | any} or {id, text, type: pair, role, needs, match: "
                    "[\"<stem>_view.ex -> <stem>_view_builder.ex\"]}], layers [{name, components}].",
     "inputSchema": {"type": "object", "properties": {
         "profile": {"type": "object"}, "note": {"type": "string", "description": "what you changed and why"}},
         "required": ["profile"]}},
    {"name": "get_handover",
     "description": "The planned refactors the human sent you to implement, in order: per refactor its thread, "
                    "title, summary, state, anchor, the items it mentions and a digest of the discussion; plus the "
                    "current change plan. How to work it: with mode all, implement the refactors one by one in "
                    "order. With mode batches, first call propose_batches (split or combine the refactors into "
                    "batches of work), then wait until the human approves (the archplan monitor prints "
                    "'batches approved'; get_handover shows status approved), then work batch by batch. For each "
                    "refactor: set_refactor_state(in-progress), change the code, call refresh_model, "
                    "set_refactor_state(done), and reply in its thread with what changed.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_batches",
     "description": "For a handover in mode batches: propose how to group the planned refactors into batches of "
                    "work, each a branch or pull request you can finish and check on its own. Every refactor of "
                    "the handover must be in exactly one batch. The human approves or edits the batches in the "
                    "editor; do not start before that.",
     "inputSchema": {"type": "object", "properties": {
         "batches": {"type": "array", "items": {"type": "object", "properties": {
             "title": {"type": "string"},
             "refactors": {"type": "array", "items": {"type": "string"}, "description": "thread ids, in order"},
             "notes": {"type": "string", "description": "why these go together, risks, order"}},
             "required": ["title", "refactors"]}}}, "required": ["batches"]}},
    {"name": "set_refactor_state",
     "description": "Report progress on a refactor of the active handover: in-progress when you start it, done "
                    "when the code is changed and refresh_model ran. The handover is done when all refactors are.",
     "inputSchema": {"type": "object", "properties": {
         "thread": {"type": "string"}, "state": {"type": "string", "enum": ["in-progress", "done"]},
         "note": {"type": "string", "description": "short: what changed, or what blocks it"}},
         "required": ["thread", "state"]}},
]

INSTRUCTIONS = (
    "archplan connects you with a human architect who edits the target architecture of this repository in a "
    "browser editor and comments on components, types, dependencies, calls and plan tasks. Check "
    "list_threads(waiting_on=agent) before and after work, and reply in the thread you acted on. Mark a reply "
    "needs_decision=true (with options) only when you cannot continue without the human's choice; everything else "
    "is feedback. After changing code, call refresh_model so the editor shows the remaining plan. Use edit_target "
    "to propose a refactor of the target when asked, instead of describing it in prose; the human accepts or "
    "rejects it in the editor. Mention model items in messages as @[Label](anchor). Threads the human locked in "
    "as planned refactors wait until the human sends them as a handover; then follow get_handover. A thread "
    "anchored \"suggest\" asks for refactor proposals: call get_refactor_brief, then propose_refactor.")


class Agent:
    def __init__(self, p: Project):
        self.p = p
        self.store = Store(p.dir)
        self.handovers = Handovers(p.dir)

    def summary(self, th: dict, active: frozenset = frozenset()) -> dict:
        last = th["messages"][-1]
        out = {"id": th["id"], "anchor": th["anchor"], "label": th["label"], "status": th["status"],
               "waiting_on": who_next(th, active), "human_needs": needs(th, active), "messages": len(th["messages"]),
               "last": {"author": last["author"], "text": mentions(last["text"])}}
        if th.get("refactor"):
            out["refactor"] = {k: th["refactor"].get(k) for k in ("title", "state", "order")}
        return out

    def status(self):
        p = self.p
        srv = read_json(p.path("server.json"))
        model, target = read_json(p.model_path), read_json(p.target_path)
        plan = read_json(p.path("plan.json")) or {}
        d = self.store.read()
        act = self.handovers.active_ids()
        ths = [t for t in d["threads"] if t["status"] == "open" or t["id"] in act]
        sgs = Suggestions(p.dir).newest()
        h = self.handovers.current()
        return {"project": p.name, "root": str(p.root),
                "editor": srv["url"] if srv else "not running (run `archplan up`)",
                "model": counts(model) if model else "none yet (call refresh_model)",
                "exclude": p.exclude.patterns,
                "target": {"edited": bool(target), "violations": len(violations(target)) if target else 0},
                "export_approved": p.trusted(),
                "intent": p.state().get("intent", ""),
                "plan_tasks": len(plan.get("tasks", [])),
                "suggestions": {"pending": sum(1 for x in sgs if x["status"] == "pending"),
                                "recent": [{k: x[k] for k in ("id", "status", "note", "accepted")} | {"ops": len(x["ops"])}
                                           for x in sgs[:5]]},
                "planned_refactors": sum(1 for t in d["threads"] if t["status"] == "planned"),
                "handover": h and {"id": h["id"], "mode": h["mode"], "status": h["status"],
                                   "refactors": len(h["refactors"]), "progress": progress(h, d),
                                   "next": "call get_handover"},
                "threads": {"waiting_on_agent": [self.summary(t, act) for t in ths if who_next(t, act) == "agent"],
                            "waiting_on_human": sum(1 for t in ths if who_next(t, act) == "human")}}

    def list_threads(self, status="open", waiting_on="any"):
        act = self.handovers.active_ids()
        return [self.summary(th, act) for th in self.store.read()["threads"]
                if (status == "all" or th["status"] == status) and (waiting_on == "any" or who_next(th, act) == waiting_on)]

    def read_thread(self, id):
        th = dict(find(self.store.read(), id))
        th["messages"] = [{**m, "text": mentions(m["text"])} for m in th["messages"]]
        if "snapshot" in th:
            th["snapshot"] = snapshot_text(th["snapshot"])
        return {**th, "waiting_on": who_next(th, self.handovers.active_ids()), "item": get_item(self.p, th["anchor"])}

    def reply(self, id, text, resolve=False, needs_decision=False, options=None):
        th = self.store.reply(id, text, "agent", "resolved" if resolve else None,
                              "decision" if needs_decision else "feedback", options)
        return self.summary(th)

    def open_thread(self, anchor, text, needs_decision=False, options=None):
        return self.summary(self.store.create(anchor, anchor, text, "agent",
                                              "decision" if needs_decision else "feedback", options))

    def get_plan(self, format="md"):
        f = self.p.path("plan.json" if format == "json" else "plan.md")
        if not f.exists():
            return "No plan yet: the editor writes one once the target model differs from the code."
        from .safeio import read_file
        return read_file(f, 16 << 20)

    def get_item(self, anchor):
        return get_item(self.p, anchor)

    def find(self, query, kind="any"):
        return find_items(self.p, query, kind)

    def edit_target(self, ops, note="", thread=None):
        return self._propose(ops, note, thread, "edit")

    def _propose(self, ops, note, thread, kind):
        if thread:
            find(self.store.read(), thread)  # fail before saving when the thread does not exist
        out = propose(self.p, ops, note, thread, kind)
        if thread:
            self.store.reply(thread, f"I proposed {len(ops)} change(s) to the target ({out['suggestion']})"
                             + (f": {note}" if note else ".") + " Review them in the editor.", "agent")
        return {**out, "message": "The human reviews this in the editor and accepts all, some or none of the ops. "
                                  "The target is unchanged until then. `archplan watch` reports the outcome."}

    def annotate(self, changes, apply=False):
        from . import annotators
        return annotators.write(self.p, changes, apply=bool(apply))

    def get_hotspots(self, since_days=365, limit=20):
        return hotspots.summary(hotspots.hotspots(self.p, int(since_days)), min(int(limit), 100))

    def refresh_model(self):
        out = {"counts": self.p.export(require_trust=True), "note": "The editor reloads the as-is model and rebases the target."}
        from .brief import auto_tick
        try:
            auto_tick(self.p, self.store)
        except Exception:  # auto mode must never fail an export
            pass
        return out

    def get_architecture(self, breaks=5, full=False):
        st = architecture.Architecture(self.p).state()
        out = architecture.summary(st, min(int(breaks), 50))
        if full:
            out["profile"] = st.get("profile") or st.get("proposal")
        return out

    def update_profile(self, profile, note=""):
        a = architecture.Architecture(self.p)
        cur = a.profile()
        if cur and cur.get("status") == "locked":
            raise ValueError("the human locked in this profile; propose the change in a thread instead")
        prof = a.save({**profile, "note": note or profile.get("note", "")}, "proposed")
        return {"saved": True, "status": prof["status"],
                "summary": architecture.summary(a.state(), 3)}

    def get_findings(self, group=None, state="new", limit=30):
        from .findings import Findings, compact
        lst = Findings(self.p).listing(self.store)
        fs = [f for f in lst["findings"] if (state == "all" or f["state"] == state) and (not group or f["group"] == group)]
        return {"counts": lst["counts"], "missing": lst["missing"], "shown": min(len(fs), int(limit)), "of": len(fs),
                "findings": [compact(f) for f in fs[:min(int(limit), 200)]]}

    def label_finding(self, id, label, note=""):
        from .findings import Findings, compact
        return compact(Findings(self.p).label(id, label, note))

    def get_vocabulary_candidates(self, component=None, top=None):
        from .vocab import Vocab
        res = Vocab(self.p).candidates(component or None, min(int(top), 100) if top else None)
        out = {}
        for c, x in res["components"].items():
            out[c] = {"files": x["files"],
                      "concepts": [{k: v for k, v in k_.items() if v} for k_ in x["concepts"]],
                      "terms": [{"term": t["term"], "score": t["score"], "df": t["df"],
                                 "others": ", ".join(f"{o['component']} {o['count']:g}" for o in t["others"][:3]),
                                 "samples": t["samples"][:4], **({"types": t["types"][:3]} if t["types"] else {}),
                                 **({"owner_guess": t["owner_guess"]} if t["owner_guess"] != c else {})}
                                for t in x["terms"]]}
        return {"n_components": res["n_components"], "components": out,
                **({"warning": res["warning"]} if res.get("warning") else {})}

    def propose_glossary(self, components, note="", thread=None):
        from .vocab import glossary_ops
        target = self.p.target_or_model()
        if not target:
            raise ValueError("no model yet; call refresh_model")
        ops = glossary_ops(target, components)
        return self._propose(ops, note or f"Glossaries for {len(components)} component(s).", thread, "glossary")

    def get_refactor_brief(self, top=8, focus=None, kinds=None):
        from .brief import brief
        return brief(self.p, top, focus or None, kinds or None, self.store)

    def propose_refactor(self, title, rationale, ops, fixes=None, opportunity=None, effort="M", risk="med",
                         thread=None):
        from .brief import propose_refactor
        if thread:
            find(self.store.read(), thread)
        sg = propose_refactor(self.p, title, rationale, ops, fixes, opportunity, effort, risk, thread)
        v = sg["verified"]
        if thread:
            self.store.reply(thread, f"I proposed a refactor ({sg['id']}): {sg['title']}. Review it in the editor.",
                             "agent")
        return {"suggestion": sg["id"], "status": "pending", "verified": v,
                "message": "The human plans, discusses or rejects it in the editor; a rejection comes with a reason. "
                           + ("Note: the ops introduce new findings; check verified.introduced. "
                              if v["introduced"] else "")
                           + ("Some fixes could not be verified from the model (verified.unverified)."
                              if v["unverified"] else "")}

    def get_handover(self):
        h = self.handovers.current()
        if not h:
            last = self.handovers.history(1)
            return {"current": None, "message": "The human has not sent planned refactors. Keep answering threads.",
                    "last": last[0]["status"] if last else None}
        d = self.store.read()
        ths = {t["id"]: t for t in d["threads"]}
        refs = []
        for n, tid in enumerate(h["refactors"], 1):
            th = ths.get(tid)
            if th is None:
                refs.append({"n": n, "thread": tid, "missing": True})
                continue
            r = th.get("refactor") or {}
            items = [th["anchor"]] + [m.group(2) for x in th["messages"] for m in MENTION.finditer(x["text"])]
            refs.append({"n": n, "thread": tid, "title": r.get("title"), "summary": r.get("summary"),
                         "state": r.get("state"), "note": r.get("note"), "anchor": th["anchor"],
                         "mentions": list(dict.fromkeys(items[1:])),
                         "discussion": digest(th)})
        todo = {HandoverMode.ALL: "Implement the refactors in order.",
                HandoverMode.BATCHES: "Call propose_batches first, then wait for the human's approval."}[handover_mode(h)]
        if h["status"] == "proposed":
            todo = "Wait: the human reviews your batches. `archplan watch` reports the approval."
        elif h["status"] in ("approved", "in-progress") and handover_mode(h) is HandoverMode.BATCHES:
            todo = "Work batch by batch, in the approved order."
        return {**{k: h[k] for k in ("id", "mode", "note", "status", "batches", "created")},
                "progress": progress(h, d), "todo": todo,
                "after_each": "refresh_model, set_refactor_state(thread, done), reply in its thread",
                "refactors": refs, "plan": self.get_plan()}

    def propose_batches(self, batches):
        h = self.handovers.propose(batches)
        return {"handover": h["id"], "status": h["status"], "batches": h["batches"],
                "message": "The human reviews the batches in the editor. Wait for the approval before you start."}

    def set_refactor_state(self, thread, state, note=""):
        h, th = self.handovers.set_state(self.store, thread, state, note)
        return {"handover": h["id"], "status": h["status"], "thread": thread, "state": th["refactor"]["state"],
                "progress": progress(h, self.store.read()),
                "next": "Reply in the thread with what changed." if state == "done" else "Implement it."}

    def wait_for_comments(self, timeout_seconds=120):
        v = self.store.version()
        wait(self.store.version, v, min(int(timeout_seconds), 600))
        return self.list_threads("open", "agent")


def digest(th: dict, n: int = 8, width: int = 300) -> list:
    """The last n messages of a thread, one short line each."""
    out = [f"{m['author']}: {' '.join(mentions(m['text']).split())[:width]}" for m in th["messages"][-n:]]
    if len(th["messages"]) > n:
        out.insert(0, f"({len(th['messages']) - n} earlier messages)")
    return out


def snapshot_text(snap) -> str:
    """A thread's graph snapshot (opaque JSON from the editor) as one line."""
    if not isinstance(snap, dict):
        return "snapshot: (unreadable)"

    def names(xs):
        out = []
        for x in xs if isinstance(xs, list) else []:
            if isinstance(x, dict):
                x = x.get("label") or x.get("name") or x.get("id") or (
                    f"{x['from']} -> {x['to']}" if "from" in x and "to" in x else None)
            if x:
                out.append(str(x))
        return out
    parts = []
    if snap.get("view"):
        parts.append(f"{snap['view']} view")
    if snap.get("mode"):
        parts.append(str(snap["mode"]))
    if snap.get("focus"):
        parts.append(f"focus: {', '.join(names(snap['focus'])) if isinstance(snap['focus'], list) else snap['focus']}")
    flt = snap.get("filters")
    if isinstance(flt, dict):
        on = [k if v is True else f"{k}={v}" for k, v in flt.items() if v not in (None, False, "", [], {})]
        if on:
            parts.append("filters: " + ", ".join(on))
    elif flt:
        parts.append(f"filters: {flt}")
    for key in ("nodes", "edges"):
        xs = names(snap.get(key))
        if xs:
            more = f" and {len(xs) - 30} more" if len(xs) > 30 else ""
            parts.append(f"{key} ({len(xs)}): " + ", ".join(xs[:30]) + more)
    return "snapshot: " + ("; ".join(parts) or "empty")


ACTIVITY = {
    "status": "Checking the project status",
    "list_threads": "Reading the threads",
    "read_thread": "Reading thread {id}",
    "reply": "Replied in thread {id}",
    "open_thread": "Opened a thread on {anchor}",
    "get_plan": "Reading the plan",
    "get_item": "Looking at {anchor}",
    "find": "Searching the model for {query}",
    "edit_target": "Proposed {n} change(s)",
    "refresh_model": "Re-exporting the model",
    "annotate": "Writing {n} declaration change(s)",
    "wait_for_comments": "Waiting for comments",
    "get_hotspots": "Analysing git history for hotspots",
    "get_handover": "Reading the planned refactors",
    "get_architecture": "Reading the architecture profile",
    "update_profile": "Updated the architecture profile",
    "get_findings": "Reading the findings",
    "get_vocabulary_candidates": "Reading vocabulary candidates",
    "propose_glossary": "Proposed glossaries",
    "get_refactor_brief": "Reading the refactor brief",
    "propose_refactor": "Proposed a refactor: {title}",
    "label_finding": "Labelled finding {id}: {label}",
    "propose_batches": "Proposed how to split the refactors into batches",
    "set_refactor_state": "{progress}",
}
DONE = {"refresh_model": "Re-exported the model"}  # long calls: a second entry when they finish
AFTER = ("reply", "open_thread", "edit_target", "propose_batches", "set_refactor_state", "update_profile",
         "label_finding", "propose_glossary", "propose_refactor", "annotate")  # recorded only when they succeed


def record(p: Project, store: Store, tool: str, args: dict, done: bool = False) -> None:
    """Write what the agent does now to .archplan/activity.json for the editor."""
    a = {k: str(v)[:80] for k, v in args.items() if isinstance(v, (str, int))}
    lst = args.get("ops", args.get("changes"))
    a["n"] = len(lst) if isinstance(lst, list) else 0
    hs = Handovers(p.dir)
    h = hs.current() or (hs.history(1) or [None])[0]
    a["progress"] = progress(h, store.read()) if h else "Updated a refactor"
    try:
        text = (DONE if done else ACTIVITY).get(tool, tool).format_map(a)
    except (KeyError, ValueError):
        text = tool
    act = {"at": stamp(), "tool": tool, "summary": text}
    anchor = args.get("anchor") if isinstance(args.get("anchor"), str) else None
    tid = args.get("id") if tool in ("read_thread", "reply") else args.get("thread")
    if isinstance(tid, str):
        act["thread"] = tid
        try:
            anchor = find(store.read(), tid)["anchor"]
        except KeyError:
            pass
    if anchor and anchor.split(":", 1)[0] in ("task", "step"):
        act["task"] = anchor.split(":", 1)[1].split("#")[0]
    if h and h["status"] in ("approved", "in-progress"):
        act["handover"] = {"id": h["id"], "progress": a["progress"]}
    try:
        write_json(p.path("activity.json"), act)
    except OSError:
        pass


class UpdateNotice:
    """A line for one tool result when newer archplan code is on disk. The MCP
    server does not restart itself: Claude Code owns its stdio pipe and the
    session state (initialize, the tool list), and a new process cannot take
    those over safely. Claude Code starts the new code on its next start."""

    def __init__(self, every: float = 10.0):
        self.every, self.checked, self.told = every, 0.0, None

    def __call__(self) -> str | None:
        import time
        if reload.disabled() or time.monotonic() - self.checked < self.every:
            return None
        self.checked = time.monotonic()
        try:
            v = reload.newer_on_disk()
        except OSError:
            return None
        if not v or v == self.told:
            return None
        self.told = v
        return (f"archplan was updated to {v} (this MCP server runs {reload.BUILD}). "
                "Restart Claude Code to load it.")


UNCONFIGURED = "This repository has no archplan project yet. The status tool explains how to start one."


def serve(find=Project.find) -> None:
    """Serve the tools on stdio for the project that find() returns. Without a
    project, offer only the status tool, which explains how to set one up, so
    the plugin's MCP server does not fail in repositories that do not use
    archplan. Until a project exists, look for it again on each request: after
    `archplan init` the full tools work without a restart, and the client is
    told that the tool list changed."""
    try:
        agent, err = Agent(find()), ""
    except ProjectError as e:
        agent, err = None, str(e)
    late = agent is None  # the tool list can still change
    names = {t["name"] for t in TOOLS}
    notice = UpdateNotice()

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue  # a notification
        if agent is None and method != "initialize":
            try:
                agent = Agent(find())
                send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            except ProjectError as e:
                err = str(e)
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {"listChanged": True} if late else {}},
                      "serverInfo": {"name": "archplan", "version": __version__},
                      "instructions": INSTRUCTIONS if agent else UNCONFIGURED}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS if agent else TOOLS[:1]}
        elif method == "tools/call" and agent is None:
            result = {"content": [{"type": "text", "text": f"{err}. Start one with `archplan init` (then "
                                   "`archplan export` and `archplan up`), or use the /archplan skill."}],
                      "isError": True}
        elif method == "tools/call":
            p = agent.p
            prm = req.get("params", {})
            name = prm.get("name", "")
            if name not in names:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
                continue
            args = prm.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            if name not in AFTER:
                record(p, agent.store, name, args)
            try:
                out = getattr(agent, name)(**args)
                if name in AFTER or name in DONE:
                    record(p, agent.store, name, args, done=name in DONE)
                result = {"content": [{"type": "text", "text": dumps(out)}]}
            except (KeyError, TypeError, ValueError, ProjectError) as e:
                result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            if note := notice():
                result["content"].append({"type": "text", "text": note})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": result})
