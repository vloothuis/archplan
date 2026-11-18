"""Weakness tags for security findings: the CWE identifiers a finding kind
stands for, and its category in the OWASP Top 10 of 2021 and of 2025. Other
tools join findings on these, such as code scanning and a pentest report.

A CWE (Common Weakness Enumeration) names a kind of flaw. A kind without a
single matching CWE, such as security.untraced, has no tags."""

from __future__ import annotations

OWASP = {
    "2021": {"A01": "Broken Access Control", "A03": "Injection", "A04": "Insecure Design",
             "A07": "Identification and Authentication Failures"},
    "2025": {"A01": "Broken Access Control", "A05": "Injection", "A06": "Insecure Design",
             "A07": "Authentication Failures"},
}
CWE = {
    20: "Improper Input Validation",
    284: "Improper Access Control",
    306: "Missing Authentication for Critical Function",
    501: "Trust Boundary Violation",
    639: "Authorization Bypass Through User-Controlled Key",
    862: "Missing Authorization",
}
AUTHZ = ([862], {"2021": "A01", "2025": "A01"})
AUTHN = ([306], {"2021": "A07", "2025": "A07"})
KINDS = {
    "security.unguarded_sink": AUTHZ,
    "security.unguarded_path": AUTHZ,
    "security.guard_outlier": AUTHZ,
    "security.unchecked_entry": ([862, 639], AUTHZ[1]),
    "security.gateway_bypass": ([284], AUTHZ[1]),
    "security.open_entry": AUTHN,
    "security.unchecked_input": ([20], {"2021": "A03", "2025": "A05"}),
    "security.zone_dependency": ([501], {"2021": "A04", "2025": "A06"}),
}


def of(f: dict) -> tuple | None:
    """([CWE number], {"2021": category, "2025": category}) for a finding, or None."""
    if f.get("kind") == "security.unguarded_entry":
        # An entry point that needs a permission check lacks authorization; any other lacks authentication.
        return AUTHZ if (f.get("evidence") or {}).get("auth") == "authz" else AUTHN
    return KINDS.get(f.get("kind"))


def tag(f: dict) -> dict:
    """Set f["cwe"] (such as ["CWE-862"]) and f["owasp"] when the kind has them."""
    w = of(f)
    if w:
        f["cwe"] = [f"CWE-{n}" for n in w[0]]
        f["owasp"] = dict(w[1])
    return f


def names(f: dict) -> dict:
    """The tags of a finding with their names, for a reader."""
    out = {}
    if f.get("cwe"):
        out["cwe"] = [f"{c}: {CWE.get(int(c[4:]), '')}".rstrip(": ") for c in f["cwe"]]
    if f.get("owasp"):
        out["owasp"] = [f"{k}:{y} {OWASP[y].get(k, '')}".rstrip() for y, k in sorted(f["owasp"].items())]
    return out
