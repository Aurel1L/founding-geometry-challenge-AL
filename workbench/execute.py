"""Run one build123d script in a fresh process and describe what it built.

    python -m workbench.execute SCRIPT OUT.json

The script either assigns its final shape to a variable named `result`, or
calls `show()` / `show_object()` as it would with OCP CAD Viewer: the last
object shown is the part. The output holds the mesh for the viewer and, for every face and edge the viewer
can show, a short geometric description, in the same order as the mesh.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import runpy
import sys
import traceback
import types
from pathlib import Path

import build123d
from build123d import BuildPart, Edge, Face, Mode, Plane, Shape, Vector
from ManualCommands import execute as execute_manual_command
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.TopLoc import TopLoc_Location
from ocp_tessellate.convert import export_three_cad_viewer_js
from ocp_tessellate.ocp_utils import get_faces
from workbench.manual_edits import (
    construct_origin as _construct_origin,
    edge_adjacencies as _edge_adjacencies,
    resolve_command as _resolve_topological_command,
)


def _round(values, digits=3):
    return [round(float(v), digits) for v in values]


def describe_face(face: Face) -> dict:
    surface = BRepAdaptor_Surface(face.wrapped)
    kind = GeomAbs_SurfaceType(surface.GetType()).name.removeprefix("GeomAbs_")
    info = {
        "type": kind.lower(),
        "area": round(face.area, 3),
        "center": _round(face.center()),
    }
    if kind == "Plane":
        info["normal"] = _round(face.normal_at())
    elif kind in ("Cylinder", "Cone", "Sphere", "Torus"):
        geom = getattr(surface, kind)()
        if kind == "Cylinder":
            axis = geom.Axis()
            origin, direction = Vector(axis.Location().Coord()), Vector(axis.Direction().Coord())
            # The axis origin OCCT stores can lie anywhere along the axis; report
            # the point level with the face centre instead.
            origin += direction * (face.center() - origin).dot(direction)
            info["radius"] = round(geom.Radius(), 3)
            info["axis_origin"] = _round(origin)
            info["axis_direction"] = _round(direction)
        elif kind == "Sphere":
            info["radius"] = round(geom.Radius(), 3)
        elif kind == "Torus":
            info["major_radius"] = round(geom.MajorRadius(), 3)
            info["minor_radius"] = round(geom.MinorRadius(), 3)
            axis = geom.Axis()
            origin, direction = Vector(axis.Location().Coord()), Vector(axis.Direction().Coord())
            origin += direction * (face.center() - origin).dot(direction)
            info["axis_origin"] = _round(origin)
            info["axis_direction"] = _round(direction)
    return info


def describe_edge(edge: Edge) -> dict:
    curve = BRepAdaptor_Curve(edge.wrapped)
    kind = GeomAbs_CurveType(curve.GetType()).name.removeprefix("GeomAbs_")
    info = {
        "type": kind.lower(),
        "length": round(edge.length, 3),
        "center": _round(edge.center()),
        "start": _round(edge.position_at(0)),
        "end": _round(edge.position_at(1)),
        "direction": _round(edge.tangent_at(0.5)),
    }
    if kind == "Circle":
        info["radius"] = round(curve.Circle().Radius(), 3)
    return info


def describe(shape: Shape, construct_origins: list[tuple[Face, dict]] | None = None) -> dict:
    """Mesh plus per-face and per-edge descriptions, index-aligned with the mesh."""
    mesh = json.loads(export_three_cad_viewer_js(None, shape, names=["result"]))

    # ocp-tessellate numbers faces and edges through TopExp.MapShapes and skips
    # the ones it could not mesh. Walk the same maps with the same filters so
    # that index i in the viewer is entry i here.
    location = TopLoc_Location()
    faces = []
    for topods_face in get_faces(shape.wrapped):
        if BRep_Tool.Triangulation_s(topods_face, location) is None:
            continue
        face = Face(topods_face)
        description = describe_face(face)
        origin = _construct_origin(face, construct_origins or [])
        description["ConstructOrigin"] = origin
        faces.append(description)
    edges = []
    for edge, adjacent_origins in _edge_adjacencies(shape, construct_origins or []):
        description = describe_edge(edge)
        description["AdjacentConstructOrigins"] = adjacent_origins
        edges.append(description)

    part = mesh["parts"][0]["shape"]
    if len(part["triangles_per_face"]) != len(faces) or len(part["segments_per_edge"]) != len(edges):
        raise RuntimeError("face/edge numbering out of sync with the mesh")

    bbox = shape.bounding_box()
    summary = {
        "volume": round(shape.volume, 3),
        "bounding_box": {"min": _round(bbox.min), "max": _round(bbox.max), "size": _round(bbox.size)},
        "solids": len(shape.solids()),
        "faces": len(faces),
        "edges": len(edges),
        "valid": shape.is_valid,
    }
    return {"mesh": mesh, "faces": faces, "edges": edges, "summary": summary}


def _fake_ocp_vscode(shown: list) -> types.ModuleType:
    """Stand in for OCP CAD Viewer, so that scripts written for it run unchanged."""
    module = types.ModuleType("ocp_vscode")
    module.show = module.show_object = lambda *objs, **kwargs: shown.extend(objs)
    return module


def _pad_sources(parameters: inspect.BoundArguments, context: BuildPart):
    profile = parameters.arguments.get("to_extrude")
    if profile is None:
        profiles = list(context.pending_faces)
        planes = list(context.pending_face_planes)
    elif isinstance(profile, Face):
        profiles = [profile]
        planes = []
    elif hasattr(profile, "faces"):
        profiles = list(profile.faces())
        planes = []
    else:
        return None

    amount = parameters.arguments.get("amount")
    until = parameters.arguments.get("until")
    taper = parameters.arguments.get("taper", 0.0)
    mode = parameters.arguments.get("mode", Mode.ADD)
    if not profiles or amount is None or until is not None or taper != 0 or mode != Mode.ADD:
        return None

    requested_direction = parameters.arguments.get("dir")
    travel_direction = Vector(requested_direction) if requested_direction is not None else None
    travel_sign = 1 if amount > 0 else -1
    distance = abs(float(amount))
    sources = []
    for index, profile_face in enumerate(profiles):
        if not profile_face.is_planar:
            return None
        if travel_direction is None:
            plane = planes[index] if index < len(planes) else Plane(profile_face)
            direction = plane.z_dir
        else:
            direction = travel_direction
        direction = direction.normalized() * travel_sign
        sources.append((profile_face.center(), direction))
    return sources, distance, bool(parameters.arguments.get("both", False))


def _pad_face_role(face: Face, sources, distance: float, both: bool) -> str:
    if not face.is_planar:
        return "side"
    tolerance = max(1e-5, distance * 1e-4)
    for origin, direction in sources:
        if abs(abs(face.normal_at().dot(direction)) - 1.0) > 1e-3:
            continue
        delta = face.center() - origin
        along = delta.dot(direction)
        perpendicular = (delta - direction * along).length
        if perpendicular > tolerance:
            continue
        if abs(along - distance) <= tolerance:
            return "top"
        if (both and abs(along + distance) <= tolerance) or (
            not both and abs(along) <= tolerance
        ):
            return "bottom"
    return "side"


def _instrument_pad_extrude(
    construct_origins: list[tuple[Face, dict]], operation_counts: dict[str, int]
):
    original_extrude = build123d.extrude
    signature = inspect.signature(original_extrude)

    def tracked_extrude(*args, **kwargs):
        parameters = signature.bind_partial(*args, **kwargs)
        context = BuildPart._get_context("extrude")
        tracking = _pad_sources(parameters, context) if isinstance(context, BuildPart) else None
        result = original_extrude(*args, **kwargs)
        if tracking is None:
            return result

        operation_counts["Pad"] = operation_counts.get("Pad", 0) + 1
        operation_id = f"Pad_{operation_counts['Pad']}"
        sources, distance, both = tracking
        for face in context.lasts.get(Face, []):
            construct_origins.append((
                face,
                {
                    "operation": "Pad",
                    "operation_id": operation_id,
                    "face_role": _pad_face_role(face, sources, distance, both),
                },
            ))
        return result

    return original_extrude, tracked_extrude


def _instrument_box(
    construct_origins: list[tuple[Face, dict]], operation_counts: dict[str, int]
):
    original_init = build123d.Box.__init__
    signature = inspect.signature(original_init)

    def tracked_init(box, *args, **kwargs):
        parameters = signature.bind(box, *args, **kwargs)
        context = BuildPart._get_context(box)
        result = original_init(box, *args, **kwargs)
        if not isinstance(context, BuildPart):
            return result
        if parameters.arguments.get("mode", Mode.ADD) != Mode.ADD:
            return result

        operation_counts["Box"] = operation_counts.get("Box", 0) + 1
        operation_id = f"Box_{operation_counts['Box']}"
        direction = Plane(box.rotation).z_dir
        for face in context.lasts.get(Face, []):
            alignment = face.normal_at().dot(direction)
            role = "top" if alignment > 0.999 else "bottom" if alignment < -0.999 else "side"
            construct_origins.append((
                face,
                {"operation": "Box", "operation_id": operation_id, "face_role": role},
            ))
        return result

    return original_init, tracked_init


def run(script: Path, manual_commands: list[dict] | None = None) -> dict:
    output = io.StringIO()
    shown = []
    construct_origins: list[tuple[Face, dict]] = []
    operation_counts: dict[str, int] = {}
    sys.modules["ocp_vscode"] = _fake_ocp_vscode(shown)
    original_extrude, tracked_extrude = _instrument_pad_extrude(construct_origins, operation_counts)
    original_box_init, tracked_box_init = _instrument_box(construct_origins, operation_counts)
    build123d.extrude = tracked_extrude
    build123d.Box.__init__ = tracked_box_init
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            namespace = runpy.run_path(str(script), run_name="__main__")
            result = namespace.get("result", shown[-1] if shown else None)
            result = getattr(result, "part", result)  # show(builder) passes a BuildPart
            if not isinstance(result, Shape):
                raise ValueError("the script must assign its final shape to `result`, or call show() on it")
            for command in manual_commands or []:
                resolved_command = _resolve_topological_command(result, command, construct_origins)
                result = execute_manual_command(result, resolved_command)
        return {"ok": True, "output": output.getvalue(), **describe(result, construct_origins)}
    except Exception:
        return {"ok": False, "output": output.getvalue(), "error": traceback.format_exc(limit=-3)}
    finally:
        build123d.extrude = original_extrude
        build123d.Box.__init__ = original_box_init


if __name__ == "__main__":
    script, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    manual_commands = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else []
    sys.path.insert(0, str(script.parent))
    out.write_text(json.dumps(run(script, manual_commands)))
