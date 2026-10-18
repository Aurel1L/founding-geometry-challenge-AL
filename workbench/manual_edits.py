"""Journal and target selection for manual CAD edits."""

from __future__ import annotations

import json
import tempfile
import uuid
from math import sqrt
from pathlib import Path
from typing import Any, Callable

from build123d import Edge, Face, Shape
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from ocp_tessellate.convert import export_three_cad_viewer_js
from ocp_tessellate.ocp_utils import get_edges, get_faces

MANUAL_JOURNAL_SCHEMA_VERSION = 1
LEGACY_SELECTOR = "viewer_index_v1"
GEOMETRIC_SELECTOR = "geometric_v1"
TOPOLOGIC_SELECTOR = "topologic_v1"
GLOBAL_EPSILON = 0.01
SELECTOR_CENTER_TOLERANCE = 0.025
SELECTOR_RELATIVE_TOLERANCE = 0.2
SELECTOR_DIRECTION_DOT_MINIMUM = 0.98


def _shape_frame(shape: Shape) -> tuple[list[float], list[float], float]:
    bounds = shape.bounding_box()
    minimum = [float(bounds.min.X), float(bounds.min.Y), float(bounds.min.Z)]
    size = [float(bounds.size.X), float(bounds.size.Y), float(bounds.size.Z)]
    diagonal = max(sqrt(sum(value * value for value in size)), GLOBAL_EPSILON)
    return minimum, size, diagonal


def _normalized_point(point: Any, minimum: list[float], size: list[float]) -> list[float]:
    values = [float(point.X), float(point.Y), float(point.Z)] if hasattr(point, "X") else [float(value) for value in point]
    return [
        0.5 if abs(axis_size) <= GLOBAL_EPSILON else (value - origin) / axis_size
        for value, origin, axis_size in zip(values, minimum, size)
    ]


def face_signature(shape: Shape, face: Face) -> dict[str, Any]:
    minimum, _, diagonal = _shape_frame(shape)
    signature: dict[str, Any] = {
        "kind": "face",
        "type": face.geom_type.name.lower(),
        "center": _normalized_point(face.center(), minimum, list(shape.bounding_box().size)),
        "area_ratio": face.area / diagonal**2,
    }
    if face.is_planar:
        signature["normal"] = [float(value) for value in face.normal_at()]
    kind = face.geom_type.name.lower()
    if kind == "cylinder" and face.radius is not None:
        signature["radius_ratio"] = face.radius / diagonal
        axis = face.axis_of_rotation
        if axis is not None:
            signature["axis_direction"] = [float(value) for value in axis.direction]
            axis_origin = axis.position
            axis_offset = (face.center() - axis_origin).dot(axis.direction)
            signature["axis_origin"] = _normalized_point(
                axis_origin + axis.direction * axis_offset, minimum, list(shape.bounding_box().size)
            )
    elif kind == "torus" and face.radii is not None:
        signature["major_radius_ratio"] = face.radii[0] / diagonal
        signature["minor_radius_ratio"] = face.radii[1] / diagonal
        axis = face.axis_of_rotation
        if axis is not None:
            axis_origin = axis.position
            axis_offset = (face.center() - axis_origin).dot(axis.direction)
            signature["axis_origin"] = _normalized_point(
                axis_origin + axis.direction * axis_offset, minimum, list(shape.bounding_box().size)
            )
            signature["axis_direction"] = [float(value) for value in axis.direction]
    return signature


def edge_signature(shape: Shape, edge: Edge) -> dict[str, Any]:
    minimum, size, diagonal = _shape_frame(shape)
    signature: dict[str, Any] = {
        "kind": "edge",
        "type": edge.geom_type.name.lower(),
        "center": _normalized_point(edge.center(), minimum, size),
        "length_ratio": edge.length / diagonal,
        "direction": [float(value) for value in edge.tangent_at(0.5)],
    }
    if edge.geom_type.name.lower() == "circle":
        signature["radius_ratio"] = edge.radius / diagonal
    vertices = edge.vertices()
    if len(vertices) >= 2:
        endpoints = [_normalized_point(vertex.center(), minimum, size) for vertex in vertices[:2]]
        signature["endpoints"] = sorted(endpoints)
    return signature


