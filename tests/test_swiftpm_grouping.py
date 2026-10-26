from copy import deepcopy
import json
import unittest

from test_swiftpm_export import ROOT, SwiftProject
from archplan.core.exclude import Exclude, filter_model
from archplan.core.grouping import apply, file_dependency_evidence
from archplan.core.merge import MergeError, merge, prefixed
from archplan.core.liteexport import export
from archplan import modelcheck


class SwiftGroupingTest(SwiftProject):
    def fixture(self):
        return self.model({
            "Sources/Core/Models/Buffer.swift": "public struct Buffer {}\n",
            "Sources/Core/IO/Process.swift": "public func process() {}\n",
            "Sources/App/Main.swift": "// application\nimport Core\nlet buffer: Buffer? = nil\n",
            "Sources/App/Other.swift": "import Core\n",
            "Tests/AppTests/Case.swift": "@testable import Core\n",
        }, tests=True)

    def test_initial_fanout_deduplicates_refs_and_imports(self):
        result = self.fixture()
        edges = self.edges(result)
        self.assertEqual(edges[("Sources/App", "Sources/Core/Models")]["weight"], 2)
        self.assertEqual(edges[("Sources/App", "Sources/Core/Models")]["evidence"],
                         ["Sources/App/Main.swift:2", "Sources/App/Other.swift:1"])
        self.assertEqual(edges[("Sources/App", "Sources/Core/IO")]["weight"], 2)
        self.assertEqual(self.refs(result, "Sources/App/Other.swift"), [])
        self.assertEqual(len(list(file_dependency_evidence(result["files"]))), 6)

    def test_group_merges_splits_and_is_idempotent(self):
        result = self.fixture()
        original_types = set(result["types"])
        config = {"components": {"core": {"match": ["Sources/Core"]},
                                 "app": {"match": ["Sources/App"]},
                                 "checks": {"match": ["Tests/AppTests"]}}}
        self.assertTrue(apply(result, config, self.root))
        self.assertEqual(set(self.edges(result)), {("app", "core"), ("checks", "core")})
        self.assertEqual(self.edges(result)[("app", "core")]["weight"], 2)
        snapshot = deepcopy(result)
        apply(result, config, self.root)
        self.assertEqual(result, snapshot)
        split = {"components": {"models": {"match": ["Sources/Core/Models"]},
                                "operations": {"match": ["Sources/Core/IO"]}}}
        apply(result, split, self.root)
        self.assertEqual(set(self.edges(result)),
                         {("app", "models"), ("app", "operations"), ("checks", "models"), ("checks", "operations")})
        self.assertEqual(set(result["types"]), original_types)
        self.assertEqual(result["files"]["Sources/Core/Models/Buffer.swift"]["module"], "Sources/Core")
        snapshot = deepcopy(result)
        apply(result, split, self.root)
        self.assertEqual(result, snapshot)
        self.assertTrue(modelcheck.check(result, self.root)["ok"])

    def test_import_only_model_survives_regroup(self):
        result = self.model({"Sources/Core/Process.swift": "public func process() {}\n",
                             "Sources/App/Main.swift": "import Core\n",
                             "Tests/AppTests/Case.swift": "@testable import Core\n"}, tests=True)
        apply(result, {"components": {"library": {"match": ["Sources/Core"]},
                                     "client": {"match": ["Sources/App"]}}}, self.root)
        self.assertEqual(set(self.edges(result)), {("client", "library"), ("Tests/AppTests", "library")})
        self.assertTrue(all(not entry["refs"] for entry in result["files"].values()))

    def test_no_grouping_and_layer_only_preserve_edges(self):
        result = self.fixture()
        original = deepcopy(result)
        self.assertFalse(apply(result, {}, self.root))
        self.assertEqual(result, original)
        apply(result, {"layers": [{"name": "application", "components": ["Sources/App"]}]}, self.root)
        self.assertEqual(result["dependencies"], original["dependencies"])

    def test_self_edges_omitted_when_modules_merge(self):
        result = self.fixture()
        apply(result, {"components": {"everything": {"match": ["Sources", "Tests"]}}}, self.root)
        self.assertEqual(result["dependencies"], [])

    def test_external_and_unaffected_metadata_preserved(self):
        result = self.fixture()
        result["components"]["sdk"] = {"name": "sdk", "kind": "external"}
        external = {"from": "Sources/App", "to": "sdk", "weight": 2, "typeOnly": 1,
                    "evidence": ["Sources/App/Main.swift:4"], "custom": "preserved"}
        result["dependencies"].append(external)
        apply(result, {"components": {"core": {"match": ["Sources/Core"]}}}, self.root)
        self.assertEqual(self.edges(result)[("Sources/App", "sdk")], external)

    def test_partial_and_full_module_exclusion(self):
        result = self.fixture()
        filter_model(result, Exclude(self.root, ["Sources/Core/Models"]), self.root)
        self.assertEqual(set(self.edges(result)), {("Sources/App", "Sources/Core/IO"),
                                                   ("Tests/AppTests", "Sources/Core/IO")})
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Sources/Core", "line": 2}])
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        self.assertTrue(modelcheck.check(result, self.root)["ok"])
        filter_model(result, Exclude(self.root, ["Sources/Core/IO"]), self.root)
        self.assertEqual(result["dependencies"], [])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"], [])
        self.assertTrue(modelcheck.check(result, self.root)["ok"])

    def test_exclusion_recomputes_source_weights_and_preserves_unknown_targets(self):
        result = self.fixture()
        result["files"]["Sources/App/Main.swift"]["moduleImports"].append({"to": "Sources/Missing", "line": 5})
        filter_model(result, Exclude(self.root, ["Sources/App/Other.swift"]), self.root)
        self.assertEqual(self.edges(result)[("Sources/App", "Sources/Core/IO")]["weight"], 1)
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"][-1],
                         {"to": "Sources/Missing", "line": 5})
        self.assertFalse(modelcheck.check(result, self.root)["ok"])

    def test_unrelated_exclusion_preserves_dependency_metadata(self):
        result = self.fixture()
        result["dependencies"][0]["custom"] = "preserved"
        original = deepcopy(result["dependencies"])
        filter_model(result, Exclude(self.root, ["unrelated"]), self.root)
        self.assertEqual(result["dependencies"], original)

    def test_import_provenance_does_not_discard_legacy_edges(self):
        result = self.model({"Sources/Core/Process.swift": "public func process() {}\n",
                             "Sources/App/Main.swift": "import Core\n"})
        result["components"].update(legacy={"name": "legacy"}, support={"name": "support"})
        result["files"]["legacy/main.txt"] = {"component": "legacy"}
        result["files"]["support/helper.txt"] = {"component": "support"}
        result["dependencies"].append({"from": "legacy", "to": "support", "weight": 3,
                                       "evidence": ["legacy/main.txt:4"]})
        apply(result, {"components": {"moved": {"match": ["legacy"]}}}, self.root)
        self.assertEqual(self.edges(result)[("moved", "support")]["weight"], 3)
        self.assertEqual(self.edges(result)[("moved", "support")]["evidence"], ["legacy/main.txt:4"])

    def test_export_exclusions_and_prefix_paths(self):
        self.write({".archplan/config.json": json.dumps({"exclude": ["Sources/Core/Models"]})})
        result = self.fixture()
        self.assertNotIn("Sources/Core/Models/Buffer.swift", result["files"])
        self.assertEqual(set(self.edges(result)), {("Sources/App", "Sources/Core/IO"),
                                                   ("Tests/AppTests", "Sources/Core/IO")})

    def test_component_prefixing_and_part_merge(self):
        result = prefixed(self.fixture(), "swift")
        self.assertEqual(result["files"]["Sources/Core/IO/Process.swift"]["module"], "Sources/Core")
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Sources/Core", "line": 2}])
        other = {"format": "archmodel/1", "components": {"service": {"name": "service"}},
                 "files": {"service/main.py": {"component": "service", "refs": []}}}
        result = merge([({"cmd": "swift", "prefix": None}, result),
                        ({"cmd": "python", "prefix": "python"}, other)])
        apply(result, {"components": {"models": {"match": ["Sources/Core/Models"]}}}, self.root)
        self.assertIn(("swift/Sources/App", "models"), self.edges(result))
        self.assertIn(("swift/Sources/App", "swift/Sources/Core/IO"), self.edges(result))
        self.assertTrue(modelcheck.check(result, self.root)["ok"])

    def test_subfolder_grouping_and_exclusions_keep_logical_paths(self):
        (self.root / ".git").mkdir()
        self.write({"Package/Sources/Core/Models/Buffer.swift": "struct Buffer {}\n",
                    "Package/Sources/Core/IO/Process.swift": "public func process() {}\n",
                    "Package/Sources/App/Main.swift": "import Core\n"})
        root = self.root / "Package"
        result = export(root, rel_to=self.root)
        apply(result, {"components": {"client": {"match": ["Sources/App"]}}}, root)
        self.assertEqual(set(self.edges(result)),
                         {("client", "Package/Sources/Core/IO"), ("client", "Package/Sources/Core/Models")})
        filter_model(result, Exclude(root, ["Sources/Core/Models"]), root)
        self.assertEqual(set(self.edges(result)), {("client", "Package/Sources/Core/IO")})
        self.assertEqual(result["files"]["Package/Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Package/Sources/Core", "line": 1}])

    def test_evidence_limit_and_exclusion_beyond_sample(self):
        fixture = {"Sources/Core/Process.swift": "public func process() {}\n"}
        fixture.update({f"Sources/App/Client{number}.swift": "import Core\n" for number in range(7)})
        result = self.model(fixture)
        edge = self.edges(result)[("Sources/App", "Sources/Core")]
        self.assertEqual(edge["weight"], 7)
        self.assertEqual(edge["evidence"], [f"Sources/App/Client{number}.swift:1" for number in range(5)])
        filter_model(result, Exclude(self.root, ["Sources/App/Client6.swift"]), self.root)
        edge = self.edges(result)[("Sources/App", "Sources/Core")]
        self.assertEqual(edge["weight"], 6)
        self.assertEqual(len(edge["evidence"]), 5)
        apply(result, {"components": {"client": {"match": ["Sources/App"]}}}, self.root)
        edge = self.edges(result)[("client", "Sources/Core")]
        self.assertEqual(edge["weight"], 6)
        self.assertEqual(len(edge["evidence"]), 5)

    def test_shared_file_part_merge_preserves_provenance(self):
        first = {"files": {"Sources/App/Main.swift": {"component": "app", "refs": []}}}
        second = {"files": {"Sources/App/Main.swift": {"module": "Sources/App",
                                                     "moduleImports": [{"to": "Sources/Core", "line": 2}]},
                            "Sources/Core/Process.swift": {"module": "Sources/Core"}}}
        result = merge([({"cmd": "first", "prefix": None}, first),
                        ({"cmd": "second", "prefix": None}, second)])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["module"], "Sources/App")
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Sources/Core", "line": 2}])
        conflict = {"files": {"Sources/App/Main.swift": {"module": "Sources"}}}
        with self.assertRaisesRegex(MergeError, "conflicting logical module"):
            merge([({"cmd": "first", "prefix": None}, result),
                   ({"cmd": "conflict", "prefix": None}, conflict)])

    def test_legacy_non_swift_test_filter_and_no_provenance_fallback(self):
        result = {"components": {"a": {"name": "a"}, "b": {"name": "b"}},
                  "files": {"a/main.py": {"component": "a", "refs": [{"to": "b/core.py", "line": 2}]},
                            "tests/test_a.py": {"component": "a", "refs": [{"to": "b/core.py", "line": 1}]},
                            "b/core.py": {"component": "b", "refs": []}},
                  "dependencies": [{"from": "a", "to": "b", "weight": 1, "evidence": ["a/main.py:2"]}]}
        apply(result, {"components": {"client": {"match": ["a", "tests"]}}}, self.root)
        self.assertEqual(result["dependencies"], [
            {"from": "client", "to": "b", "weight": 1, "evidence": ["a/main.py:2"]}])
        result.pop("files")
        apply(result, {"components": {"renamed": {"match": ["a"]}}}, self.root)
        self.assertEqual(result["dependencies"][0]["to"], "b")


