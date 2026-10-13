"""types.fields: a type with many fields, or with groups of related fields."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, finding, plural, types_focus
from .fields import is_flag

ID = "types.fields"
NAME = "Large types"
GROUP = "types"
NEEDS = {"fields"}
LARGE_TYPE = 25          # fields
CLUSTER_GROUPS = 3       # prefix groups ...
CLUSTER_SIZE = 3         # ... of at least this many fields
FLAGS = 6                # boolean fields
PARAMS = {"large_type": LARGE_TYPE, "cluster_groups": CLUSTER_GROUPS, "cluster_size": CLUSTER_SIZE, "flags": FLAGS}



def run(ctx, params) -> list:
    return field_findings(ctx.model, params["large_type"], params["cluster_groups"], params["cluster_size"],
                          params["flags"])


def field_findings(model: dict, large: int = LARGE_TYPE, n_groups: int = CLUSTER_GROUPS, group_size: int = CLUSTER_SIZE,
                   n_flags: int = FLAGS) -> list:
    out = []
    for tid, t in sorted((model.get("types") or {}).items()):
        fields = [f for f in t.get("fields") or [] if f.get("name")]
        if len(fields) < max(n_groups * group_size, n_flags):
            continue
        groups = defaultdict(list)
        for f in fields:
            head, sep, _ = f["name"].lstrip("_").partition("_")
            if sep and head:
                groups[head].append(f["name"])
        clusters = {k: v for k, v in groups.items() if len(v) >= group_size and k not in ("is", "has", "can")}
        flags = [f["name"] for f in fields if is_flag(f)]
        why = []
        if len(fields) >= large:
            why.append(f"{len(fields)} fields")
        if len(clusters) >= n_groups:
            why.append(plural(len(clusters), "group") + " of related fields")
        if len(flags) >= n_flags:
            why.append(plural(len(flags), "boolean flag"))
        if not why:
            continue
        name = t.get("name") or tid
        if len(clusters) >= n_groups:
            title = f"{tid} has {plural(len(clusters), 'group')} of related fields."
        elif len(fields) >= large:
            title = f"{tid} has {len(fields)} fields."
        else:
            title = f"{tid} has {len(flags)} boolean flags."
        detail = " · ".join([f"{k}_* ×{len(v)}" for k, v in sorted(clusters.items(), key=lambda kv: -len(kv[1]))]
                            + ([plural(len(flags), "boolean flag")] if len(flags) >= n_flags else []))
        size = len(fields)
        strength = "strong" if len(why) >= 2 or size >= 2 * large else "medium"
        out.append(finding(
            "types.fields", (tid,), title, detail or ", ".join(why), strength, "; ".join(why),
            [t.get("component")], [t.get("file")],
            {"type": tid, "name": name, "fields": size,
             "groups": {k: v[:EXAMPLES] for k, v in sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:8]},
             "flags": flags[:EXAMPLES], **({"line": t["line"]} if t.get("line") else {})},
            focus=types_focus({tid: [n for v in clusters.values() for n in v] + flags}, [t.get("component")],
                              [tid]),
            weight=Weight(30, "large type").spread(size, "fields", per=8, cap=16, free=large // 2)
            .spread(len(clusters), "field groups", per=6, cap=12, free=n_groups - 1)
            .spread(len(flags), "boolean flags", per=4, cap=8, free=n_flags - 1)
            .add("reasons", len(why), 6 * (len(why) - 1))))
    return out