def _vector_close(first: list[float], second: list[float], tolerance: float) -> bool:
    return len(first) == len(second) and sqrt(
        sum((a - b) ** 2 for a, b in zip(first, second))
    ) <= tolerance


def _relative_close(expected: float, actual: float, tolerance: float) -> bool:
    return abs(expected - actual) <= tolerance * max(abs(expected), abs(actual), 1e-9)


def selector_matches(selector: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if selector.get("kind") != candidate.get("kind") or selector.get("type") != candidate.get("type"):
        return False
    if not _vector_close(selector.get("center", []), candidate.get("center", []), SELECTOR_CENTER_TOLERANCE):
        return False

    for key in ("area_ratio", "length_ratio", "radius_ratio", "major_radius_ratio", "minor_radius_ratio"):
        if key in selector and (
            key not in candidate
            or not _relative_close(float(selector[key]), float(candidate[key]), SELECTOR_RELATIVE_TOLERANCE)
        ):
            return False

    for key in ("normal", "direction", "axis_direction"):
        if key in selector:
            direction = candidate.get(key)
            if direction is None or sum(a * b for a, b in zip(selector[key], direction)) < SELECTOR_DIRECTION_DOT_MINIMUM:
                return False

    if "axis_origin" in selector and not _vector_close(
        selector["axis_origin"], candidate.get("axis_origin", []), SELECTOR_CENTER_TOLERANCE
    ):
        return False

    if "endpoints" in selector:
        endpoints = candidate.get("endpoints")
        if endpoints is None or len(endpoints) != len(selector["endpoints"]) or any(
            not _vector_close(expected, actual, SELECTOR_CENTER_TOLERANCE)
            for expected, actual in zip(selector["endpoints"], endpoints)
        ):
            return False
    return True


def viewer_faces(shape: Shape) -> list[Face]:
    export_three_cad_viewer_js(None, shape, names=["result"])
    location = TopLoc_Location()
    return [
        Face(face)
        for face in get_faces(shape.wrapped)
        if BRep_Tool.Triangulation_s(face, location) is not None
    ]


def viewer_edges(shape: Shape) -> list[Edge]:
    export_three_cad_viewer_js(None, shape, names=["result"])
    location = TopLoc_Location()
    edges = []
    for edge, face in get_edges(shape.wrapped, True):
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is not None and BRep_Tool.PolygonOnTriangulation_s(
            edge, triangulation, location
        ) is not None:
            edges.append(Edge(edge))
    return edges


def resolve_geometric_selector(shape: Shape, selector: dict[str, Any]) -> Face | Edge:
    kind = selector.get("kind")
    if kind == "face":
        candidates = [
            face for face in viewer_faces(shape)
            if selector_matches(selector, face_signature(shape, face))
        ]
    elif kind == "edge":
        candidates = [
            edge for edge in viewer_edges(shape)
            if selector_matches(selector, edge_signature(shape, edge))
        ]
    else:
        raise ValueError(f"Unsupported geometric selector kind: {kind}")

    if len(candidates) != 1:
        raise ValueError(
            f"Geometric selector matched {len(candidates)} {kind}s; "
            "select the entity again to resolve it unambiguously"
        )
    return candidates[0]


def construct_origin(face: Face, construct_origins: list[tuple[Face, dict]]) -> dict:
    for source_face, origin in construct_origins:
        if face.is_same(source_face):
            return origin

    if not face.is_planar:
        return {"operation": "Untracked", "operation_id": None, "face_role": "Unknown"}

    normal = face.normal_at()
    center = face.center()
    matches = []
    for source_face, origin in construct_origins:
        if not source_face.is_planar:
            continue
        source_normal = source_face.normal_at()
        if abs(abs(normal.dot(source_normal)) - 1.0) > 1e-3:
            continue
        if abs((center - source_face.center()).dot(source_normal)) <= 1e-3:
            if origin not in matches:
                matches.append(origin)
    return matches[0] if len(matches) == 1 else {
        "operation": "Untracked",
        "operation_id": None,
        "face_role": "Unknown",
    }


def edge_adjacencies(
    shape: Shape, construct_origins: list[tuple[Face, dict]]
) -> list[tuple[Edge, list[dict]]]:
    export_three_cad_viewer_js(None, shape, names=["result"])
    face_map = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, face_map)
    location = TopLoc_Location()
    result = []
    for topods_edge, tessellation_face in get_edges(shape.wrapped, True):
        triangulation = BRep_Tool.Triangulation_s(tessellation_face, location)
        if triangulation is None or BRep_Tool.PolygonOnTriangulation_s(
            topods_edge, triangulation, location
        ) is None:
            continue
        origins = []
        for topods_face in face_map.FindFromKey(topods_edge):
            origin = construct_origin(Face(topods_face), construct_origins)
            operation_id = origin.get("operation_id")
            face_role = origin.get("face_role")
            if isinstance(operation_id, str) and face_role not in (None, "Unknown"):
                neighbor = {"operation_id": operation_id, "face_role": face_role}
                if neighbor not in origins:
                    origins.append(neighbor)
        result.append((
            Edge(topods_edge),
            sorted(origins, key=lambda item: (item["operation_id"], item["face_role"])),
        ))
    return result


