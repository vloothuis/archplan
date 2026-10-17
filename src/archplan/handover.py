"""Planned refactors and the handover of them to the agent.

The human locks in a thread as a planned refactor once its discussion is
settled. Planned refactors wait in a stack, in an order the human sets, and
the agent leaves them alone. When the human is happy with all of them, they
send the stack to the agent as one handover, in .archplan/handovers.json:

  mode all       the agent implements the refactors in order
  mode batches   the agent first proposes how to split or combine them into
                 batches; the human approves (and may edit) the batches

A handover moves through sent, proposed (batches only), approved (batches
only), in-progress and done. The human may cancel it at any time. At most one
handover is active (not done or cancelled).

Locks: code that changes both files takes the handovers lock first, then the
threads lock."""

from __future__ import annotations

import uuid
from enum import Enum

from .store import Conflict, JsonStore, NotFound, Store, find, stamp

class HandoverMode(str, Enum):
    ALL = "all"
    BATCHES = "batches"


MODES = tuple(mode.value for mode in HandoverMode)
STATES = ("planned", "in-progress", "done")
ACTIVE = ("sent", "proposed", "approved", "in-progress")


class Handovers(JsonStore):
    name = "handovers"
    empty = {"handovers": []}

    def current(self, d: dict | None = None) -> dict | None:
        """The active handover, if any."""
        d = d or self.read()
        return next((h for h in reversed(d["handovers"]) if h["status"] in ACTIVE), None)

    def history(self, n: int = 10) -> list:
        """The last n finished or cancelled handovers, newest first."""
        return [h for h in reversed(self.read()["handovers"]) if h["status"] not in ACTIVE][:n]

    def active_ids(self) -> frozenset:
        """Thread ids of the active handover: those planned threads wait on the agent."""
        h = self.current()
        return frozenset(h["refactors"]) if h else frozenset()

    # --- the human ---

    def create(self, store: Store, mode: HandoverMode | str, note: str = "") -> dict:
        """Send all planned refactors that are not done yet, in order."""
        try:
            mode = HandoverMode(mode)
        except ValueError:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")

        def fn(d):
            if self.current(d):
                raise Conflict(f"handover {self.current(d)['id']} is still active")
            ids = [t["id"] for t in planned(store.read())]
            if not ids:
                raise ValueError("no planned refactors to send")
            h = {"id": "ho-" + uuid.uuid4().hex[:8], "created": stamp(), "mode": mode.value, "note": note or "",
                 "refactors": ids, "status": "sent", "batches": [], "decided": None}
            d["handovers"].append(h)
            return h
        return self.update(fn)

    def approve(self, store: Store, hid: str, batches: list | None = None) -> dict:
        def fn(d):
            h = get(d, hid)
            if h["status"] != "proposed":
                raise Conflict(f"handover {hid} is {h['status']}, not proposed")
            if batches is not None:
                h["batches"] = check_batches(h, batches)
            h["status"], h["decided"] = "approved", stamp()
            return h
        return self.update(fn)

    def cancel(self, store: Store, hid: str) -> dict:
        """Cancel the handover. Refactors in progress go back to planned; done ones stay done."""
        def fn(d):
            h = get(d, hid)
            if h["status"] not in ACTIVE:
                raise Conflict(f"handover {hid} is already {h['status']}")
            h["status"] = "cancelled"

            def reset(td):
                for t in td["threads"]:
                    r = t.get("refactor")
                    if t["id"] in h["refactors"] and r and r.get("state") != "done":
                        r["state"] = "planned"
            store.update(reset)
            return h
        return self.update(fn)

    # --- the agent ---

    def propose(self, batches: list) -> dict:
        def fn(d):
            h = self.current(d)
            if not h:
                raise Conflict("no active handover")
            if handover_mode(h) is not HandoverMode.BATCHES or h["status"] not in ("sent", "proposed"):
                raise Conflict(f"handover {h['id']} ({h['mode']}, {h['status']}) takes no batch proposal")
            h["batches"] = check_batches(h, batches)
            h["status"] = "proposed"
            return h
        return self.update(fn)

    def set_state(self, store: Store, tid: str, state: str, note: str = "") -> tuple:
        """Mark a refactor of the active handover in progress or done. Returns
        (handover, thread). The handover is done when all its refactors are."""
        if state not in ("in-progress", "done"):
            raise ValueError("state must be in-progress or done")

        def fn(d):
            h = self.current(d)
            if not h or tid not in h["refactors"]:
                raise Conflict(f"thread {tid} is not part of an active handover")
            if h["status"] in ("sent", "proposed") and handover_mode(h) is HandoverMode.BATCHES:
                raise Conflict("the batches are not approved yet: call propose_batches, then wait for the human")

            def upd(td):
                th = find(td, tid)
                th["refactor"].update(state=state, note=note or th["refactor"].get("note"))
                th["updated"] = stamp()
                return th, {t["id"]: t.get("refactor", {}).get("state") for t in td["threads"]}
            th, states = store.update(upd)
            h["status"] = "done" if all(states.get(i) == "done" for i in h["refactors"]) else "in-progress"
            return h, th
        return self.update(fn)


def get(d: dict, hid: str) -> dict:
    for h in d["handovers"]:
        if h["id"] == hid:
            return h
    raise NotFound(f"no handover {hid}")


