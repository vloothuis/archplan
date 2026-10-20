"""archplan: plan architecture changes with a human in an editor and an agent over MCP.

  archplan init [--export CMD]   set up .archplan/ in this repository
  archplan export                run the export command: code -> as-is model
  archplan up [--open]           start the editor in the background; print its URL
  archplan down                  stop it
  archplan serve                 run the editor in the foreground
  archplan status [--json]       editor URL, model size, threads waiting
  archplan threads [--waiting agent|human|any]
  archplan mcp                   MCP server on stdio, for an agent
  archplan watch                 one line per human comment, plan change or decided suggestion
  archplan hotspots [--since N]  components that change often, and ones that change together
  archplan blast [--diff REF] [FILE ...]  what traced entry points reach, what a change touches,
                                 and the security tests to add
  archplan profile [infer|show|list|use NAME]  the architecture profile: roles, rules and where the code breaks them
  archplan check [--format github] [--tighten]  new findings (cycles, rule breaks, ...) and the ratchet;
                                 exit 1 when there are new findings or a ratchet metric got worse
  archplan detectors [enable|disable|trust|run ID]  the checks behind the findings, and their settings
  archplan annotators [trust ID|read|write FILE [--apply]]  declarations in the code (zones, guards, auth):
                                 who reads and writes them, and changes to them as a patch
  archplan schema [show|tables] [--json] [--sqlite FILE] [--postgres --url-env VAR]
                                 database tables, their owners and who reads or writes them
  archplan glossary candidates   candidate glossary terms per component, from tf-idf over the code
  archplan hook                  tell the agent about new comments
  archplan export-python         built-in exporter for Python projects
  archplan export-lite           approximate exporter for Java, Kotlin, Scala, C#, PHP, Ruby, Rust and Swift
  archplan exporters             which exporters match this repository, and whether they are installed
  archplan export-check FILE     check a model against the exporter plugin contract
  archplan export-elixir         exporter for Elixir/Mix projects (needs mix)
  archplan export-ts             exporter for TypeScript and JavaScript projects (needs node)
  archplan trace-elixir          run mix tests and record call sequences
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .project import Project, ProjectError, changed_note, counts, find_root, init, read_json, write_json
from .handover import Handovers, HandoverMode, handover_mode
from .store import Store, mentions, needs, who_next


def die(msg: str, code: int = 1):
    print(f"archplan: {msg}", file=sys.stderr)
    sys.exit(code)


def project(a) -> Project:
    try:
        return Project.find(dir=a.dir)
    except ProjectError as e:
        die(str(e))


def server_status(p: Project) -> dict | None:
    """This project's editor server as it answers: url, pid, build; or None."""
    info = read_json(p.path("server.json"))
    if not info:
        return None
    try:
        with urllib.request.urlopen(info["url"] + "api/info", timeout=1.5) as r:
            d = json.load(r)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if d.get("root") != str(p.root) or d.get("pid", info.get("pid")) != info.get("pid"):
        return None
    return {"url": info["url"], "port": info.get("port"), "pid": info.get("pid"),
            "build": d.get("build"), "archplan_version": d.get("archplan_version")}


def running(p: Project) -> str | None:
    """The URL of this project's editor server when it answers."""
    s = server_status(p)
    return s and s["url"]


def stop_server(pid, wait: float = 5.0) -> bool:
    """SIGTERM an archplan process and wait for it to end. Refuses other processes."""
    try:
        cmd = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True).stdout
    except (TypeError, ValueError):
        return False
    if "archplan" not in cmd:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        return False
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    return True


# ---------- commands ----------

def cmd_init(a):
    root = Path(a.root or ".").resolve()
    if (r := find_root(root)) and r != root and not a.force:
        die(f"{r} already has an archplan project; run archplan there, or pass --force")
    export = a.export[0] if a.export and len(a.export) == 1 else a.export
    try:
        p = init(root, export, a.port, a.force)
    except ProjectError as e:
        die(str(e))
    print(f"created {p.dir}/config.json")
    if p.export_command():
        print(f"export command: {p.export_command()}")
        if p.trusted():
            print("next: archplan export && archplan up")
        else:
            found = getattr(p, "found", None) or {}
            why = ("it comes from this repository's .archplan/exporters" if found.get("trusted_source") is False
                   else "it runs code from this repository (for example the project's own compiler or build files)")
            print(f"not approved yet: {why}. Read the command, then run `archplan trust` in a terminal.")
            print("next: archplan trust && archplan export && archplan up")
        found = getattr(p, "found", None) or {}
        pick = next((r for r in found.get("exporters") or [] if r["name"] == found.get("pick")), None)
        others = [r for r in found.get("exporters") or []
                  if pick and r.get("command") and r["name"] not in (pick["name"], "lite") and r["match"] != pick["match"]]
        for r in others[:3]:
            print(f"{r['name']} also matches {r['match']}/. To export both, make \"export\" a list of commands that "
                  "each write to {out} (see `archplan exporters`).")
    else:
        print("no exporter for this language yet: set \"export\" in config.json to a command that writes "
              f"{p.model_path.relative_to(p.root)} (format: archmodel/1), then run archplan export")


