"""pmi.py — read semantic PMI (the designer's stated intent) from an AP242 STEP file.

AP242 files can carry SEMANTIC product and manufacturing information: dimensions with nominal
values and tolerances, geometric tolerances, and datums, each linked to the B-rep faces it
applies to. That is ground truth for design-intent inference — the drawing the designer
actually made — so featuretree scores its inferred intent against it (see intent.py).

    from pmi import read_pmi
    p = read_pmi("part_ap242.stp")
    p["dimensions"]   # [{"type", "value", "lower", "upper", "holes": [Hole, ...]}, ...]
    p["tolerances"]   # [{"type", "value"}, ...]
    p["datums"]       # sorted datum letters

A Hole is (diameter, point_on_axis, axis_direction) for each DISTINCT cylinder a dimension
references. STEP often splits one cylindrical hole into two half-cylinder faces, so faces are
de-duplicated by (diameter, axis line).

OCP note: `label.FindAttribute(guid, attr)` returns True but does not bind `attr` through the
Python bindings, so attributes are found by iterating the label with TDF_AttributeIterator.
"""
from __future__ import annotations

from collections import namedtuple

Hole = namedtuple("Hole", "diameter point axis")


def _attr(label, type_name):
    from OCP.TDF import TDF_AttributeIterator
    it = TDF_AttributeIterator(label)
    while it.More():
        if it.Value().DynamicType().Name() == type_name:
            return it.Value()
        it.Next()
    return None


def _holes(shape_labels):
    """Distinct cylinders among the faces of the referenced shapes."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_ShapeTool

    seen, out = set(), []
    for j in range(1, shape_labels.Length() + 1):
        ex = TopExp_Explorer(XCAFDoc_ShapeTool.GetShape_s(shape_labels.Value(j)), TopAbs_FACE)
        while ex.More():
            s = BRepAdaptor_Surface(TopoDS.Face_s(ex.Current()))
            if s.GetType() == GeomAbs_Cylinder:
                c = s.Cylinder()
                p, d = c.Location(), c.Axis().Direction()
                ax = (d.X(), d.Y(), d.Z())
                # canonical point on the axis line: the foot of the perpendicular from the origin
                t = p.X() * ax[0] + p.Y() * ax[1] + p.Z() * ax[2]
                foot = (p.X() - t * ax[0], p.Y() - t * ax[1], p.Z() - t * ax[2])
                key = (round(2 * c.Radius(), 3), *(round(v, 2) for v in foot))
                if key not in seen:
                    seen.add(key)
                    out.append(Hole(2 * c.Radius(), foot, ax))
            ex.Next()
    return out


def read_pmi(path):
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DimTolTool, XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    reader = STEPCAFControl_Reader()
    reader.SetGDTMode(True)
    reader.ReadFile(str(path))
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.DimTolTool_s(doc.Main())

    dims = []
    seq = TDF_LabelSequence()
    tool.GetDimensionLabels(seq)
    for i in range(1, seq.Length() + 1):
        a = _attr(seq.Value(i), "XCAFDoc_Dimension")
        if a is None:
            continue
        o = a.GetObject()
        first, second = TDF_LabelSequence(), TDF_LabelSequence()
        XCAFDoc_DimTolTool.GetRefShapeLabel_s(seq.Value(i), first, second)
        try:
            lo, hi = o.GetLowerTolValue(), o.GetUpperTolValue()
        except Exception:
            lo = hi = None
        dims.append({"type": str(o.GetType()).split("DimensionType_")[-1], "value": o.GetValue(),
                     "lower": lo, "upper": hi, "holes": _holes(first)})

    tols = []
    seq = TDF_LabelSequence()
    tool.GetGeomToleranceLabels(seq)
    for i in range(1, seq.Length() + 1):
        a = _attr(seq.Value(i), "XCAFDoc_GeomTolerance")
        if a is not None:
            o = a.GetObject()
            tols.append({"type": str(o.GetType()).split("GeomToleranceType_")[-1], "value": o.GetValue()})

    datums = set()
    seq = TDF_LabelSequence()
    tool.GetDatumLabels(seq)
    for i in range(1, seq.Length() + 1):
        a = _attr(seq.Value(i), "XCAFDoc_Datum")
        if a is not None:
            datums.add(a.GetObject().GetName().ToCString())

    return {"dimensions": dims, "tolerances": tols, "datums": sorted(datums)}


def hole_callouts(pmi):
    """The designer's hole groups: one entry per diameter callout that references holes.
    A 'diameter' whose value is far from its holes' diameter (e.g. an 8-inch bolt circle
    referencing two 8 mm holes) is a pattern diameter, not a hole size — flagged as such."""
    out = []
    for d in pmi["dimensions"]:
        if "Diameter" not in d["type"] or not d["holes"]:
            continue
        hole_d = sorted({round(h.diameter, 3) for h in d["holes"]})
        bolt_circle = all(abs(d["value"] - hd) > 0.5 for hd in hole_d)
        out.append({"nominal": d["value"], "hole_diameters": hole_d, "count": len(d["holes"]),
                    "holes": d["holes"], "bolt_circle": bolt_circle})
    return out