def _normalized_server_point(point: list[float], bounds: dict[str, Any]) -> list[float]:
    minimum, size = bounds["min"], bounds["size"]
    return [
        round(0.5 if abs(float(length)) <= 1e-9 else (float(value) - float(origin)) / float(length), 6)
        for value, origin, length in zip(point, minimum, size)
    ]


def geometric_selector(selection: dict, bounds: dict[str, Any]) -> dict:
    kind = selection.get("kind")
    if kind not in ("face", "edge"):
        raise ValueError("Manual selector must target a face or edge")

    size = [float(value) for value in bounds["size"]]
    diagonal = max(sqrt(sum(value * value for value in size)), 1e-9)
    selector: dict[str, Any] = {
        "kind": kind,
        "type": selection["type"],
        "center": _normalized_server_point(selection["center"], bounds),
    }
    if kind == "face":
        if "area" in selection:
            selector["area_ratio"] = round(float(selection["area"]) / diagonal**2, 6)
        for key in ("normal", "axis_direction"):
            if key in selection:
                selector[key] = [round(float(value), 6) for value in selection[key]]
        if "axis_origin" in selection:
            selector["axis_origin"] = _normalized_server_point(selection["axis_origin"], bounds)
        for key in ("radius", "major_radius", "minor_radius"):
            if key in selection:
                selector[f"{key}_ratio"] = round(float(selection[key]) / diagonal, 6)
    else:
        if "length" in selection:
            selector["length_ratio"] = round(float(selection["length"]) / diagonal, 6)
        if "radius" in selection:
            selector["radius_ratio"] = round(float(selection["radius"]) / diagonal, 6)
        if "direction" in selection:
            selector["direction"] = [round(float(value), 6) for value in selection["direction"]]
        if "start" in selection and "end" in selection:
            selector["endpoints"] = sorted([
                _normalized_server_point(selection[key], bounds)
                for key in ("start", "end")
            ])
    return selector


def topologic_selector(selection: dict, bounds: dict[str, Any]) -> dict:
    origin = selection.get("ConstructOrigin", {})
    operation_id = origin.get("operation_id")
    face_role = origin.get("face_role")
    topology = None
    if (
        selection.get("kind") == "face"
        and isinstance(operation_id, str)
        and face_role not in (None, "Unknown")
    ):
        topology = {"operation_id": operation_id, "face_role": face_role}
    elif selection.get("kind") == "edge":
        adjacent_faces = selection.get("AdjacentConstructOrigins", [])
        neighbors = [
            {"operation_id": item["operation_id"], "face_role": item["face_role"]}
            for item in adjacent_faces
            if isinstance(item, dict)
            and isinstance(item.get("operation_id"), str)
            and item.get("face_role") not in (None, "Unknown")
        ]
        if neighbors:
            topology = {"adjacent_faces": neighbors}
    return {
        "kind": selection.get("kind"),
        "topology": topology,
        "fallback": geometric_selector(selection, bounds),
    }