def shown(cmd: str) -> str:
    """The command with control and invisible characters escaped, so a prompt
    shows exactly what will run."""
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in cmd)


def confirm_trust(p: Project) -> None:
    """Ask a person, at a terminal, to approve the export command before it
    first runs here. There is no flag to skip this: an agent must not be able
    to approve a command a cloned repository supplied."""
    ok, changed = p.trust_state()
    if ok or not p.export_command():
        return
    if changed:
        print(f"The export command in {p.dir / 'config.json'} was approved, but files it runs changed since:\n  "
              + "\n  ".join(shown(f) for f in changed) + f"\nCommand:\n  {shown(p.export_command())}", file=sys.stderr)
    else:
        print(f"The export command in {p.dir / 'config.json'} is new on this machine:\n  {shown(p.export_command())}",
              file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        die("not approved; run `archplan trust` in a terminal after reading the command")
    sys.stderr.write("Run it, and allow the agent and the editor to run it from now on? [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        die("not approved")
    p.trust()


def cmd_trust(a):
    p = project(a)
    confirm_trust(p)
    print(f"approved: {shown(p.export_command())}")


def cmd_export(a):
    p = project(a)
    if not p.export_command():
        die("no export command; set \"export\" in .archplan/config.json (see `archplan exporters`)")
    confirm_trust(p)
    print(f"running: {shown(p.export_command())}", file=sys.stderr)
    t = time.monotonic()
    try:
        c = p.export()
    except ProjectError as e:
        die(str(e))
    print(f"{p.model_path.relative_to(p.root)}: " + ", ".join(f"{v} {k}" for k, v in c.items())
          + f" ({time.monotonic() - t:.1f}s)")


def cmd_serve(a):
    from .server import serve
    p = project(a)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        serve(p, a.bind, a.port)
    except KeyboardInterrupt:
        pass
    except ProjectError as e:
        die(str(e))


def cmd_up(a):
    from .reload import BUILD
    p = project(a)
    srv = server_status(p)
    url = srv and srv["url"]
    port = a.port
    if srv and srv["build"] != BUILD and srv["pid"] != os.getpid():
        # A server with other code (an older plugin version, or a checkout before
        # a pull): stop it and start this code on the same port.
        print(f"replacing the editor server {srv['build'] or 'of an older archplan'} with {BUILD}", file=sys.stderr)
        if stop_server(srv["pid"]):
            p.path("server.json").unlink(missing_ok=True)
            url, port = None, port or srv["port"]
    if not url:
        if not p.model_path.exists() and p.export_command():
            print("no model yet; exporting first", file=sys.stderr)
            cmd_export(a)
        args = [sys.executable, "-m", "archplan", "serve", "--dir", str(p.root)]
        if port:
            args += ["--port", str(port)]
        with open(p.path("server.log"), "a") as log:
            subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=p.root, env=os.environ.copy())
        for _ in range(50):
            time.sleep(0.1)
            if url := running(p):
                break
        else:
            die(f"the editor did not start; see {p.path('server.log')}")
    print(url)
    if a.open:
        webbrowser.open(url)


def cmd_down(a):
    p = project(a)
    info = read_json(p.path("server.json"))
    if not info:
        return print("not running")
    if stop_server(info.get("pid")):
        print(f"stopped {info['url']}")
    else:
        print("not running")
    p.path("server.json").unlink(missing_ok=True)


def cmd_status(a):
    from .mcp import Agent
    p = project(a)
    s = Agent(p).status()
    if a.json:
        return print(json.dumps(s, indent=2, ensure_ascii=False))
    print(f"project  {s['project']}  ({s['root']})")
    print(f"editor   {running(p) or 'not running (archplan up)'}")
    m = s["model"]
    print("model    " + (", ".join(f"{v} {k}" for k, v in m.items()) if isinstance(m, dict) else m))
    if s["exclude"]:
        print(f"exclude  {', '.join(s['exclude'])}")
    if s["intent"]:
        print(f"intent   {s['intent']}")
    print(f"plan     {s['plan_tasks']} tasks, {s['target']['violations']} policy violations in the target")
    w = s["threads"]["waiting_on_agent"]
    print(f"threads  {len(w)} waiting on the agent, {s['threads']['waiting_on_human']} on the human")
    for t in w:
        print(f"  {t['id']}  {t['label'][:60]}: {t['last']['text'][:100]}")
    h = s["handover"]
    print(f"refactors {s['planned_refactors']} planned"
          + (f"; handover {h['id']} ({h['mode']}, {h['status']}): {h['progress']}" if h else ""))


def cmd_threads(a):
    p = project(a)
    act = Handovers(p.dir).active_ids()
    for t in Store(p.dir).read()["threads"]:
        if t["status"] != "open" and not a.all:
            continue
        if a.waiting != "any" and who_next(t, act) != a.waiting:
            continue
        flag = needs(t, act) or ""
        print(f"{t['id']}  [{t['status']}, waiting on {who_next(t, act)}{', ' + flag if flag else ''}]  {t['label']}")
        for m in t["messages"]:
            print(f"    {m['author']}: {' '.join(mentions(m['text']).split())[:300]}")


def cmd_mcp(a):
    from . import mcp
    mcp.serve(lambda: Project.find(dir=a.dir))


def cmd_watch(a):
    from .watch import watch
    try:
        while True:
            try:
                p = Project.find(dir=a.dir)
                break
            except ProjectError:
                if a.dir:
                    raise
                time.sleep(5)  # wait quietly until `archplan init` runs here
        watch(p, a.interval)
    except KeyboardInterrupt:
        pass


def cmd_hook(a):
    """For SessionStart and UserPromptSubmit hooks. Prints (into the
    agent's context) the threads waiting on the agent that it was not told
    about yet in this session. Silent outside archplan projects."""
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        ev = {}
    root = find_root(Path(ev.get("cwd") or "."))
    if not root:
        return
    p = Project(root)
    seen_path = p.path(".hook-seen.json")
    seen = read_json(seen_path, {}) or {}
    sid = ev.get("session_id", "")
    if seen.get("session") != sid:
        seen = {"session": sid, "threads": {}}
    hs = Handovers(p.dir)
    act = hs.active_ids()
    new = []
    for t in Store(p.dir).read()["threads"]:
        if who_next(t, act) == "agent" and seen["threads"].get(t["id"]) != len(t["messages"]):
            new.append(t)
            seen["threads"][t["id"]] = len(t["messages"])
    # A handover waits on the agent when sent, or when its batches were approved.
    h = hs.current()
    ho = f"{h['id']}:{h['status']}" if h and h["status"] in ("sent", "approved") else None
    ho_new = ho is not None and seen.get("handover") != ho
    if ho_new:
        seen["handover"] = ho
    if ev.get("hook_event_name") == "SessionStart" or new or ho_new:
        write_json(seen_path, seen)
    if not new and not ho_new:
        return
    url = running(p)
    lines = []
    if ho_new:
        what = ("the batches are approved: work batch by batch" if h["status"] == "approved"
                else "implement them in order" if handover_mode(h) is HandoverMode.ALL else "propose batches first")
        lines.append(f"archplan: the human sent {len(h['refactors'])} planned refactors in {p.name} "
                     f"(handover {h['id']}); {what}. Call get_handover for the details.")
    if new:
        lines.append(f"archplan: {len(new)} comment thread(s) in {p.name} wait on you"
                     + (f" (editor {url})" if url else "") + ". Read them with the archplan MCP tools "
                     "(read_thread, then reply in the thread); use the /archplan skill for the workflow.")
    for t in new[:10]:
        last = " ".join(mentions(t["messages"][-1]["text"]).split())
        lines.append(f"- {t['id']} on {t['label'][:80]}: {last[:200]}")
    print("\n".join(lines))


def cmd_hotspots(a):
    from . import hotspots
    p = project(a)
    t = time.monotonic()
    try:
        res = hotspots.hotspots(p, a.since, refresh=a.refresh)
    except hotspots.NotGit as e:
        die(f"no git history here: {e}")
    if a.json:
        return print(json.dumps(res, indent=2, ensure_ascii=False))
    print(hotspots.text(res))
    print(f"\n({time.monotonic() - t:.1f}s; cached in {p.path('hotspots.json').relative_to(p.root)})", file=sys.stderr)


def cmd_blast(a):
    from . import blast
    from .detectors import Context
    ctx = Context(project(a))
    files = a.files or None
    if a.diff:
        try:
            files = blast.changed(ctx, a.diff) + (files or [])
        except ValueError as e:
            die(str(e))
    rep = blast.report(ctx, files, a.limit)
    print(json.dumps(rep, indent=2, ensure_ascii=False) if a.json else blast.text(rep))


def cmd_profile(a):
    from . import architecture
    p = project(a)
    arch = architecture.Architecture(p)
    try:
        if a.action == "list":
            from . import frameworks
            model = arch.model()
            res = frameworks.listing(model, architecture.source_files(model, p.root), p.root, p.dir)
            return print(json.dumps(res, indent=2) if a.json else frameworks.listing_text(res))
        if a.action == "use" and not a.name:
            die("name a profile: archplan profile use <name> (see archplan profile list)")
        if a.action in ("infer", "use"):
            proposal, saved = arch.use(a.name) if a.action == "use" else arch.infer()
            print(f"proposed a profile ({proposal['source']}): "
                  + (f"saved to {arch.path.relative_to(p.root)}" if saved
                     else "not saved, the profile is locked in; showing the locked one"), file=sys.stderr)
        st = arch.state()
    except architecture.ArchitectureError as e:
        die(str(e))
    if a.json:
        return print(json.dumps(st, indent=2, ensure_ascii=False))
    if st.get("profile") is None:
        print("No profile saved yet; this is the proposal. Save it with `archplan profile infer`.\n")
    print(architecture.text(st))


def cmd_check(a):
    """Exit 1 when there are new findings or a ratchet metric got worse, 0
    when neither, 2 on errors."""
    from . import findings, ratchet
    try:
        p = Project.find(dir=a.dir)
    except ProjectError as e:
        die(str(e), 2)
    if a.export:
        ok, changed = p.trust_state()
        if not ok:
            if not (sys.stdin.isatty() and sys.stderr.isatty()):
                die("the export command is not approved on this machine" + changed_note(changed)
                    + "; run `archplan trust` in a terminal, "
                    "or run the exporter yourself before `archplan check`", 2)
            confirm_trust(p)
        print(f"running: {shown(p.export_command() or '')}", file=sys.stderr)
        try:
            p.export()
        except ProjectError as e:
            die(str(e), 2)
    try:
        f = findings.Findings(p)
        f.execute = True  # external detectors run here, not in the editor
        if a.update_baseline:
            r = f.update_baseline()
            print(f"baseline: {r['added']} accepted, {r['removed']} removed, {r['total']} in "
                  f"{f.baseline.path.relative_to(p.root)}", file=sys.stderr)
        if a.min_weight is not None and not 0 <= a.min_weight <= 100:
            die("--min-weight must be 0 to 100", 2)
        if a.loosen is not None and not a.loosen.strip():
            die("--loosen needs a reason", 2)
        if a.loosen is not None:
            r = f.ratchet_write("loosen", a.loosen, a.min_weight)
            print(f"ratchet: loosened, {len(r['changed'])} metrics changed", file=sys.stderr)
        elif a.tighten or a.update_baseline:
            r = f.ratchet_write("tighten", None, a.min_weight)
            ch = r["changed"]
            msg = (f"started with {len(ch)} metrics" if r["started"] else "nothing got better" if not ch else
                   "tightened " + ", ".join(f"{k} {ratchet.fmt(o)} -> {ratchet.fmt(n)}" for k, (o, n) in ch.items()))
            print(f"ratchet: {msg}", file=sys.stderr)
        full = f.listing()
        rat = f.ratchet(full, a.min_weight)
        lst = findings.above(full, findings.min_weight(p, a.min_weight))
    except ProjectError as e:
        die(str(e), 2)
    new, worse = lst["counts"]["new"], rat["worse"]
    if a.json:
        out = {**lst, "findings": [x for x in lst["findings"] if a.all or x["state"] == "new"], "ratchet": rat}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif a.format == "github":
        text = "\n".join(x for x in (findings.github(lst, a.all), ratchet.github(rat)) if x)
        if text:
            print(text)
        print(f"archplan: {new} new findings, {worse} ratchet metrics worse", file=sys.stderr)
    else:
        print(findings.text(lst, a.all) + "\n\n" + ratchet.text(rat))
    sys.exit(1 if new or worse else 0)


def cmd_detectors(a):
    """List the detectors, switch one on or off, approve or run one."""
    from . import detectors
    from .detectors import external
    p = project(a)
    reg, _ = detectors.registry(p)
    if a.action != "list":
        if not a.id:
            die(f"usage: archplan detectors {a.action} <id>")
        if a.id not in reg:
            die(f"no detector {a.id}; see `archplan detectors`")
    if a.action in ("enable", "disable"):
        detectors.configure(p, a.id, enabled=a.action == "enable")
        return print(f"{a.id}: {a.action}d in {p.path('config.json').relative_to(p.root)}")
    if a.action == "trust":
        spec = reg[a.id]
        ok, changed = external.trust_state(p.root, spec)
        if ok:
            return print(f"{a.id}: nothing to approve")
        if changed:
            print(f"The command of detector {a.id} ({spec['file']}) was approved, but files it runs changed since:\n  "
                  + "\n  ".join(shown(f) for f in changed) + f"\nCommand:\n  {shown(spec['command'])}",
                  file=sys.stderr)
        else:
            print(f"The command of detector {a.id} ({spec['file']}) is new on this machine:\n  {shown(spec['command'])}",
                  file=sys.stderr)
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            die("not approved; run `archplan detectors trust` in a terminal after reading the command")
        sys.stderr.write("Allow archplan to run it from now on? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            die("not approved")
        external.trust(p.root, spec)
        return print(f"approved: {shown(spec['command'])}")
    try:
        res = detectors.run(p, only={a.id} if a.action == "run" else None, execute=a.action == "run",
                            include_disabled=a.action == "run")
    except ProjectError as e:
        die(str(e))
    if a.action == "run":
        if a.json:
            return print(json.dumps(res, indent=2, ensure_ascii=False))
        st = res["detectors"][0]
        for f in res["findings"]:
            print(f"  {f['strength']:<6}  {f['title']}")
        for m in res["missing"]:
            print(f"Not run: {m['reason']}.")
        return print(f"{a.id}: {plural_findings(st['count'])}" + (f" in {st['seconds']}s" if "seconds" in st else ""),
                     file=sys.stderr)
    if a.json:
        return print(json.dumps({k: res[k] for k in ("detectors", "missing", "capabilities")}, indent=2,
                                ensure_ascii=False))
    print(detectors.text(res))


def cmd_annotators(a):
    """List the annotators and their keys, approve one, read the declarations
    or write changes to them."""
    from . import annotators
    p = project(a)
    specs, errors = annotators.load(p)
    if a.action == "trust":
        if not a.arg or a.arg not in specs:
            die(f"usage: archplan annotators trust <id>; ids: {', '.join(sorted(specs)) or 'none'}")
        spec = specs[a.arg]
        ok, changed = annotators.trust_state(p.root, spec)
        if ok:
            return print(f"{a.arg}: nothing to approve")
        what = "was approved, but files it runs changed since:\n  " + "\n  ".join(shown(f) for f in changed) \
            if changed else "is new on this machine:"
        print(f"The annotator {a.arg} ({spec['file']}) {what}\nCommands:\n  "
              + "\n  ".join(shown(c) for c in (spec["read"], spec.get("write")) if c), file=sys.stderr)
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            die("not approved; run `archplan annotators trust` in a terminal after reading the commands")
        sys.stderr.write("Allow archplan to run them from now on? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            die("not approved")
        annotators.trust(p.root, spec)
        return print(f"approved: {a.arg}")
    if a.action == "write":
        if not a.arg:
            die("usage: archplan annotators write FILE|- [--apply]; FILE holds {\"changes\": [...]}")
        try:
            data = json.loads(sys.stdin.read() if a.arg == "-" else Path(a.arg).read_text())
        except (OSError, ValueError) as e:
            die(f"cannot read the changes: {e}")
        changes = data.get("changes") if isinstance(data, dict) else data
        try:
            res = annotators.write(p, changes, apply=a.apply)
        except ProjectError as e:
            die(str(e))
        if a.json:
            return print(json.dumps(res, indent=2, ensure_ascii=False))
        for r in res["results"]:
            c = r["change"] if isinstance(r["change"], dict) else {}
            t = c.get("target") if isinstance(c.get("target"), dict) else {}
            print(f"  {r['status']:<11} {c.get('op', '?')} {t.get('kind', '?')}.{c.get('key', '?')} "
                  f"of {t.get('id', '?')}" + (f": {r['note']}" if r.get("note") else ""), file=sys.stderr)
        if res["applied"]:
            return print(f"applied to {', '.join(res['files'])}", file=sys.stderr)
        if res["patch"]:
            sys.stdout.write(res["patch"])
            print("not applied; run again with --apply", file=sys.stderr)
        return
    if a.action == "read":
        res = annotators.read(p)
        if a.json:
            return print(json.dumps(res, indent=2, ensure_ascii=False))
        for d in res["declarations"]:
            where = f"  ({d['file']}:{d.get('line', '?')})" if d.get("file") else ""
            print(f"  {d['target']['kind']}.{d['key']} = {json.dumps(d['value'])}  {d['target']['id']}{where}"
                  + (f"  reason: {d['reason']}" if d.get("reason") else ""))
        for w in res["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        return print(f"{len(res['declarations'])} declarations", file=sys.stderr)
    own, conflicts = annotators.owners(p, specs)
    if a.json:
        return print(json.dumps({"owners": own, "conflicts": conflicts,
                                 "annotators": [dict(s, approved=annotators.trust_state(p.root, s)[0])
                                                for s in specs.values()],
                                 "errors": [{"file": f, "error": e} for f, e in errors]}, indent=2))
    for k in annotators.KEYS:
        aid = own.get(k)
        state = "" if aid in (None, annotators.BUILTIN) else \
            "" if annotators.trust_state(p.root, specs[aid])[0] else "  (not approved)"
        print(f"  {k:<16} {aid or 'not read'}{state}")
    for m in conflicts + [f"{f}: {e}" for f, e in errors]:
        print(f"warning: {m}", file=sys.stderr)


def plural_findings(n: int) -> str:
    return f"{n} finding{'' if n == 1 else 's'}"


def cmd_schema(a):
    """Show the model's schema section. --refresh rebuilds it from the sources
    in the repository; --sqlite and --postgres read a live database, only when
    asked."""
    from . import schema
    from .schema import live
    p = project(a)
    model = read_json(p.model_path)
    if not model:
        die("no model yet; run `archplan export` first")
    snapshot = None
    try:
        if a.sqlite:
            path = Path(a.sqlite)
            snapshot = {"kind": "sqlite", **live.sqlite(path)}
            try:
                snapshot["path"] = path.resolve().relative_to(p.root).as_posix()
            except ValueError:
                snapshot["path"] = path.name
        elif a.postgres:
            if not a.url_env:
                die("--postgres needs --url-env VAR: the environment variable that holds the database URL")
            snapshot = {"kind": "postgres", **live.postgres(a.url_env)}
    except live.LiveError as e:
        die(str(e))
    if a.drop_live:
        p.path(schema.LIVE).unlink(missing_ok=True)
    sch = model.get("schema")
    if snapshot is not None or a.refresh or a.drop_live or not sch:
        sch = schema.enrich(p, live=snapshot, force=True)
    if not sch:
        die("no database schema found: no Django models, Ecto schemas, db/schema.rb, schema.prisma, schema.sql or "
            "structure.sql (or use --sqlite FILE, or --postgres --url-env VAR)")
    if a.json:
        return print(json.dumps(sch, indent=2, ensure_ascii=False))
    print(schema.text(sch, a.action))


def cmd_glossary(a):
    from .vocab import Vocab
    p = project(a)
    try:
        res = Vocab(p).candidates(a.component, a.top)
    except (KeyError, ValueError) as e:
        die(str(e).strip("'\""))
    if a.json:
        return print(json.dumps(res, indent=2, ensure_ascii=False))
    for c, x in res["components"].items():
        have = ", ".join(k["name"] for k in x["concepts"])
        print(f"{c} ({x['files']} files)" + (f"; concepts: {have}" if have else ""))
        for t in x["terms"]:
            others = ", ".join(f"{o['component']} {o['count']:g}" for o in t["others"][:3])
            print(f"  {t['score']:6.2f}  {t['term']:<28} df {t['df']:<3} {', '.join(t['samples'][:3])[:60]}"
                  + (f"  (also {others})" if others else ""))
        print()
    print(f"{res['n_components']} components, {res['took_ms']} ms", file=sys.stderr)
    if res.get("warning"):
        print(f"warning: {res['warning']}", file=sys.stderr)


def cmd_exporters(a):
    from . import exporters
    root = Path(a.root or find_root() or ".").resolve()
    res = exporters.survey(root, root / ".archplan")
    print(json.dumps(res, indent=2) if a.json else exporters.text(res))


def cmd_export_check(a):
    """Exit 0 when the model follows the exporter contract, 1 when not, 2 when it cannot be read."""
    from . import modelcheck
    try:
        m = json.loads(Path(a.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        die(f"cannot read {a.file}: {e}", 2)
    res = modelcheck.check(m, Path(a.root) if a.root else None)
    print(json.dumps(res, indent=2) if a.json else modelcheck.text(res))
    sys.exit(0 if res["ok"] else 1)


def cmd_export_lite(a):
    from .liteexport import LANGS, export
    root = Path(a.root or ".").resolve()
    if not root.is_dir():
        die(f"no folder {root}", 2)
    cwd = Path.cwd().resolve()
    rel_to = cwd if root == cwd or cwd in root.parents else root  # paths relative to the repository root
    langs = [x for x in (a.language or [])]
    bad = [x for x in langs if x not in set(LANGS.values())]
    if bad:
        die(f"unknown language: {', '.join(bad)} (known: {', '.join(sorted(set(LANGS.values())))})", 2)
    write_model(export(root, a.tests, langs or None, rel_to), a.output)


def write_model(m: dict, output: str | None) -> None:
    text = json.dumps(m, indent=2, ensure_ascii=False) + "\n"
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text)
        print(f"{output}: " + ", ".join(f"{v} {k}" for k, v in counts(m).items()), file=sys.stderr)
    else:
        sys.stdout.write(text)


def cmd_export_python(a):
    from .pyexport import export
    root = Path(a.root or ".").resolve()
    try:
        m = export(root, a.package, a.granularity, a.tests)
    except ValueError as e:
        die(str(e))
    text = json.dumps(m, indent=2, ensure_ascii=False) + "\n"
    if a.output:
        Path(a.output).parent.mkdir(parents=True, exist_ok=True)
        Path(a.output).write_text(text)
        print(f"{a.output}: " + ", ".join(f"{v} {k}" for k, v in counts(m).items()), file=sys.stderr)
    else:
        sys.stdout.write(text)


def cmd_export_ts(a):
    """Run the shipped Node exporter. It loads the project's own typescript
    package. Sequences come from .archplan/sequences.json, read here as in
    export-python."""
    import shutil

    from .pyexport import read_sequences
    node = shutil.which("node")
    if not node:
        die("export-ts needs Node.js (node on the PATH); install it, then run npm install in the project")
    try:
        sequences = read_sequences(Path(a.root or ".").resolve())
    except ValueError as e:
        die(str(e))
    script = Path(__file__).resolve().parent / "ts" / "archplan_export.mjs"
    args = [node, str(script), "-C", a.C]
    if a.output and not sequences:
        args += ["-o", a.output]
    if a.root:
        args += ["--root", a.root]
    if a.depth:
        args += ["--depth", str(a.depth)]
    if a.tests:
        args.append("--tests")
    from .exclude import load
    ex = load(Path(a.root or ".").resolve())
    if ex:
        args += ["--exclude-base", str(ex.base)] + [x for pat in ex.patterns for x in ("--exclude", pat)]
    if not sequences:
        sys.exit(subprocess.run(args).returncode)
    r = subprocess.run(args, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    if r.returncode:
        sys.exit(r.returncode)
    m = json.loads(r.stdout)
    m["sequences"] = sequences
    if "calls" not in m["source"]["capabilities"]:
        m["source"]["capabilities"].append("calls")
    write_model(m, a.output)


INTRO = """\
archplan: plan an architecture change in a browser editor, then let an agent build it.

The as-is model comes from your code. You edit the to-be model; the difference
becomes a plan. You and the agent discuss any component, type, dependency or
call in comment threads.

Start, in the repository root:
  archplan init      set up .archplan/ (picks an exporter for Go, Python, Elixir or TypeScript)
  archplan export    read the code into .archplan/model.archmodel.json
  archplan up        open the editor (press ? there for help)

Then, to see where the code changes most:
  archplan hotspots  hotspots and hidden coupling from the git history
  archplan profile   the conventions inside units (roles, rules) and where code breaks them
  archplan check     findings: cycles, rule breaks, layer and type problems; also for CI

With Claude Code: install the plugin, then type /archplan in the repository.
  claude plugin marketplace add vloothuis/archplan
  claude plugin install archplan@archplan

All commands: archplan --help
"""


def cmd_help(a):
    print(INTRO, end="")
    root = find_root()
    if root:
        print(f"\nThis repository is set up: {root}/.archplan. Run `archplan status`.")


def elixir(cmd: str, args: list) -> None:
    """Run the shipped Elixir exporter: export-elixir and trace-elixir pass
    their arguments to `archplan-elixir export|trace`."""
    wrapper = Path(__file__).resolve().parent / "elixir" / "archplan-elixir"
    os.execvp("sh", ["sh", str(wrapper), cmd, *args])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("export-elixir", "trace-elixir"):
        if argv[1:2] in (["-h"], ["--help"]):
            argv = [argv[0]]
        elixir(argv[0].split("-")[0], argv[1:])
    ap = argparse.ArgumentParser(prog="archplan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"archplan {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help):
        s = sub.add_parser(name, help=help)
        s.add_argument("--dir", help="project root (default: the nearest directory with .archplan, from here up)")
        s.set_defaults(fn=fn)
        return s

    s = add("init", cmd_init, "set up .archplan/ in a repository")
    s.add_argument("root", nargs="?", help="repository root (default: .)")
    s.add_argument("--export", action="append",
                   help="command that writes the as-is model (default: detected from the language); repeat it to "
                        "merge the models of several exporters, each writing to {out}")
    s.add_argument("--port", type=int)
    s.add_argument("--force", action="store_true")
    s = add("export", cmd_export, "re-export the as-is model from the code")
    s = add("trust", cmd_trust, "approve this repository's export command for the agent and the editor")
    s = add("serve", cmd_serve, "run the editor server in the foreground")
    s.add_argument("--port", type=int)
    s.add_argument("--bind", default="127.0.0.1")
    s = add("up", cmd_up, "start the editor server in the background and print its URL")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open it in the default browser")
    add("down", cmd_down, "stop the background editor server")
    s = add("status", cmd_status, "show the project state")
    s.add_argument("--json", action="store_true")
    s = add("threads", cmd_threads, "print comment threads")
    s.add_argument("--waiting", choices=["agent", "human", "any"], default="any")
    s.add_argument("--all", action="store_true", help="include resolved threads")
    add("mcp", cmd_mcp, "MCP server on stdio")
    s = add("watch", cmd_watch, "print human comments and plan changes as they happen")
    s.add_argument("--interval", type=float, default=2)
    add("hook", cmd_hook, "hook (reads the hook event on stdin)")
    s = add("profile", cmd_profile, "show or infer the architecture profile (roles, rules, breaks)")
    s.add_argument("action", nargs="?", choices=["show", "infer", "list", "use"], default="show")
    s.add_argument("name", nargs="?", help="for use: a profile name from `archplan profile list`, or inferred")
    s.add_argument("--json", action="store_true", help="the profile and the full analysis as JSON")
    s = add("check", cmd_check, "list new findings and the ratchet (exit 1 on new findings or a worse metric), for CI")
    s.add_argument("--json", action="store_true", help="findings, counts and missing kinds as JSON")
    s.add_argument("--format", choices=["text", "github"], default="text",
                   help="github: ::warning annotations for GitHub Actions")
    s.add_argument("--all", action="store_true", help="also list accepted and dismissed findings")
    s.add_argument("--update-baseline", action="store_true",
                   help="accept every current finding into .archplan/baseline.json (reason: baseline)")
    s.add_argument("--tighten", action="store_true",
                   help="write the ratchet metrics that got better into the baseline (--update-baseline does too)")
    s.add_argument("--loosen", metavar="REASON", default=None,
                   help="write every ratchet metric as it is now, worse ones too; the reason goes into the log")
    s.add_argument("--export", action="store_true", help="run the export command first")
    s.add_argument("--min-weight", type=int, metavar="N", default=None,
                   help='leave out new findings that weigh less than N (0 to 100); default: "check": '
                        '{"min_weight": N} in config.json, else 0')
    s = add("detectors", cmd_detectors, "list the detectors; enable, disable, trust or run one")
    s.add_argument("action", nargs="?", choices=["list", "enable", "disable", "trust", "run"], default="list")
    s.add_argument("id", nargs="?", help="a detector id, such as concepts.data_clump")
    s.add_argument("--json", action="store_true")
    s = add("annotators", cmd_annotators, "who reads and writes the declarations in the code; approve one, "
                                          "read them, or write changes as a patch")
    s.add_argument("action", nargs="?", choices=["list", "trust", "read", "write"], default="list")
    s.add_argument("arg", nargs="?", help="for trust: an annotator id; for write: a JSON file of changes, or -")
    s.add_argument("--apply", action="store_true", help="for write: apply the patch to the working tree")
    s.add_argument("--json", action="store_true")
    s = add("schema", cmd_schema, "database tables, their owners and who reads or writes them")
    s.add_argument("action", nargs="?", choices=["show", "tables"], default="show")
    s.add_argument("--json", action="store_true", help="the schema section as JSON")
    s.add_argument("--refresh", action="store_true", help="read the schema sources again (no export)")
    s.add_argument("--sqlite", metavar="FILE", help="read the tables from this SQLite database (read-only)")
    s.add_argument("--postgres", action="store_true", help="read the tables from PostgreSQL with psql")
    s.add_argument("--url-env", metavar="VAR", help="the environment variable that holds the PostgreSQL URL; "
                                                    "the URL is never stored or printed")
    s.add_argument("--drop-live", action="store_true", help="forget the last --sqlite or --postgres result")
    s = add("glossary", cmd_glossary, "candidate glossary terms per component (tf-idf over the code)")
    s.add_argument("action", choices=["candidates"])
    s.add_argument("--component", help="one component (default: the top terms of every component)")
    s.add_argument("--top", type=int, help="terms per component (default: 30 for one, 15 for all)")
    s.add_argument("--json", action="store_true")
    s = add("hotspots", cmd_hotspots, "components that change often, and pairs that change together (from git)")
    s.add_argument("--since", type=int, default=365, metavar="DAYS", help="days of history (default: 365)")
    s.add_argument("--json", action="store_true", help="the full analysis as JSON")
    s.add_argument("--refresh", action="store_true", help="ignore the cache in .archplan/hotspots.json")
    s = add("blast", cmd_blast, "what each traced entry point reaches, what a change touches, and the security "
                                "tests to add")
    s.add_argument("files", nargs="*", help="changed files: show the entry points they touch")
    s.add_argument("--diff", metavar="REF", help="the files changed since the merge base with REF, such as develop")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s = add("exporters", cmd_exporters, "list the exporters, which match this repository and which are installed")
    s.add_argument("root", nargs="?", help="repository root (default: the project root, or .)")
    s.add_argument("--json", action="store_true")
    s = add("export-check", cmd_export_check, "check an archmodel file against the exporter contract")
    s.add_argument("file")
    s.add_argument("--root", help="repository root, to also catch paths that leave it through links")
    s.add_argument("--json", action="store_true")
    s = add("export-lite", cmd_export_lite, "export an approximate model for Java, Kotlin, Scala, C#, PHP, Ruby, "
                                            "Rust or Swift (regex-based)")
    s.add_argument("root", nargs="?")
    s.add_argument("-o", "--output")
    s.add_argument("--language", action="append", help="only this language (repeatable; default: all found)")
    s.add_argument("--tests", action="store_true", help="include test folders and test files")
    s = add("export-python", cmd_export_python, "export a Python project's as-is model")
    s.add_argument("root", nargs="?")
    s.add_argument("-o", "--output")
    s.add_argument("--package", action="append", help="top-level package to include (default: all found)")
    s.add_argument("--granularity", choices=["auto", "package", "module"], default="auto",
                   help="one component per package or per module (auto: modules when there are fewer than 3 packages)")
    s.add_argument("--tests", action="store_true", help="include test packages and test_*.py files")
    s = add("export-ts", cmd_export_ts, "export a TypeScript or JavaScript project's as-is model (needs node)")
    s.add_argument("-C", default=".", metavar="DIR", help="the project directory, with package.json (default: .)")
    s.add_argument("-o", "--output")
    s.add_argument("--root", help="directory the model's paths are relative to (default: .)")
    s.add_argument("--depth", type=int, help="how deep components nest under src (default: 3)")
    s.add_argument("--tests", action="store_true", help="count test files in dependencies and types")

    add("help", cmd_help, "how to get started")
    a = ap.parse_args(argv)
    (getattr(a, "fn", None) or cmd_help)(a)
