"""Operations applied to the built shape without changing its source script."""

from __future__ import annotations

from math import isfinite
from typing import Any

from build123d import Edge, Face, Shape, Solid
import OCP.BRepAlgoAPI as BRepAlgoAPI
from OCP.BRepTools import BRepTools_ReShape
from workbench.manual_edits import (
    GLOBAL_EPSILON as GlobalEpsilon,
    edge_signature as _edge_signature,
    face_signature as _face_signature,
    resolve_geometric_selector as _resolve_geometric_selector,
    selector_matches as _selector_matches,
    viewer_edges as _viewer_edges,
    viewer_faces as _viewer_faces,
)

def _selected_face(shape: Shape, selection: dict[str, Any]) -> Face:
    topological_face = selection.get("topological_face")
    if selection.get("kind") == "face" and isinstance(topological_face, Face):
        return topological_face
    if "selector" in selection:
        resolved = _resolve_geometric_selector(shape, selection["selector"])
        if not isinstance(resolved, Face):
            raise ValueError("Geometric selector did not resolve to a face")
        return resolved
    if selection.get("kind") != "face":
        raise ValueError("This command requires a selected face")

    index = selection.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("The selected face has no valid index")

    faces = _viewer_faces(shape)
    if index < 0 or index >= len(faces):
        raise ValueError(f"Selected face index {index} is out of range {0} to {len(faces) - 1}")
    return faces[index]


def _selected_faces(shape: Shape, selections: list[dict[str, Any]]) -> list[Face]:
    if not selections:
        raise ValueError("Select at least one face")
    if any(selection.get("kind") != "face" for selection in selections):
        raise ValueError("This command requires only selected faces")

    selected = []
    for selection in selections:
        if "selector" in selection or "topological_face" in selection:
            face = _selected_face(shape, selection)
            if not any(face.is_same(existing) for existing in selected):
                selected.append(face)
            continue

        faces = _viewer_faces(shape)
        index = selection.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("A selected face has no valid index")
        if index < 0 or index >= len(faces):
            raise ValueError(f"Selected face index {index} is out of range 0 to {len(faces) - 1}")
        face = faces[index]
        if not any(face.is_same(existing) for existing in selected):
            selected.append(face)
    return selected


def _faces_are_connected(faces: list[Face]) -> bool:
    if len(faces) < 2:
        return True

    connected = [faces[0]]
    remaining = faces[1:]
    while remaining:
        neighbor = next(
            (
                candidate
                for candidate in remaining
                if any(
                    edge.is_same(candidate_edge)
                    for face in connected
                    for edge in face.edges()
                    for candidate_edge in candidate.edges()
                )
            ),
            None,
        )
        if neighbor is None:
            return False
        connected.append(neighbor)
        remaining.remove(neighbor)
    return True


def _blend_radius(face: Face) -> float | None:
    kind = face.geom_type.name.lower()
    if kind == "cylinder":
        return face.radius
    if kind == "torus" and face.radii is not None:
        return face.radii[1]
    return None


def propagate_face(shape: Shape, selection: dict[str, Any]) -> list[Face]:
    """Expand a selected fillet face to connected blend faces of matching radius."""
    seed = _selected_face(shape, selection)
    seed_radius = _blend_radius(seed)
    if seed_radius is None:
        raise ValueError("Face propagation requires a cylindrical or toroidal fillet face")

    candidates = [
        face
        for face in _viewer_faces(shape)
        if (radius := _blend_radius(face)) is not None
        and abs(radius - seed_radius) <= GlobalEpsilon
    ]
    propagated = [seed]
    remaining = [face for face in candidates if not face.is_same(seed)]

    changed = True
    while changed:
        changed = False
        for candidate in remaining[:]:
            if any(_faces_are_connected([face, candidate]) for face in propagated):
                propagated.append(candidate)
                remaining.remove(candidate)
                changed = True

    return propagated


def _selected_edge(shape: Shape, selection: dict[str, Any]) -> Edge:
    topological_edge = selection.get("topological_edge")
    if selection.get("kind") == "edge" and isinstance(topological_edge, Edge):
        return topological_edge
    if "selector" in selection:
        resolved = _resolve_geometric_selector(shape, selection["selector"])
        if not isinstance(resolved, Edge):
            raise ValueError("Geometric selector did not resolve to an edge")
        return resolved
    if selection.get("kind") != "edge":
        raise ValueError("This command requires a selected edge")

    index = selection.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("The selected edge has no valid index")

    edges = _viewer_edges(shape)

    if index < 0 or index >= len(edges):
        raise ValueError(f"Selected edge index {index} is out of range 0 to {len(edges) - 1}")
    return edges[index]


