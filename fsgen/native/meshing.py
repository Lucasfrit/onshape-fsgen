"""Triangle meshes of solids (in millimeters) for the print check, the fit check report and the 3D viewer."""
from __future__ import annotations

import base64
import struct

import numpy as np
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.gp import gp_Trsf
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape

from ..fslite import geom as G


def to_mm(shape: TopoDS_Shape) -> TopoDS_Shape:
    """The local builder works in meters; fit checks and meshes use millimeters."""
    t = gp_Trsf()
    t.SetScaleFactor(1000.0)
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def face_triangles(shape_mm: TopoDS_Shape, deflection=0.02, angle=0.15):
    """Yield (face, triangles[n,3,3]) for every face, triangles oriented outward (mm)."""
    BRepMesh_IncrementalMesh(shape_mm, deflection, False, angle, True)
    for f in G.subshapes(shape_mm, G.TopAbs_FACE):
        loc = TopLoc_Location()
        tri = G.BRep_Tool.Triangulation_s(G.TopoDS.Face(f), loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        pts = np.array([[p.X(), p.Y(), p.Z()] for p in
                        (tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1))])
        idx = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)]) - 1
        if G.TopoDS.Face(f).Orientation() == 1:  # reversed face: flip winding so normals point out
            idx = idx[:, [0, 2, 1]]
        yield f, pts[idx]


def triangles(shape_mm: TopoDS_Shape, deflection=0.02, angle=0.15) -> np.ndarray:
    parts = [t for _, t in face_triangles(shape_mm, deflection, angle)]
    return np.concatenate(parts) if parts else np.zeros((0, 3, 3))


def stl_base64(tris: np.ndarray) -> str:
    """Binary STL, base64 (the viewer embeds meshes as text)."""
    tris = np.asarray(tris, dtype=np.float32)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = n, tris
    return base64.b64encode(b"\0" * 80 + struct.pack("<I", len(tris)) + rec.tobytes()).decode()
