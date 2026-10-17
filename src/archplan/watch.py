"""Print one line per change an agent should act on, for use as an event stream
(a Claude Code monitor). It reads the .archplan files; no server is needed.

Events:
  comment   a thread whose newest message is from the human (new or updated)
  resolved  a thread the human resolved or reopened
  plan      the plan changed (tasks added or removed)
  suggestion  the human accepted, partly accepted or rejected a suggestion
  handover  the human sent planned refactors, approved batches, or cancelled

Comments on planned refactors are left out until the human sends them.
"""

from __future__ import annotations

import time

from .project import Project, read_json
from .store import Store, mentions
from .handover import Handovers, HandoverMode, handover_mode
from .suggestions import Suggestions


def tasks(p: Project):
    plan = read_json(p.path("plan.json"))
    if plan is None:
        return None
    return {t.get("key") or t.get("title"): t.get("title", "") for t in plan.get("tasks", [])}


def watch(p: Project, interval: float = 2, emit=print) -> None:
    store = Store(p.dir)
    seen = {t["id"]: (len(t["messages"]), t["status"]) for t in store.read()["threads"]}
    last_tasks = tasks(p)
    sgs = Suggestions(p.dir)
    decided = {x["id"]: x["status"] for x in sgs.read()["suggestions"]}
    hos = Handovers(p.dir)
    ho_seen = {h["id"]: h["status"] for h in hos.read()["handovers"]}
    out = lambda s: emit(s, flush=True) if emit is print else emit(s)
    while True:
        time.sleep(interval)
        active = hos.active_ids()
        for t in store.read()["threads"]:
            n, st = len(t["messages"]), t["status"]
            old = seen.get(t["id"])
            seen[t["id"]] = (n, st)
            last = t["messages"][-1]
            quiet = st == "planned" and t["id"] not in active  # locked in: not for the agent yet
            if (old is None or n > old[0]) and last["author"] != "agent":
                if not quiet:
                    text = " ".join(mentions(last["text"]).split())
                    out(f"comment {t['id']} on {t['anchor'][:120]}: {text[:300]}")
            elif old and st != old[1] and n == old[0] and st != "planned":
                # status alone: the human (the agent resolves with a reply)
                out(f"{'reopened' if st == 'open' else st} {t['id']} on {t['anchor'][:120]}")
        for x in sgs.read()["suggestions"]:
            if x["status"] != decided.get(x["id"], "pending"):
                out(suggestion_line(x))
            decided[x["id"]] = x["status"]
        for h in hos.read()["handovers"]:
            line = handover_line(h, ho_seen.get(h["id"]))
            ho_seen[h["id"]] = h["status"]
            if line:
                out(line)
        cur = tasks(p)
        if cur is not None and last_tasks is not None and cur != last_tasks:
            added = [cur[k] for k in cur if k not in last_tasks]
            removed = [last_tasks[k] for k in last_tasks if k not in cur]
            if added or removed:
                parts = [f"+{x}" for x in added[:5]] + [f"-{x}" for x in removed[:5]]
                out(f"plan {len(cur)} tasks: " + "; ".join(parts)[:400])
        if cur is not None:
            last_tasks = cur


def suggestion_line(x: dict) -> str:
    n, k = len(x["ops"]), len(x.get("accepted") or [])
    what = {"accepted": f"accepted ({k} of {n} ops)", "partial": f"partly accepted ({k} of {n} ops: "
            + ", ".join(map(str, x["accepted"])) + ")", "rejected": f"rejected (0 of {n} ops)"}.get(x["status"], x["status"])
    on = f" in thread {x['thread']}" if x.get("thread") else ""
    note = " ".join((x.get("title") or x.get("note") or "").split())[:200]
    line = f"suggestion {x['id']}{on} {what}" + (f": {note}" if note else "")
    if x.get("reason"):
        line += " Reason: " + " ".join(x["reason"].split())[:300]
    return line


def handover_line(h: dict, was: str | None) -> str | None:
    """A line for the handover changes the human makes; None for the agent's own."""
    if h["status"] == was:
        return None
    note = " ".join((h.get("note") or "").split())[:300]
    if was is None and h["status"] == "sent":
        how = "all in one go" if handover_mode(h) is HandoverMode.ALL else "ask for a split first"
        return (f"handover {h['id']} sent: {len(h['refactors'])} planned refactors, {how}"
                + (f": {note}" if note else "") + ". Call get_handover.")
    if h["status"] == "approved":
        return f"handover {h['id']} batches approved"
    if h["status"] == "cancelled":
        return f"handover {h['id']} cancelled"
    return None