def handover_mode(h: dict) -> HandoverMode:
    """Parse the persisted/API string at the handover boundary."""
    return HandoverMode(h["mode"])


def check_batches(h: dict, batches) -> list:
    """Batches as the agent or the human gave them, checked: every refactor of
    the handover appears in exactly one batch."""
    if not isinstance(batches, list) or not batches:
        raise ValueError("batches must be a non-empty list")
    out, seen = [], []
    for b in batches:
        if not isinstance(b, dict) or not isinstance(b.get("refactors"), list) or not b["refactors"]:
            raise ValueError("each batch needs a non-empty refactors list of thread ids")
        out.append({"title": str(b.get("title") or f"Batch {len(out) + 1}"),
                    "refactors": [str(x) for x in b["refactors"]], "notes": str(b.get("notes") or "")})
        seen += out[-1]["refactors"]
    extra = [x for x in seen if x not in h["refactors"]]
    twice = sorted({x for x in seen if seen.count(x) > 1})
    missing = [x for x in h["refactors"] if x not in seen]
    if extra or twice or missing:
        raise ValueError("every planned refactor must be in exactly one batch"
                         + (f"; not in this handover: {', '.join(extra)}" if extra else "")
                         + (f"; more than once: {', '.join(twice)}" if twice else "")
                         + (f"; missing: {', '.join(missing)}" if missing else ""))
    return out


def planned(d: dict) -> list:
    """Planned refactors that are not done, in the human's order."""
    ts = [t for t in d["threads"] if t["status"] == "planned" and t.get("refactor", {}).get("state") != "done"]
    return sorted(ts, key=lambda t: (t["refactor"].get("order", 0), t["created"]))


# --- locking threads in as planned refactors ---

def lock(store: Store, tid: str, title: str = "", summary: str = "") -> dict:
    def fn(d):
        th = find(d, tid)
        if th["status"] == "planned":
            raise Conflict(f"thread {tid} is already a planned refactor")
        order = max((t.get("refactor", {}).get("order", 0) for t in d["threads"] if t["status"] == "planned"),
                    default=0) + 1
        th["status"] = "planned"
        th["refactor"] = {"title": title or th["label"], "summary": summary or "", "locked": stamp(),
                          "order": order, "state": "planned", "note": None}
        th["updated"] = stamp()
        return th
    return store.update(fn)


def unlock(store: Store, handovers: Handovers, tid: str) -> dict:
    def fn(d):
        if tid in handovers.active_ids():
            raise Conflict(f"thread {tid} is part of the active handover; cancel it first")
        th = find(d, tid)
        if th["status"] != "planned":
            raise Conflict(f"thread {tid} is not a planned refactor")
        th["status"] = "open"
        th.pop("refactor", None)
        th["updated"] = stamp()
        return th
    with handovers.locked():
        return store.update(fn)


def edit(store: Store, tid: str, title: str | None = None, summary: str | None = None) -> dict:
    def fn(d):
        th = find(d, tid)
        if th["status"] != "planned":
            raise Conflict(f"thread {tid} is not a planned refactor")
        if title is not None:
            th["refactor"]["title"] = str(title)
        if summary is not None:
            th["refactor"]["summary"] = str(summary)
        th["updated"] = stamp()
        return th
    return store.update(fn)


def reorder(store: Store, ids: list) -> list:
    """Put the planned refactors in the order of ids. Planned refactors not in
    ids keep their relative order after them. Returns the planned threads."""
    if not isinstance(ids, list):
        raise ValueError("ids must be a list of thread ids")

    def fn(d):
        plan = sorted((t for t in d["threads"] if t["status"] == "planned"),
                      key=lambda t: (t["refactor"].get("order", 0), t["created"]))
        known = {t["id"] for t in plan}
        bad = [i for i in ids if i not in known]
        if bad or len(set(ids)) != len(ids):
            raise ValueError("ids must be distinct planned refactors" + (f"; not planned: {', '.join(bad)}" if bad else ""))
        rank = {i: n for n, i in enumerate(ids)}
        plan.sort(key=lambda t: rank.get(t["id"], len(ids)))
        for n, t in enumerate(plan, 1):
            t["refactor"]["order"] = n
        return plan
    return store.update(fn)


def progress(h: dict, d: dict) -> str:
    """One line about the agent's progress on a handover, for status and activity."""
    ths = {t["id"]: t for t in d["threads"]}
    n = len(h["refactors"])
    done = sum(1 for i in h["refactors"] if ths.get(i, {}).get("refactor", {}).get("state") == "done")
    for k, i in enumerate(h["refactors"], 1):
        r = ths.get(i, {}).get("refactor") or {}
        if r.get("state") == "in-progress":
            return f"Working on refactor {k} of {n}: {r.get('title', i)}"
    if h["status"] == "done":
        return f"Finished all {n} refactors"
    if h["status"] == "sent":
        return f"{n} planned refactors sent" + (", waiting for a batch proposal"
                                                 if handover_mode(h) is HandoverMode.BATCHES else "")
    if h["status"] == "proposed":
        return f"Batches proposed for {n} refactors, waiting for approval"
    return f"{done} of {n} refactors done"