def _edge_feature(shape: Shape, selection: dict[str, Any], distance: Any, name: str) -> Shape:
    edge = _selected_edge(shape, selection)
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        raise ValueError(f"{name.title()} distance must be a positive number in mm")
    if not isfinite(distance) or distance <= GlobalEpsilon:
        raise ValueError(f"{name.title()} distance must be greater than {GlobalEpsilon} mm")

    operation = getattr(shape, name, None)
    if not callable(operation):
        raise ValueError(f"{name.title()} requires a 3D solid or Part")
    result = operation(distance, None, [edge]) if name == "chamfer" else operation(distance, [edge])
    if not result.is_valid:
        raise ValueError(f"{name.title()} did not produce a valid solid")
    return result


def remove(shape: Shape, selection: dict[str, Any]) -> Shape:
    """Remove a selected face from a shape."""
    face = _selected_face(shape, selection)

    reshaper = BRepTools_ReShape()
    reshaper.Remove(face.wrapped)
    result = reshaper.Apply(shape.wrapped)
    if result.IsNull():
        raise ValueError("Removing this face would leave an empty shape")

    return shape.__class__.cast(result)


def _remove_feature_faces(shape: Shape, faces: list[Face], feature: str) -> Shape:
    if not _faces_are_connected(faces):
        raise ValueError(f"Selected {feature} faces must be connected")

    defeaturing_type = getattr(BRepAlgoAPI, "BRepAlgoAPI_Defeaturing", None)
    if defeaturing_type is None:
        raise RuntimeError("This OCP build does not provide BRepAlgoAPI_Defeaturing")

    operation = defeaturing_type()
    operation.SetShape(shape.wrapped)
    for face in faces:
        operation.AddFaceToRemove(face.wrapped)
    operation.Build()
    if not operation.IsDone():
        raise ValueError(f"OpenCascade could not remove this {feature} face")

    result_shape = operation.Shape()
    if result_shape.IsNull():
        raise ValueError("OpenCascade returned an empty shape")

    result = shape.__class__.cast(result_shape)
    if not result.is_valid or len(result.solids()) != len(shape.solids()):
        raise ValueError(f"Removing this {feature} did not produce a valid solid")
    return result


def remove_fillet(shape: Shape, selections: list[dict[str, Any]]) -> Shape:
    """Remove connected cylindrical or toroidal fillet faces and heal the solid."""
    if not selections:
        raise ValueError("Select at least one fillet face")
    faces = (
        propagate_face(shape, selections[0])
        if len(selections) == 1
        else _selected_faces(shape, selections)
    )
    if any(face.geom_type.name.lower() not in ("cylinder", "torus") for face in faces):
        raise ValueError("Remove Fillet requires cylindrical or toroidal faces")
    return _remove_feature_faces(shape, faces, "fillet")


def remove_chamfer(shape: Shape, selections: list[dict[str, Any]]) -> Shape:
    """Remove connected planar chamfer faces and heal the solid."""
    faces = _selected_faces(shape, selections)
    if any(not face.is_planar for face in faces):
        raise ValueError("Remove Chamfer requires planar faces")
    return _remove_feature_faces(shape, faces, "chamfer")


def offset(shape: Shape, selection: dict[str, Any], distance: Any) -> Shape:
    """Offset a planar face outward or inward by a signed distance."""
    face = _selected_face(shape, selection)
    if not face.is_planar:
        raise ValueError("Offset currently supports planar faces only")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        raise ValueError("Offset distance must be a finite number in mm")
    if not isfinite(distance) or abs(distance) <= GlobalEpsilon:
        raise ValueError(f"Offset magnitude must be greater than {GlobalEpsilon} mm")
    if not shape.solids():
        raise ValueError("Offset requires a solid")

    extrusion = Solid.extrude(face, face.normal_at() * distance)
    result = shape.fuse(extrusion) if distance > 0 else shape.cut(extrusion)
    volume_delta = result.volume - shape.volume
    if (
        not result.is_valid
        or len(result.solids()) != len(shape.solids())
        or (distance > 0 and volume_delta <= GlobalEpsilon**3)
        or (distance < 0 and volume_delta >= -(GlobalEpsilon**3))
    ):
        raise ValueError("The face offset did not produce a valid solid")
    return result


def execute(shape: Shape, command: dict[str, Any]) -> Shape:
    """Dispatch a manual command against the current shape."""
    name = command.get("name")
    selection = command.get("selection")
    if name in ("remove_fillet", "remove_chamfer"):
        if isinstance(selection, dict):
            selections = [selection]
        elif isinstance(selection, list):
            selections = selection
        else:
            raise ValueError("Feature removal requires selected faces")
        return remove_fillet(shape, selections) if name == "remove_fillet" else remove_chamfer(shape, selections)
    if not isinstance(selection, dict):
        raise ValueError("A manual command requires a selection")
    if name == "remove":
        return remove(shape, selection)
    if name == "offset":
        return offset(shape, selection, command.get("distance"))
    if name in ("chamfer", "fillet"):
        return _edge_feature(shape, selection, command.get("distance"), name)
    raise ValueError(f"Unknown manual command: {name}")