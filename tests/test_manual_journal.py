from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workbench import execute
from workbench import manual_edits
from workbench.server import Workbench
import ManualCommands
from ManualCommands import _edge_signature, _resolve_geometric_selector, _selector_matches


ROOT = Path(__file__).resolve().parents[1]


class ManualBuildBaselineTests(unittest.TestCase):
    def test_box_faces_report_construct_origin_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "box.py"
            script.write_text(
                "from build123d import *\n"
                "with BuildPart() as part:\n"
                "    Box(10, 8, 5)\n"
                "result = part.part\n"
            )

            result = execute.run(script)

        self.assertTrue(result["ok"], result.get("error"))
        origins = [face["ConstructOrigin"] for face in result["faces"]]
        self.assertEqual(len(origins), 6)
        self.assertEqual({origin["face_role"] for origin in origins}, {"bottom", "top", "side"})
        self.assertEqual([origin["face_role"] for origin in origins].count("side"), 4)
        self.assertTrue(all(origin["operation"] == "Box" for origin in origins))
        self.assertEqual({origin["operation_id"] for origin in origins}, {"Box_1"})

    def test_box_planar_faces_keep_origin_after_fillet_and_holes(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "model.py"
            script.write_text(
                "from build123d import *\n"
                "with BuildPart() as plate:\n"
                "    Box(60, 40, 10)\n"
                "    fillet(plate.edges().filter_by(Axis.Z), radius=10)\n"
                "    with Locations(plate.faces().sort_by(Axis.Z)[-1]):\n"
                "        with GridLocations(40, 20, 2, 2):\n"
                "            Hole(3)\n"
                "result = plate.part\n"
            )

            result = execute.run(script)

        self.assertTrue(result["ok"], result.get("error"))
        origins = [
            face["ConstructOrigin"]
            for face in result["faces"]
            if face["ConstructOrigin"]["operation"] == "Box"
        ]
        self.assertEqual({origin["face_role"] for origin in origins}, {"bottom", "top", "side"})
        self.assertEqual(len({origin["operation_id"] for origin in origins}), 1)

    def test_pad_faces_report_construct_origin_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "pad.py"
            script.write_text(
                "from build123d import *\n"
                "with BuildPart() as part:\n"
                "    with BuildSketch(Plane.XY):\n"
                "        Rectangle(10, 8)\n"
                "    extrude(amount=5)\n"
                "result = part.part\n"
            )

            result = execute.run(script)

        self.assertTrue(result["ok"], result.get("error"))
        origins = [face["ConstructOrigin"] for face in result["faces"]]
        self.assertEqual({origin["face_role"] for origin in origins}, {"bottom", "top", "side"})
        self.assertTrue(all(origin["operation"] == "Pad" for origin in origins))
        self.assertEqual({origin["operation_id"] for origin in origins}, {"Pad_1"})

    def test_geometric_edge_selector_survives_dimension_change(self):
        from build123d import Box

        shape = Box(10, 10, 10)
        selected_edge = shape.edges()[0]
        selector = _edge_signature(shape, selected_edge)

        scaled_shape = shape.scale((1.2, 1, 1))
        resolved_edge = _resolve_geometric_selector(scaled_shape, selector)

        self.assertTrue(_selector_matches(selector, _edge_signature(scaled_shape, resolved_edge)))

    def test_geometric_selector_refuses_ambiguous_match(self):
        selector = {"kind": "edge", "type": "line", "center": [0.5, 0.5, 0.5]}
        candidate = {"kind": "edge", "type": "line", "center": [0.5, 0.5, 0.5]}
        with (
            patch.object(manual_edits, "viewer_edges", return_value=[object(), object()]),
            patch.object(manual_edits, "edge_signature", return_value=candidate),
        ):
            with self.assertRaisesRegex(ValueError, "matched 2 edges"):
                _resolve_geometric_selector(object(), selector)

    def test_topological_selector_falls_back_when_origin_is_not_found(self):
        fallback = {"kind": "face", "type": "plane", "center": [0.5, 0.5, 0.5]}
        selection = {
            "kind": "face",
            "selector": {
                "kind": "face",
                "topology": {"operation_id": "Box_1", "face_role": "top"},
                "fallback": fallback,
            },
        }
        with (
            patch.object(manual_edits, "viewer_faces", return_value=[object()]),
            patch.object(manual_edits, "construct_origin", return_value={"operation_id": None}),
        ):
            resolved = manual_edits.resolve_selection(object(), selection, [])

        self.assertEqual(resolved, {"kind": "face", "selector": fallback})

    def test_topological_selector_resolves_unique_construct_origin(self):
        selection = {
            "kind": "face",
            "selector": {
                "kind": "face",
                "topology": {"operation_id": "Box_1", "face_role": "top"},
                "fallback": {"kind": "face"},
            },
        }
        candidates = [object(), object()]
        with (
            patch.object(manual_edits, "viewer_faces", return_value=candidates),
            patch.object(
                manual_edits,
                "construct_origin",
                side_effect=[
                    {"operation_id": "Box_1", "face_role": "bottom"},
                    {"operation_id": "Box_1", "face_role": "top"},
                ],
            ),
            patch.object(manual_edits, "face_signature", return_value={}),
        ):
            resolved = manual_edits.resolve_selection(object(), selection, [])

        self.assertEqual(resolved, {"kind": "face", "topological_face": candidates[1]})

    def test_topological_edge_selector_matches_adjacent_face_origins(self):
        neighbors = [
            {"operation_id": "Box_1", "face_role": "side"},
            {"operation_id": "Pad_1", "face_role": "top"},
        ]
        edge = object()
        selection = {
            "kind": "edge",
            "selector": {
                "kind": "edge",
                "topology": {"adjacent_faces": neighbors},
                "fallback": {"kind": "edge"},
            },
        }
        with (
            patch.object(manual_edits, "edge_adjacencies", return_value=[(edge, neighbors)]),
            patch.object(manual_edits, "edge_signature", return_value={}),
        ):
            resolved = manual_edits.resolve_selection(object(), selection, [])

        self.assertEqual(resolved, {"kind": "edge", "topological_edge": edge})

    def test_multiple_topological_edges_choose_closest_orientation_then_center(self):
        neighbors = [{"operation_id": "Box_1", "face_role": "top"}]
        selection = {
            "kind": "edge",
            "selector": {
                "kind": "edge",
                "topology": {"adjacent_faces": neighbors},
                "fallback": {
                    "kind": "edge",
                    "center": [0.8, 0.5, 0.5],
                    "direction": [1, 0, 0],
                },
            },
        }
        first_edge, second_edge, unrelated_edge = object(), object(), object()
        adjacent = [
            (first_edge, neighbors),
            (second_edge, neighbors),
            (unrelated_edge, [{"operation_id": "Box_2", "face_role": "top"}]),
        ]
        signatures = [
            {"center": [0.8, 0.5, 0.5], "direction": [0, 1, 0]},
            {"center": [0.7, 0.5, 0.5], "direction": [1, 0, 0]},
        ]
        with (
            patch.object(manual_edits, "edge_adjacencies", return_value=adjacent),
            patch.object(manual_edits, "edge_signature", side_effect=signatures),
        ):
            resolved = manual_edits.resolve_selection(object(), selection, [])

        self.assertEqual(resolved, {"kind": "edge", "topological_edge": second_edge})

    def test_selected_topological_edge_does_not_re_resolve_viewer_index(self):
        from build123d import Box

        shape = Box(10, 10, 10)
        expected = shape.edges()[0]
        with patch.object(ManualCommands, "_viewer_edges", side_effect=AssertionError("viewer lookup should be skipped")):
            resolved = ManualCommands._selected_edge(
                shape,
                {"kind": "edge", "topological_edge": expected},
            )

        self.assertTrue(resolved.is_same(expected))

    def test_tied_topological_faces_choose_first_and_warn(self):
        selection = {
            "kind": "face",
            "selector": {
                "kind": "face",
                "topology": {"operation_id": "Box_1", "face_role": "side"},
                "fallback": {"kind": "face", "center": [0.5, 0.5, 0.5], "normal": [1, 0, 0]},
            },
        }
        candidates = [object(), object()]
        with (
            patch.object(manual_edits, "viewer_faces", return_value=candidates),
            patch.object(manual_edits, "construct_origin", return_value={"operation_id": "Box_1", "face_role": "side"}),
            patch.object(manual_edits, "face_signature", return_value={"center": [0.5, 0.5, 0.5], "normal": [1, 0, 0]}),
            patch("builtins.print") as print_warning,
        ):
            resolved = manual_edits.resolve_selection(object(), selection, [])

        self.assertEqual(resolved, {"kind": "face", "topological_face": candidates[0]})
        self.assertIn("WARNING:", print_warning.call_args.args[0])

    def test_ppp0102_builds_to_a_valid_solid(self):
        result = execute.run(ROOT / "examples" / "ttt-ppp0102.py")

        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["summary"]["valid"])
        self.assertGreater(result["summary"]["volume"], 0)

    def test_manual_chamfer_is_replayed_on_the_script_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "box.py"
            script.write_text("from build123d import Box\nresult = Box(10, 10, 10)\n")
            command = {
                "name": "chamfer",
                "selection": {"kind": "edge", "index": 0},
                "distance": 1.0,
            }

            result = execute.run(script, [command])

        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["summary"]["valid"])
        self.assertLess(result["summary"]["volume"], 1000)

    def test_script_runs_before_manual_commands_in_list_order(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "box.py"
            script.write_text("from build123d import Box\nresult = Box(10, 10, 10)\n")
            applied: list[str] = []

            def record_command(shape, command):
                self.assertAlmostEqual(shape.volume, 1000)
                applied.append(command["name"])
                return shape

            commands = [{"name": "first"}, {"name": "second"}]
            with patch.object(execute, "execute_manual_command", side_effect=record_command):
                result = execute.run(script, commands)

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(applied, ["first", "second"])
        self.assertAlmostEqual(result["summary"]["volume"], 1000)


class ManualJournalStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_face_command_uses_topological_selector_with_geometric_fallback(self):
        bounds = {"min": [-50, -50, -50], "size": [100, 100, 100]}
        command = {
            "name": "offset",
            "selection": {
                "kind": "face",
                "type": "plane",
                "area": 400,
                "center": [0, 0, 0],
                "normal": [0, 0, 1],
                "ConstructOrigin": {
                    "operation": "Box",
                    "operation_id": "Box_1",
                    "face_role": "top",
                },
            },
            "distance": 2.0,
            "bounds": bounds,
        }

        operation = Workbench._operation_from_command(command)
        restored = Workbench._command_from_operation(operation)
        selector = operation["target"]["selection"][0]

        self.assertEqual(operation["target"]["strategy"], "topologic_v1")
        self.assertEqual(selector["topology"], {"operation_id": "Box_1", "face_role": "top"})
        self.assertEqual(selector["fallback"]["kind"], "face")
        self.assertEqual(restored["selection"]["selector"], selector)
        self.assertEqual(restored["distance"], 2.0)

    async def test_new_edge_command_stores_adjacent_face_origins(self):
        bounds = {"min": [-5, -5, -5], "size": [10, 10, 10]}
        neighbors = [
            {"operation_id": "Box_1", "face_role": "top"},
            {"operation_id": "Pad_1", "face_role": "side"},
        ]
        operation = Workbench._operation_from_command({
            "name": "fillet",
            "selection": {
                "kind": "edge",
                "type": "line",
                "length": 10,
                "center": [0, 0, 0],
                "start": [-5, 0, 0],
                "end": [5, 0, 0],
                "direction": [1, 0, 0],
                "AdjacentConstructOrigins": neighbors,
            },
            "distance": 1.0,
            "bounds": bounds,
        })

        self.assertEqual(operation["target"]["strategy"], "topologic_v1")
        self.assertEqual(operation["target"]["selection"][0]["topology"]["adjacent_faces"], neighbors)
        self.assertEqual(operation["target"]["selection"][0]["fallback"]["direction"], [1, 0, 0])

    async def test_new_command_uses_normalized_geometric_selector(self):
        bounds = {"min": [-50, -50, -50], "size": [100, 100, 100]}
        command = {
            "name": "fillet",
            "selection": {
                "kind": "edge",
                "index": 27,
                "type": "line",
                "length": 20,
                "center": [0, 0, 0],
                "start": [-10, 0, 0],
                "end": [10, 0, 0],
            },
            "distance": 1.5,
            "bounds": bounds,
        }

        operation = Workbench._operation_from_command(command)
        restored = Workbench._command_from_operation(operation)
        selector = operation["target"]["selection"][0]

        self.assertEqual(operation["target"]["strategy"], "geometric_v1")
        self.assertEqual(selector["kind"], "edge")
        self.assertEqual(selector["type"], "line")
        self.assertEqual(selector["center"], [0.5, 0.5, 0.5])
        self.assertAlmostEqual(selector["length_ratio"], 0.2)
        self.assertNotIn("index", selector)
        self.assertEqual(restored["selection"]["selector"], selector)
        self.assertEqual(restored["distance"], 1.5)

    async def test_migrates_legacy_journal_without_losing_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".workbench"
            state.mkdir()
            script = root / "model.py"
            other_script = root / "other.py"
            script.write_text("# model\n")
            other_script.write_text("# other\n")
            command = {
                "name": "fillet",
                "selection": {"kind": "edge", "index": 3, "type": "circle", "radius": 12.0},
                "distance": 1.5,
            }
            legacy = {
                str(script.resolve()): [command],
                str(other_script.resolve()): [{"name": "offset", "selection": {"kind": "face", "index": 2}, "distance": -2.0}],
            }
            (state / "manual_commands.json").write_text(json.dumps(legacy))

            with patch("workbench.server.STATE", state):
                bench = Workbench(script)
                migrated = json.loads(bench.manual_commands_file.read_text())

            self.assertEqual(migrated["schema_version"], 1)
            scripts = migrated["scripts"]
            operation = scripts[str(script.resolve())]["operations"][0]
            self.assertTrue(operation["id"])
            self.assertEqual(operation["name"], "fillet")
            self.assertEqual(operation["target"]["strategy"], "viewer_index_v1")
            self.assertEqual(operation["target"]["selection"], command["selection"])
            self.assertEqual(operation["parameters"], {"distance": 1.5})
            self.assertEqual(scripts[str(script.resolve())]["script_version"], bench.version())
            self.assertEqual(bench.manual_commands(), [command])
            self.assertEqual(len(scripts[str(other_script.resolve())]["operations"]), 1)

    async def test_commands_are_isolated_by_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_script = root / "first.py"
            second_script = root / "second.py"
            first_script.write_text("# first\n")
            second_script.write_text("# second\n")

            with patch("workbench.server.STATE", root / ".workbench"):
                bench = Workbench(first_script)
                command = {"name": "fillet", "selection": {"kind": "edge", "index": 2}, "distance": 1.0}
                key = str(first_script.resolve())
                bench._manual_commands_by_script[key] = {
                    "script_version": bench.version(),
                    "operations": [bench._operation_from_command(command)],
                }

                self.assertEqual(bench.manual_commands(), [command])
                bench.open(second_script)
                self.assertEqual(bench.manual_commands(), [])
                bench.open(first_script)
                self.assertEqual(bench.manual_commands(), [command])

    async def test_failed_replay_does_not_replace_saved_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "model.py"
            script.write_text("# model\n")

            with patch("workbench.server.STATE", root / ".workbench"):
                bench = Workbench(script)
                old_command = {"name": "fillet", "selection": {"kind": "edge", "index": 2}, "distance": 1.0}
                new_command = {"name": "chamfer", "selection": {"kind": "edge", "index": 4}, "distance": 2.0}
                key = str(script.resolve())
                old_operation = bench._operation_from_command(old_command)
                bench._manual_commands_by_script[key] = {
                    "script_version": bench.version(),
                    "operations": [old_operation],
                }
                bench._persist_manual_journal()
                bench._run_build = AsyncMock(return_value={"ok": False, "error": "replay failed", "output": ""})

                result = await bench.apply_manual_command(new_command, bench.build_id([old_operation]))

                self.assertFalse(result["ok"])
                self.assertEqual(bench.manual_commands(), [old_command])
                saved = json.loads(bench.manual_commands_file.read_text())
                self.assertEqual(saved["schema_version"], 1)
                self.assertEqual(saved["scripts"][key]["operations"], [old_operation])

    async def test_replay_preserves_command_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "model.py"
            script.write_text("# model\n")

            with patch("workbench.server.STATE", root / ".workbench"):
                bench = Workbench(script)
                bench._run_build = AsyncMock(
                    side_effect=[{"ok": True}, {"ok": True}, {"ok": True}]
                )

                initial = await bench.build()
                first = {"name": "offset", "selection": {"kind": "face", "index": 1}, "distance": 2.0}
                second = {"name": "fillet", "selection": {"kind": "edge", "index": 3}, "distance": 1.0}
                after_first = await bench.apply_manual_command(first, initial["build_id"])
                await bench.apply_manual_command(second, after_first["build_id"])

                calls = bench._run_build.await_args_list
                self.assertEqual(calls[0].args[0], [])
                self.assertEqual(calls[1].args[0], [first])
                self.assertEqual(calls[2].args[0], [first, second])

                saved = json.loads(bench.manual_commands_file.read_text())
                operations = saved["scripts"][str(script.resolve())]["operations"]
                self.assertEqual([operation["name"] for operation in operations], ["offset", "fillet"])
                self.assertNotEqual(operations[0]["id"], operations[1]["id"])


if __name__ == "__main__":
    unittest.main()