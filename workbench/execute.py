"""Run one build123d script in a fresh process and describe what it built.

    python -m workbench.execute SCRIPT OUT.json

The script either assigns its final shape to a variable named `result`, or
calls `show()` / `show_object()` as it would with OCP CAD Viewer: the last
object shown is the part. The output holds the mesh for the viewer and, for every face and edge the viewer
can show, a short geometric description, in the same order as the mesh.
"""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import traceback
import types
from pathlib import Path

from build123d import Edge, Face, Shape, Vector
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.TopLoc import TopLoc_Location
from ocp_tessellate.convert import export_three_cad_viewer_js
from ocp_tessellate.ocp_utils import get_edges, get_faces


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
    }
    if kind == "Circle":
        info["radius"] = round(curve.Circle().Radius(), 3)
    return info


def describe(shape: Shape) -> dict:
    """Mesh plus per-face and per-edge descriptions, index-aligned with the mesh."""
    mesh = json.loads(export_three_cad_viewer_js(None, shape, names=["result"]))

    # ocp-tessellate numbers faces and edges through TopExp.MapShapes and skips
    # the ones it could not mesh. Walk the same maps with the same filters so
    # that index i in the viewer is entry i here.
    location = TopLoc_Location()
    faces = [
        describe_face(Face(f))
        for f in get_faces(shape.wrapped)
        if BRep_Tool.Triangulation_s(f, location) is not None
    ]
    edges = []
    for edge, face in get_edges(shape.wrapped, True):
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if BRep_Tool.PolygonOnTriangulation_s(edge, triangulation, location) is None:
            continue
        edges.append(describe_edge(Edge(edge)))

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


def run(script: Path) -> dict:
    output = io.StringIO()
    shown = []
    sys.modules["ocp_vscode"] = _fake_ocp_vscode(shown)
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            namespace = runpy.run_path(str(script), run_name="__main__")
        result = namespace.get("result", shown[-1] if shown else None)
        result = getattr(result, "part", result)  # show(builder) passes a BuildPart
        if not isinstance(result, Shape):
            raise ValueError("the script must assign its final shape to `result`, or call show() on it")
        return {"ok": True, "output": output.getvalue(), **describe(result)}
    except Exception:
        return {"ok": False, "output": output.getvalue(), "error": traceback.format_exc(limit=-3)}


if __name__ == "__main__":
    script, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(script.parent))
    out.write_text(json.dumps(run(script)))