def operation_from_command(command: dict) -> dict:
    if not isinstance(command, dict) or not isinstance(command.get("name"), str):
        raise ValueError("Invalid manual command in journal")
    selection = command.get("selection")
    if not isinstance(selection, (dict, list)):
        raise ValueError("Manual command is missing its selection")
    bounds = command.get("bounds")
    if bounds is None:
        target = {"strategy": LEGACY_SELECTOR, "selection": selection}
    else:
        selections = selection if isinstance(selection, list) else [selection]
        selectors = [topologic_selector(item, bounds) for item in selections]
        has_topology = any(selector["topology"] is not None for selector in selectors)
        target = {
            "strategy": TOPOLOGIC_SELECTOR if has_topology else GEOMETRIC_SELECTOR,
            "selection": selectors if has_topology else [selector["fallback"] for selector in selectors],
            "multiple": isinstance(selection, list),
        }
    return {
        "id": uuid.uuid4().hex,
        "name": command["name"],
        "target": target,
        "parameters": {
            key: value for key, value in command.items()
            if key not in ("name", "selection", "bounds")
        },
    }


def command_from_operation(operation: dict) -> dict:
    target = operation.get("target", {})
    strategy = target.get("strategy")
    if strategy == LEGACY_SELECTOR:
        selection = target["selection"]
    elif strategy in (GEOMETRIC_SELECTOR, TOPOLOGIC_SELECTOR):
        selectors = target["selection"]
        selections = [
            {"kind": selector["kind"], "selector": selector}
            for selector in selectors
        ]
        selection = selections if target.get("multiple", False) else selections[0]
    else:
        raise ValueError(f"Unsupported manual target strategy: {strategy}")
    command = {"name": operation["name"], "selection": selection}
    command.update(operation.get("parameters", {}))
    return command


def _choose_topological_candidate(shape: Shape, matches, kind: str, fallback: dict):
    signature = face_signature if kind == "face" else edge_signature
    orientation_key = next(
        (key for key in ("normal", "direction", "axis_direction") if key in fallback),
        None,
    )
    ranked = []
    for candidate in matches:
        candidate_signature = signature(shape, candidate)
        orientation = candidate_signature.get(orientation_key) if orientation_key else None
        orientation_score = (
            1 - sum(a * b for a, b in zip(fallback[orientation_key], orientation))
            if orientation is not None
            else None
        )
        center = candidate_signature.get("center")
        center_score = (
            sum((a - b) ** 2 for a, b in zip(fallback["center"], center))
            if center is not None and "center" in fallback
            else None
        )
        ranked.append((candidate, orientation_score, center_score))

    if any(score is not None for _, score, _ in ranked):
        best_orientation = min(score for _, score, _ in ranked if score is not None)
        ranked = [
            item for item in ranked
            if item[1] is not None and abs(item[1] - best_orientation) <= 1e-6
        ]
    if len(ranked) > 1 and any(center is not None for _, _, center in ranked):
        best_center = min(center for _, _, center in ranked if center is not None)
        ranked = [
            item for item in ranked
            if item[2] is not None and abs(item[2] - best_center) <= 1e-9
        ]
    if len(ranked) > 1:
        print(
            f"WARNING: {len(ranked)} topological {kind} candidates remain tied; "
            "using the first candidate."
        )
    return ranked[0][0]


