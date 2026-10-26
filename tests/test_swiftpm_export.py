import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan.core.liteexport import export, parse_swift_imports, swift_module
from archplan import modelcheck


class SwiftProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def model(self, files, **kwargs):
        self.write(files)
        result = export(self.root, **kwargs)
        report = modelcheck.check(result, self.root)
        self.assertTrue(report["ok"], report["errors"])
        return result

    def refs(self, result, path):
        return [ref["to"] for ref in result["files"][path]["refs"]]

    def edges(self, result):
        return {(edge["from"], edge["to"]): edge for edge in result["dependencies"]}


class SwiftExportTest(SwiftProject):
    def test_normalized_import_fields_and_conformance(self):
        result = self.model({
            "Sources/my-core/Buffer.swift": "public struct Buffer {}\npublic protocol Reading {}\n",
            "Sources/App/Main.swift": "import my_core\nstruct Reader: Reading {\n  var buffers: [Buffer]\n}\n",
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/my-core/Buffer.swift"])
        reader = result["types"]["Sources/App.Reader"]
        self.assertEqual(reader["implements"], ["Sources/my-core.Reading"])
        self.assertEqual(reader["fields"], [{"name": "buffers", "ref": "Sources/my-core.Buffer", "many": True}])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Sources/my-core", "line": 1}])
        self.assertEqual(result["dependencies"], [
            {"from": "Sources/App", "to": "Sources/my-core", "weight": 1,
             "evidence": ["Sources/App/Main.swift:1"]}])

    def test_targets_and_test_targets_are_separate(self):
        result = self.model({
            "Sources/Core/Buffer.swift": "public struct Buffer {}\n",
            "Tests/FirstTests/Fixture.swift": "struct Fixture {}\n",
            "Tests/SecondTests/Case.swift": "struct Example {\n  var fixture: Fixture\n  var buffer: Buffer\n}\n",
            "Tests/CoreTests/Case.swift": "@testable import Core\nstruct Example {\n  var buffer: Buffer\n}\n",
        }, tests=True)
        self.assertEqual(self.refs(result, "Tests/SecondTests/Case.swift"), [])
        self.assertEqual(result["types"]["Tests/SecondTests.Example"]["fields"],
                         [{"name": "fixture", "type": "Fixture"}, {"name": "buffer", "type": "Buffer"}])
        self.assertEqual(self.refs(result, "Tests/CoreTests/Case.swift"), ["Sources/Core/Buffer.swift"])
        self.assertEqual(result["files"]["Tests/FirstTests/Fixture.swift"]["module"], "Tests/FirstTests")
        without_tests = export(self.root)
        self.assertFalse(any(path.startswith("Tests/") for path in without_tests["files"]))

    def test_same_module_subfolders_preserve_components_and_ids(self):
        result = self.model({
            "Sources/Core/Models/Buffer.swift": "public struct Buffer {}\n",
            "Sources/Core/IO/Reader.swift": "struct Reader {\n  var buffer: Buffer\n}\n",
        })
        self.assertEqual(set(result["components"]), {"Sources/Core/Models", "Sources/Core/IO"})
        self.assertEqual(result["files"]["Sources/Core/IO/Reader.swift"]["module"], "Sources/Core")
        self.assertEqual(result["files"]["Sources/Core/Models/Buffer.swift"]["module"], "Sources/Core")
        self.assertEqual(result["types"]["Sources/Core/IO.Reader"]["fields"],
                         [{"name": "buffer", "ref": "Sources/Core/Models.Buffer"}])

    def test_attributed_multiline_access_and_symbol_imports(self):
        imports = parse_swift_imports(
            "// import Ignored\n@testable\n@preconcurrency public import\n Core\n"
            "@_implementationOnly import struct Core.Buffer\n"
            "@_exported\nimport protocol Core.Reading\n"
            "@_spi(Testing) internal import Core\n"
            "import func Core.process\n")
        self.assertEqual([(imp.module, imp.line, imp.symbol, imp.reexport) for imp in imports],
                         [("Core", 3, None, False), ("Core", 5, "Buffer", False),
                          ("Core", 7, "Reading", True), ("Core", 8, None, False),
                          ("Core", 9, "process", False)])

    def test_symbol_import_exposes_no_other_types(self):
        result = self.model({
            "Sources/Core/Buffer.swift": "public struct Buffer {}\n",
            "Sources/Core/Reader.swift": "public struct Reader {}\n",
            "Sources/App/Main.swift": "import struct Core.Buffer\nstruct Main {\n"
                                      "  var buffer: Buffer\n  var reader: Reader\n}\n",
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/Core/Buffer.swift"])
        self.assertEqual(result["types"]["Sources/App.Main"]["fields"],
                         [{"name": "buffer", "ref": "Sources/Core.Buffer"}, {"name": "reader", "type": "Reader"}])

    def test_transitive_cyclic_reexports(self):
        result = self.model({
            "Sources/Hardware/Device.swift": "public struct Device {}\n",
            "Sources/Kit/Exports.swift": "@_exported import Hardware\n@_exported import Profile\n",
            "Sources/Profile/Exports.swift": "@_exported\nimport Kit\n",
            "Sources/App/Main.swift": "import Profile\nlet device: Device? = nil\n",
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/Hardware/Device.swift"])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Sources/Profile", "line": 1}])
        self.assertEqual(set(self.edges(result)),
                         {("Sources/App", "Sources/Profile"), ("Sources/App", "Sources/Hardware"),
                          ("Sources/Profile", "Sources/Kit"), ("Sources/Kit", "Sources/Hardware"),
                          ("Sources/Kit", "Sources/Profile")})

    def test_symbol_reexports_keep_restrictions(self):
        result = self.model({
            "Sources/Core/Buffer.swift": "public struct Buffer {}\n",
            "Sources/Core/Reader.swift": "public struct Reader {}\n",
            "Sources/Kit/Exports.swift": "@_exported import struct Core.Buffer\n",
            "Sources/App/Main.swift": "import Kit\nlet buffer: Buffer? = nil\nlet reader: Reader? = nil\n",
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/Core/Buffer.swift"])

    def test_ambiguous_imports_are_reported_not_guessed(self):
        with self.assertWarnsRegex(UserWarning, "ambiguous Swift type 'Buffer'"):
            result = self.model({
                "Sources/First/Buffer.swift": "public struct Buffer {}\n",
                "Sources/Second/Buffer.swift": "public struct Buffer {}\n",
                "Sources/App/Main.swift": "import First\nimport Second\nstruct Main {\n  var buffer: Buffer\n}\n",
            })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        self.assertEqual(result["types"]["Sources/App.Main"]["fields"], [{"name": "buffer", "type": "Buffer"}])
        self.assertEqual(set(self.edges(result)),
                         {("Sources/App", "Sources/First"), ("Sources/App", "Sources/Second")})

    def test_own_module_shadows_imports(self):
        result = self.model({
            "Sources/First/Buffer.swift": "public struct Buffer {}\n",
            "Sources/App/Models/Buffer.swift": "struct Buffer {}\n",
            "Sources/App/Main.swift": "import First\nstruct Main {\n  var buffer: Buffer\n}\n",
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/App/Models/Buffer.swift"])
        self.assertEqual(result["types"]["Sources/App.Main"]["fields"],
                         [{"name": "buffer", "ref": "Sources/App/Models.Buffer"}])

    def test_own_module_ambiguity_does_not_fall_back_to_import(self):
        with self.assertWarnsRegex(UserWarning, "ambiguous Swift type 'Buffer'"):
            result = self.model({
                "Sources/First/Buffer.swift": "public struct Buffer {}\n",
                "Sources/App/First/Buffer.swift": "struct Buffer {}\n",
                "Sources/App/Second/Buffer.swift": "struct Buffer {}\n",
                "Sources/App/Main.swift": "import First\nlet buffer: Buffer? = nil\n",
            })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])

    def test_normalization_collisions_are_ambiguous(self):
        with self.assertWarnsRegex(UserWarning, "ambiguous Swift module 'my_core'"):
            result = self.model({"Sources/my-core/Buffer.swift": "public struct Buffer {}\n",
                                 "Sources/my_core/Reader.swift": "public struct Reader {}\n",
                                 "Sources/App/Main.swift": "import my_core\nlet buffer: Buffer? = nil\n"})
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"], [])

    def test_same_target_name_in_several_packages(self):
        result = self.model({"PkgA/Sources/Core/A.swift": "public struct A {}\n",
                             "PkgB/Sources/Core/B.swift": "public struct B {}\n",
                             "PkgA/Sources/App/Main.swift": "import Core\nlet a: A? = nil\n"})
        self.assertEqual(self.refs(result, "PkgA/Sources/App/Main.swift"), ["PkgA/Sources/Core/A.swift"])
        self.assertEqual(result["files"]["PkgA/Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "PkgA/Sources/Core", "line": 1}])

    def test_leading_digit_target_names(self):
        result = self.model({"Sources/3-core/Buffer.swift": "struct Buffer {}\n",
                             "Sources/App/Main.swift": "import _3_core\nlet buffer: Buffer? = nil\n"})
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), ["Sources/3-core/Buffer.swift"])

    def test_import_only_functions_exact_lines_no_fake_refs(self):
        result = self.model({
            "Sources/Core/Process.swift": "public func process() {}\n",
            "Sources/App/Main.swift": "// application\n@preconcurrency import Core\n",
            "Sources/App/Other.swift": "import Core\nimport Core\n",
        })
        self.assertEqual(result["types"], {})
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        self.assertEqual(result["dependencies"], [
            {"from": "Sources/App", "to": "Sources/Core", "weight": 2,
             "evidence": ["Sources/App/Main.swift:2", "Sources/App/Other.swift:1"]}])

    def test_comments_strings_external_and_unused_import_names(self):
        result = self.model({
            "Sources/Core/Core.swift": "struct Core {}\n",
            "Sources/Unused/Device.swift": "struct Device {}\n",
            "Sources/App/Main.swift": '// import Unused\n/* import Unused */\n'
                                      'let text = "import Unused"\nimport Foundation\nimport Core\n',
        })
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        self.assertEqual(set(self.edges(result)), {("Sources/App", "Sources/Core")})

    def test_subfolder_paths_and_legacy_fallback(self):
        self.write({"Package/Sources/Core/Buffer.swift": "struct Buffer {}\n",
                    "Package/Sources/App/Main.swift": "import Core\nlet buffer: Buffer? = nil\n"})
        result = export(self.root / "Package", rel_to=self.root)
        self.assertEqual(result["files"]["Package/Sources/Core/Buffer.swift"]["module"], "Package/Sources/Core")
        self.assertEqual(result["files"]["Package/Sources/App/Main.swift"]["moduleImports"],
                         [{"to": "Package/Sources/Core", "line": 1}])
        self.assertEqual(swift_module("Legacy/Models/Buffer.swift"), "Legacy")
        self.assertEqual(swift_module("Buffer.swift"), ".")
        self.assertTrue(modelcheck.check(result, self.root)["ok"])

    def test_multiline_raw_strings_and_nested_comments_are_not_imports(self):
        result = self.model({
            "Sources/Core/Buffer.swift": "struct Buffer {}\n",
            "Sources/App/Main.swift": 'let multiline = """\nimport Core\nBuffer\n"""\n'
                                      'let raw = ##"""\nimport Core\n"Buffer"\n"""##\n'
                                      'let quoted = #"import Core"#\n'
                                      '/* outer\n/* inner */\nimport Core\nBuffer\n*/\n',
        })
        self.assertEqual(result["dependencies"], [])
        self.assertEqual(result["files"]["Sources/App/Main.swift"]["moduleImports"], [])
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])

    def test_no_manifest_or_swift_execution(self):
        self.write({"Package.swift": 'fatalError("must not execute")\n',
                    "Sources/Core/Buffer.swift": "struct Buffer {}\n"})
        with patch("archplan.core.liteexport.subprocess.run", wraps=subprocess.run) as run:
            export(self.root)
        self.assertTrue(run.call_args_list)
        self.assertTrue(all(call.args[0][0] == "git" for call in run.call_args_list))

    def test_large_attribute_runs_and_unclosed_raw_delimiters(self):
        start = time.process_time()
        imports = parse_swift_imports("@_exported\n" * 100_000 + "import Core\n")
        self.assertEqual([(imp.module, imp.line, imp.reexport) for imp in imports],
                         [("Core", 100_001, True)])
        self.assertEqual(parse_swift_imports("#" * 1_000_000), [])
        self.assertEqual(parse_swift_imports('"\\' * 500_000), [])
        self.assertLess(time.process_time() - start, 4.0)

    def test_cli_normal_export_and_contract_check(self):
        self.write({"Package.swift": "// swift-tools-version:5.9\n",
                    "Sources/Core/Process.swift": "public func process() {}\n",
                    "Sources/App/Main.swift": "// application\n@preconcurrency import Core\n"})
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")

        def cli(*args):
            completed = subprocess.run([sys.executable, "-m", "archplan", *args], cwd=self.root,
                                       env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            return completed

        cli("init")
        cli("export")
        path = self.root / ".archplan/model.archmodel.json"
        result = json.loads(path.read_text())
        self.assertEqual(result["dependencies"], [
            {"from": "Sources/App", "to": "Sources/Core", "weight": 1,
             "evidence": ["Sources/App/Main.swift:2"]}])
        self.assertEqual(self.refs(result, "Sources/App/Main.swift"), [])
        checked = cli("export-check", str(path), "--json")
        self.assertTrue(json.loads(checked.stdout)["ok"])
        cli("export-lite", "-o", str(self.root / "lite.json"))
        self.assertEqual(json.loads((self.root / "lite.json").read_text())["dependencies"], result["dependencies"])


if __name__ == "__main__":
    unittest.main()
