"""threemf_emit.py — write an ASSEMBLY IR to 3MF (3D Manufacturing Format), via lib3mf.

3MF is a MANUFACTURING format: triangle meshes, units, materials/colours, and — the part that matters
here — a real object hierarchy (a components object places other objects by 4x3 transforms). It has
no parameters and no joints, so it is never the design source; it is what the design is delivered AS:

  * the assembly: one mesh object per distinct part, one components object placing every occurrence
    (optionally at a solved pose — assembly_kin), one build item. Any 3MF viewer/slicer opens it.
  * the design rides along: the assembly IR JSON is embedded as an OPC attachment, so the package
    carries manufacturable geometry AND the mates/closures that explain it (readers that don't know
    it ignore it).

    emit_assembly(asm, "out/bot.3mf", meshes={occ: stl}, materials={occ: "PETG"}, pose={"lifter": 40})
"""
from __future__ import annotations

import json

import numpy as np

import assembly_kin as K

ATTACHMENT_URI = "/Metadata/featuretree.assembly.json"
ATTACHMENT_REL = "https://github.com/punkfab/featuretree/assembly-ir"


def _lookup(d, occ):
    if not d:
        return None
    for k in (occ["id"], occ["label"]):
        if k in d:
            return d[k]
    return None


def _mesh_arrays(occ, meshes, tol):
    """(vertices Nx3 mm, triangles Mx3) for an occurrence: supplied STL, else its part IR."""
    path = _lookup(meshes, occ)
    if path:
        import trimesh
        m = trimesh.load(path, force="mesh")
        return np.asarray(m.vertices, float), np.asarray(m.faces, int), path
    if occ.get("part"):
        import b3d_emit
        solid, _ = b3d_emit.emit(occ["part"])
        v, t = solid.tessellate(tol)
        return np.array([(p.X, p.Y, p.Z) for p in v], float), np.asarray(t, int), json.dumps(occ["part"], sort_keys=True)
    return None, None, None


def _transform(lib3mf, T):
    """4x4 column-vector transform -> 3MF's 4x3 row-vector matrix."""
    tr = lib3mf.Transform()
    for r in range(3):
        for c in range(3):
            tr.Fields[r][c] = float(T[c][r])
    for c in range(3):
        tr.Fields[3][c] = float(T[c][3])
    return tr


def emit_assembly(asm, path, meshes=None, materials=None, pose=None, tol=0.05, embed_ir=True):
    """Write the assembly to `path` (.3mf). `pose`: mate values to solve for (closures honoured);
    None = the authored pose. Returns {"objects": n_meshes, "components": n_placed}."""
    import lib3mf
    w = lib3mf.get_wrapper()
    model = w.CreateModel()
    model.SetUnit(lib3mf.ModelUnit.MilliMeter)
    transforms = K.solve(asm, pose)[1] if pose else K.posed(asm, {})

    mats = model.AddBaseMaterialGroup()
    mat_index = {}

    def material(occ):
        rgb = occ.get("color") or [0.7, 0.7, 0.7]
        name = _lookup(materials, occ) or "default"
        key = (name, tuple(round(c, 3) for c in rgb))
        if key not in mat_index:
            col = w.RGBAToColor(*[int(round(255 * c)) for c in rgb], 255)
            mat_index[key] = mats.AddMaterial(f"{name} {occ['label']}" if name == "default" else name, col)
        return mat_index[key]

    objects, placed = {}, 0
    comps = model.AddComponentsObject()
    comps.SetName(asm["name"])
    for occ in asm["occurrences"]:
        v, t, key = _mesh_arrays(occ, meshes, tol)
        if v is None:
            continue
        if key not in objects:
            mesh = model.AddMeshObject()
            mesh.SetName(occ["label"])
            verts = []
            for x, y, z in v:
                p = lib3mf.Position()
                p.Coordinates[0], p.Coordinates[1], p.Coordinates[2] = x, y, z
                verts.append(p)
            tris = []
            for a, b, c in t:
                tri = lib3mf.Triangle()
                tri.Indices[0], tri.Indices[1], tri.Indices[2] = int(a), int(b), int(c)
                tris.append(tri)
            mesh.SetGeometry(verts, tris)
            mesh.SetObjectLevelProperty(mats.GetResourceID(), material(occ))
            objects[key] = mesh
        comps.AddComponent(objects[key], _transform(lib3mf, transforms[occ["id"]]))
        placed += 1
    model.AddBuildItem(comps, _transform(lib3mf, np.eye(4)))

    if embed_ir:
        att = model.AddAttachment(ATTACHMENT_URI, ATTACHMENT_REL)
        att.ReadFromBuffer(bytearray(json.dumps(asm).encode()))
    model.QueryWriter("3mf").WriteToFile(path)
    return {"objects": len(objects), "components": placed}


