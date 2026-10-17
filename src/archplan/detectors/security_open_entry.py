"""security.open_entry: an entry point declared `auth=none` without a reason.

An open entry point is a decision, such as a health check or a public
webhook with its own signature check. The reason records why, so a reviewer
can tell a decision from a mistake. One finding per entry point."""

from __future__ import annotations

from .common import Weight, finding
from .security_common import decls, target

ID = "security.open_entry"
NAME = "Open entry point without a reason"
GROUP = "security"
NEEDS = {"declarations"}
PARAMS = {}


def run(ctx, params) -> list:
    out = []
    for d in sorted(decls(ctx, "entrypoint", "auth"), key=lambda d: d["target"].get("id") or ""):
        if d.get("value") != "none" or (d.get("reason") or "").strip():
            continue
        t = target(ctx, d)
        where = f"{d['file']}:{d['line']}" if d.get("file") else None
        out.append(finding(
            ID, (t["id"],),
            f"{t['name']} is open to callers without auth, and no reason says why.",
            f"Declared auth=none{' at ' + where if where else ''}.",
            "medium", "auth=none without a reason", [t["component"]], [d.get("file")],
            {"entry": t["id"], "declared_at": where,
             "moves": ['Add a reason, such as `auth=none reason="load balancer health check"`.',
                       "Or declare `auth=required` if callers must sign in."]},
            weight=Weight(45, "auth=none without a reason")))
    return out