def resolve_selection(
    shape: Shape,
    selection: dict,
    construct_origins: list[tuple[Face, dict]],
) -> dict:
    selector = selection.get("selector")
    if not isinstance(selector, dict) or "fallback" not in selector:
        return selection

    topology = selector.get("topology")
    if selection.get("kind") == "face" and isinstance(topology, dict):
        matches = [
            face
            for face in viewer_faces(shape)
            if (origin := construct_origin(face, construct_origins)).get("operation_id")
            == topology.get("operation_id")
            and origin.get("face_role") == topology.get("face_role")
        ]
        if matches:
            return {
                "kind": "face",
                "topological_face": _choose_topological_candidate(shape, matches, "face", selector["fallback"]),
            }
    elif selection.get("kind") == "edge" and isinstance(topology, dict):
        expected = topology.get("adjacent_faces")
        if isinstance(expected, list) and expected:
            expected_neighbors = {
                (neighbor.get("operation_id"), neighbor.get("face_role"))
                for neighbor in expected
                if isinstance(neighbor, dict)
            }
            adjacent_edges = edge_adjacencies(shape, construct_origins)
            matches = [
                edge
                for edge, adjacent_origins in adjacent_edges
                if {
                    (origin["operation_id"], origin["face_role"])
                    for origin in adjacent_origins
                } == expected_neighbors
            ]
            if matches:
                return {
                    "kind": "edge",
                    "topological_edge": _choose_topological_candidate(
                        shape, matches, "edge", selector["fallback"]
                    ),
                }

    return {"kind": selection["kind"], "selector": selector["fallback"]}


def resolve_command(
    shape: Shape,
    command: dict,
    construct_origins: list[tuple[Face, dict]],
) -> dict:
    resolved = command.copy()
    selection = command.get("selection")
    if isinstance(selection, list):
        resolved["selection"] = [
            resolve_selection(shape, item, construct_origins)
            for item in selection
        ]
    elif isinstance(selection, dict):
        resolved["selection"] = resolve_selection(shape, selection, construct_origins)
    return resolved


class ManualEditJournal:
    def __init__(self, path: Path, source_version: Callable[[Path], str | None]):
        self.path = path
        self.source_version = source_version
        self.scripts: dict[str, dict] = {}

    def load(self) -> bool:
        raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        scripts, migrated = self.parse(raw)
        self.scripts = scripts
        if migrated:
            self.persist()
        return migrated

    def parse(self, raw: dict) -> tuple[dict[str, dict], bool]:
        if not isinstance(raw, dict):
            raise ValueError("Manual command journal must be a JSON object")
        if "schema_version" in raw:
            if raw["schema_version"] != MANUAL_JOURNAL_SCHEMA_VERSION:
                raise ValueError(f"Unsupported manual journal schema: {raw['schema_version']}")
            scripts = raw.get("scripts")
            if not isinstance(scripts, dict):
                raise ValueError("Versioned manual journal must contain a scripts object")
            return scripts, False

        migrated = {}
        for script_path, commands in raw.items():
            if not isinstance(commands, list):
                raise ValueError(f"Legacy journal entry for {script_path} must be a list")
            script = Path(script_path)
            migrated[script_path] = {
                "script_version": self.source_version(script),
                "operations": [operation_from_command(command) for command in commands],
            }
        return migrated, bool(raw)

    def persist(self) -> None:
        self._write(self.scripts)

    def _write(self, scripts: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f"{self.path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump({"schema_version": MANUAL_JOURNAL_SCHEMA_VERSION, "scripts": scripts}, stream, indent=2)
            temporary_path.replace(self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def operations(self, script: Path) -> list[dict]:
        return self.scripts.get(str(script.resolve()), {}).get("operations", [])

    def commands(self, script: Path) -> list[dict]:
        return [command_from_operation(operation) for operation in self.operations(script)]

    def set_operations(self, script: Path, script_version: str, operations: list[dict]) -> None:
        scripts = dict(self.scripts)
        scripts[str(script.resolve())] = {
            "script_version": script_version,
            "operations": operations,
        }
        self._write(scripts)
        self.scripts = scripts

    def clear(self, script: Path) -> None:
        scripts = dict(self.scripts)
        scripts.pop(str(script.resolve()), None)
        self._write(scripts)
        self.scripts = scripts