def emit_objects(objects, path, metadata=None):
    """Write loose objects as separate BUILD ITEMS — a print plate (slicers treat each build item as
    its own object on the bed). objects: [{"name", "stl" | ("vertices", "triangles"), "transform"
    4x4 (bed placement), "material", "color" (r, g, b 0..1)}]. Units mm."""
    import lib3mf
    w = lib3mf.get_wrapper()
    model = w.CreateModel()
    model.SetUnit(lib3mf.ModelUnit.MilliMeter)
    mats, idx = model.AddBaseMaterialGroup(), {}
    for o in objects:
        if "stl" in o:
            import trimesh
            m = trimesh.load(o["stl"], force="mesh")
            v, t = np.asarray(m.vertices, float), np.asarray(m.faces, int)
        else:
            v, t = np.asarray(o["vertices"], float), np.asarray(o["triangles"], int)
        mesh = model.AddMeshObject()
        mesh.SetName(o["name"])
        verts = []
        for x, y, z in v:
            p = lib3mf.Position()
            p.Coordinates[0], p.Coordinates[1], p.Coordinates[2] = x, y, z
            verts.append(p)
        tris = []
        for a_, b_, c_ in t:
            tri = lib3mf.Triangle()
            tri.Indices[0], tri.Indices[1], tri.Indices[2] = int(a_), int(b_), int(c_)
            tris.append(tri)
        mesh.SetGeometry(verts, tris)
        key = (o.get("material", "default"), tuple(o.get("color") or (0.7, 0.7, 0.7)))
        if key not in idx:
            col = w.RGBAToColor(*[int(round(255 * c)) for c in key[1]], 255)
            idx[key] = mats.AddMaterial(key[0], col)
        mesh.SetObjectLevelProperty(mats.GetResourceID(), idx[key])
        model.AddBuildItem(mesh, _transform(lib3mf, o.get("transform", np.eye(4))))
    if metadata:
        md = model.GetMetaDataGroup()
        for k, val in metadata.items():
            md.AddMetaData("", k, str(val), "xs:string", False)
    model.QueryWriter("3mf").WriteToFile(path)
    return {"objects": len(objects)}


def read_embedded_ir(path):
    """The assembly IR embedded in a 3MF written by emit_assembly (None if absent)."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        name = ATTACHMENT_URI.lstrip("/")
        return json.loads(z.read(name)) if name in z.namelist() else None


def read_assembly(path):
    """Read a 3MF back with lib3mf -> [(object name, world-placed vertices Nx3 mm)] for every
    component of every build item (the round-trip check: what a slicer/viewer will see)."""
    import lib3mf
    w = lib3mf.get_wrapper()
    model = w.CreateModel()
    model.QueryReader("3mf").ReadFromFile(path)

    def mat(tr):
        M = np.eye(4)
        for r in range(3):
            for c in range(3):
                M[c][r] = tr.Fields[r][c]
            M[r][3] = tr.Fields[3][r]
        return M

    def verts(mesh):
        return np.array([tuple(p.Coordinates) for p in mesh.GetVertices()], float)

    out = []
    items = model.GetBuildItems()
    while items.MoveNext():
        bi = items.GetCurrent()
        root, Tb = bi.GetObjectResource(), mat(bi.GetObjectTransform())
        if root.IsComponentsObject():
            co = model.GetComponentsObjectByID(root.GetResourceID())
            for k in range(co.GetComponentCount()):
                c = co.GetComponent(k)
                mesh = model.GetMeshObjectByID(c.GetObjectResourceID())
                T = Tb @ mat(c.GetTransform())
                v = verts(mesh)
                out.append((mesh.GetName(), (T[:3, :3] @ v.T).T + T[:3, 3]))
        else:
            mesh = model.GetMeshObjectByID(root.GetResourceID())
            v = verts(mesh)
            out.append((mesh.GetName(), (Tb[:3, :3] @ v.T).T + Tb[:3, 3]))
    return out