class SwiftMetadataTest(SwiftProject):
    def fixture(self):
        return self.model({"Sources/Core/Process.swift": "public func process() {}\n",
                           "Sources/App/Main.swift": "import Core\n"})

    def test_optional_metadata_validation(self):
        invalid = [
            {"module": None}, {"module": ""}, {"module": 3}, {"module": "../Sources/App"},
            {"module": "/Sources/App"}, {"module": "Sources/Other"}, {"module": "Sources/App/"},
            {"module": "Sources/./App"}, {"moduleImports": None}, {"moduleImports": {}},
            {"moduleImports": [None]}, {"moduleImports": [{}]},
            {"moduleImports": [{"to": "Sources/Core", "line": True}]},
            {"moduleImports": [{"to": "Sources/Core", "line": 0}]},
            {"moduleImports": [{"to": "Sources/Core", "line": -1}]},
            {"moduleImports": [{"to": "Sources/Core", "line": 1.5}]},
            {"moduleImports": [{"to": "../Core", "line": 1}]},
            {"moduleImports": [{"to": ["Sources/Core"], "line": 1}]},
            {"moduleImports": [{"to": "Sources/Missing", "line": 1}]},
        ]
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                result = self.fixture()
                result["files"]["Sources/App/Main.swift"].update(metadata)
                self.assertFalse(modelcheck.check(result, self.root)["ok"])
        result = self.fixture()
        del result["files"]["Sources/App/Main.swift"]["module"]
        self.assertFalse(modelcheck.check(result, self.root)["ok"])

    def test_legacy_models_and_schema_contract(self):
        result = self.fixture()
        for entry in result["files"].values():
            entry.pop("module")
            entry.pop("moduleImports")
        self.assertTrue(modelcheck.check(result, self.root)["ok"])
        schema = json.loads((ROOT / "src/archplan/format/archmodel.schema.json").read_text())
        self.assertEqual(schema["properties"]["format"], {"const": "archmodel/1"})
        fields = schema["properties"]["files"]["additionalProperties"]["properties"]
        self.assertEqual(fields["moduleImports"]["items"]["required"], ["to", "line"])
        self.assertEqual(fields["moduleImports"]["items"]["properties"]["line"]["minimum"], 1)


if __name__ == "__main__":
    unittest.main()
