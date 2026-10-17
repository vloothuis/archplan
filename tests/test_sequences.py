"""Record a few representative test workflows as Archplan sequence diagrams."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import detectors, project, schema  # noqa: E402
from archplan.pyexport import export  # noqa: E402


def component(module):
    for prefix in ("archplan.detectors", "archplan.schema", "archplan.core"):
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    if module.startswith("archplan."):
        return "archplan"
    return None


def record_sequence(sid, title, actor, entry, entry_component, action):
    """Capture cross-component Python calls while one real test action runs."""
    messages = [{"id": f"{actor}>{entry_component}:{entry}#1", "from": actor, "to": entry_component,
                 "label": entry, "depth": 0}]
    participants = [actor, entry_component]
    active = {}
    seen = set()

    def profile(frame, event, _arg):
        if event == "return":
            active.pop(frame, None)
            return
        if event != "call":
            return
        target = component(frame.f_globals.get("__name__", ""))
        if target is None:
            return
        caller = frame.f_back
        while caller is not None and caller not in active:
            caller = caller.f_back
        source, parent_depth = active.get(caller, (None, 0))
        depth = parent_depth
        if source is not None and source != target and len(messages) < 80:
            label = f"{frame.f_globals.get('__name__', '')}.{frame.f_code.co_name}()"
            key = (source, target, label)
            pair_count = sum(1 for src, dst, _ in seen if src == source and dst == target)
            if key in seen or pair_count >= 3 or frame.f_code.co_name in {
                    "__init__", "<module>", "model_path", "path", "read_json", "write_json",
                    "read_file", "read_text", "model_base", "prefix_of"}:
                active[frame] = (target, parent_depth + (source != target))
                return
            seen.add(key)
            messages.append({"id": f"{source}>{target}:{frame.f_code.co_name}#{len(messages)}",
                             "from": source, "to": target, "label": label, "depth": parent_depth + 1})
            depth = parent_depth + 1
            if target not in participants:
                participants.append(target)
        active[frame] = (target, depth)

    old = sys.getprofile()
    sys.setprofile(profile)
    try:
        action()
    finally:
        sys.setprofile(old)
    return {"title": title, "entry": entry, "source": "tests", "participants": participants, "messages": messages}


class SequenceTraceTest(unittest.TestCase):
    def test_capture_workflows_and_export_sequences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname = 'sample'\n")
            (root / "sample.py").write_text("def run():\n    return True\n")
            db = root / "schema.sql"
            db.write_text("CREATE TABLE accounts (id integer PRIMARY KEY);\n")
            p = project.init(root)

            traces = {}
            traces["export-and-enrich"] = record_sequence(
                "export-and-enrich", "Export and enrich the model", "Test", "Project.export()", "archplan",
                p.export)

            traces["schema-discovery"] = record_sequence(
                "schema-discovery", "Discover and map database tables", "Test", "schema.build()", "archplan.schema",
                lambda: schema.build(root, export(root)))
            traces["run-findings"] = record_sequence(
                "run-findings", "Run architecture findings", "Test", "detectors.run()", "archplan.detectors",
                lambda: detectors.run(p))

            for seq in traces.values():
                self.assertGreater(len(seq["messages"]), 1)
                self.assertGreater(len(seq["participants"]), 1)

            out = ROOT / ".archplan" / "sequences.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"format": "archplan-sequences/1", "sequences": traces}, indent=2) + "\n")

            model = export(ROOT)
            self.assertEqual(set(model["sequences"]), set(traces))
            self.assertIn("calls", model["source"]["capabilities"])

