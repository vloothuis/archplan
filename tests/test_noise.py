"""Rules that keep detectors quiet on deliberate patterns (checked on rela, a large Go code base)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archplan.core.hotspots import TEST, analyse, cleanup  # noqa: E402
from archplan.core.policy import leaks  # noqa: E402
from archplan.detectors.concepts_data_clump import holder, same_types  # noqa: E402
from archplan.detectors.concepts_units_in_names import bare_number  # noqa: E402
from archplan.detectors.fields import is_bag  # noqa: E402
from archplan.detectors.history_coupling import coupling_findings  # noqa: E402
from archplan.detectors.types_synonyms import synonym_findings  # noqa: E402


def fields(*names, t="string"):
    return [{"name": n, "type": t} for n in names]


class NoiseTest(unittest.TestCase):
    def test_vendor_terms_are_not_leaks(self):
        m = {"components": {"web": {}, "jwt": {"kind": "external"}, "auth": {}},
             "policy": {"default": "deny", "rules": []},
             "vocabulary": {"concepts": {"jwt": {"name": "jwt", "owner": "jwt", "kind": "vendor"},
                                         "token": {"name": "token", "owner": "auth", "kind": "domain"}},
                            "usage": [{"component": "web", "concept": "jwt"}, {"component": "web", "concept": "token"}]}}
        self.assertEqual([x["concept"] for x in leaks(m)], ["token"])

    def test_synonyms(self):
        names = ("ID", "EntityID", "Face", "Op", "Actor", "Reason", "Message", "UpdatedAt")
        m = {"components": {"a": {}, "b": {}}, "types": {
            "a.Record": {"name": "Record", "component": "a", "kind": "struct", "fields": fields(*names)},
            "b.Input": {"name": "Input", "component": "b", "kind": "struct", "fields": fields(*names)}}}
        (f,) = synonym_findings(m)
        self.assertEqual(f["evidence"]["shared"], ["Actor", "EntityID", "Face", "Message", "Op", "Reason"])  # not ID
        self.assertIn("deliberate copy", f["detail"])
        self.assertEqual(f["strength"], "weak")
        m["types"]["b.Input"]["fields"] = fields(*reversed(names))
        (f,) = synonym_findings(m)
        self.assertEqual(f["strength"], "strong")
        m["types"]["a.Record"]["fields"] = fields("ID", "EntityID", "Face", "Op", "Actor")  # 4 after ID
        m["types"]["b.Input"]["fields"] = fields("Actor", "Op", "Face", "EntityID", "ID")
        self.assertEqual(synonym_findings(m)[0]["strength"], "medium")

    def test_clump_rules(self):
        types = {"x.Store": {"kind": "interface"}, "x.Clock": {"kind": "interface"},
                 "a.Deps": {"fields": [{"name": "Store", "ref": "x.Store"}, {"name": "Clock", "ref": "x.Clock"},
                                       {"name": "Name", "type": "string"}]},
                 "a.Addr": {"fields": fields("street", "city", "zip")},
                 "b.Addr": {"fields": fields("street", "city") + [{"name": "zip", "type": "int"}]},
                 "c.Pos": {"fields": fields("street", t="int") + fields("city", "zip", t="float")}}
        ctx = SimpleNamespace(types=types)
        self.assertTrue(holder(ctx, types["a.Deps"], 0.3))
        self.assertFalse(holder(ctx, types["a.Addr"], 0.3))
        self.assertEqual(same_types(ctx, {"street", "city", "zip"}, ["a.Addr", "b.Addr"]), 2)
        self.assertEqual(same_types(ctx, {"street", "city", "zip"}, ["a.Addr", "c.Pos"]), 0)

    def test_units_and_bags(self):
        self.assertTrue(bare_number({"type": "int64"}))
        self.assertTrue(bare_number({}))  # unknown type, as in dynamic languages
        self.assertFalse(bare_number({"type": "string"}))
        self.assertFalse(bare_number({"type": "time.Duration"}))
        self.assertFalse(is_bag({"type": "json.RawMessage"}))
        self.assertTrue(is_bag({"type": "jsonb"}))

    def test_coupling(self):
        m = {"components": {"memstore": {}, "pgstore": {}, "store": {}},
             "types": {"store.Store": {"component": "store", "kind": "interface"},
                       "memstore.S": {"component": "memstore", "implements": ["store.Store"]},
                       "pgstore.S": {"component": "pgstore", "implements": ["store.Store"]}}}
        res = {"pairs": [{"a": "memstore", "b": "pgstore", "together": 8, "degree": 0.8}], "since_days": 90}
        (f,) = coupling_findings(res, m)
        self.assertEqual(f["evidence"]["implement"], ["store.Store"])
        self.assertIn("sibling implementations", f["detail"])
        del m["types"]["pgstore.S"]["implements"]
        self.assertGreater(coupling_findings(res, m)[0]["weight"], f["weight"])

    def test_sibling_coupling_needs_strong_degree(self):
        m = {"components": {"memstore": {}, "fsstore": {}, "store": {}},
             "types": {"store.Store": {"component": "store", "kind": "interface"},
                       "memstore.S": {"component": "memstore", "implements": ["store.Store"]},
                       "fsstore.S": {"component": "fsstore", "implements": ["store.Store"]}}}
        res = {"pairs": [{"a": "fsstore", "b": "memstore", "together": 8, "degree": 0.67}], "since_days": 365}
        self.assertEqual(coupling_findings(res, m), [])
        self.assertEqual(len(coupling_findings(res, m, sibling_degree=0.6)), 1)
        del m["types"]["fsstore.S"]["implements"]
        self.assertEqual(len(coupling_findings(res, m)), 1)  # not siblings: the default degree applies

    def test_cleanup_subjects(self):
        for s in ("chore: bump", "chore(lint): enable modernize", "ci: weekly sweep", "ci!: drop node 18",
                  "style(web): format", "build(deps)!: go 1.23", "feat(ci): coverage-ignore directives",
                  "fix(lint): unused var", "refactor(fmt): gofumpt", "feat(format): prettier",
                  "feat(cli, lint): x", "Chore: capitalised"):
            self.assertTrue(cleanup(s), s)
        for s in ("feat(store): compare-and-swap", "fix: off by one", "refactor(cli): split", "chore without colon",
                  'Revert "chore(lint): enable modernize"', "Merge branch 'main'", "feat(cicd): deploy",
                  "Add short random IDs", "", "docs(tickets): ci notes"):
            self.assertFalse(cleanup(s), s)

    def test_cleanup_commits_are_not_coupling(self):
        m = {"components": {"cli": {"path": "cli"}, "migration": {"path": "migration"}}}

        def commit(i, subject, files=("cli/a.go", "migration/b.go")):
            return (f"{i:040x}", "a@x", f"2026-01-{i + 1:02d}T00:00:00Z", list(files), subject)
        feature = [commit(i, f"feat: change {i}") for i in range(3)]
        noise = [commit(10, "chore(lint): enable linter"), commit(11, "feat(ci): coverage-ignore"),
                 commit(12, "style: gofmt"), commit(13, "build!: go 1.23"), commit(14, "ci: sweep")]
        solo = [commit(20 + i, f"fix(cli): solo {i}", ("cli/a.go",)) for i in range(3)]
        res = analyse(feature + noise + solo, m, min_together=1)
        (pair,) = res["pairs"]
        self.assertEqual((pair["together"], pair["degree"]), (3, 1.0))  # migration's 3 feature commits
        self.assertEqual(res["cleanup"], 5)
        self.assertEqual(res["components"]["cli"]["commits"], 11)  # still a change for hotspots
        reverts = [commit(30 + i, f'Revert "chore(lint): x {i}"') for i in range(2)]
        self.assertEqual(analyse(feature + reverts, m, min_together=1)["pairs"][0]["together"], 5)
        old = [c[:4] for c in feature + noise]  # commits without a subject count as before
        self.assertEqual(analyse(old, m, min_together=1)["pairs"][0]["together"], 8)

    def test_test_paths(self):
        for p in ("internal/a/a_test.go", "tests/test_x.py", "pkg/test_util.py", "src/a.spec.ts", "lib/x_spec.rb",
                  "web/__tests__/a.js"):
            self.assertTrue(TEST.search(p), p)
        for p in ("internal/a/a.go", "src/testing/helpers.py", "lib/contest.py"):
            self.assertFalse(TEST.search(p), p)


if __name__ == "__main__":
    unittest.main()
