"""Give a material slot repeating UVs for one shared tiling material, by one of three methods.
Tree: tubes (trunks, branches) are seamed lengthwise on the side facing the trunk; other pieces
are cut around their underside. Standard Box: faces are grouped by the world axis they face.
Standard Triplanar: the texture is projected from three axes and blended (Box UVs are laid out
too, for baking on export). Every island gets the same texel density and is never stretched."""

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import io

TUBE_GAP_DEGREES = 120  # a tube's side normals leave no wider gap around its axis
SIDE_NORMAL = 0.5  # normals more along the axis than this belong to caps and ends
END_BAND = 0.05  # share of a span's length that counts as its end, where the seam starts
MIN_INWARD = 0.25  # share of the way to the trunk's axis that must lie across a branch
SIDE_PULL = 6.0  # extra cost per meter of seam on the far side, keeping it on the chosen side
_MAX_SLABS = 64
MASK_ROLES = ("Metallic", "Roughness", "AO")
UNDERSIDE = 0.5  # faces whose normal points this far down form the underside island
MAX_DISTORTION = 1.5  # a flat island whose edge scales spread wider (p90/p10) is cut per box side
MIN_ELONGATION = 1.5  # a tube is at least this many diameters long; a rock is not
END_RIM = 0.2  # a tube's open rims lie within this share of its length from either end
TREE, BOX, TRIPLANAR = "TREE", "BOX", "TRIPLANAR"
METHODS = (TREE, BOX, TRIPLANAR)
TRIPLANAR_SUFFIX = "_Triplanar"
TRIPLANAR_BLEND = 0.2  # Image Texture box blend: width of the soft edge between projections
PACK_MARGIN = 0.005  # gap between islands of the 0-1 layout a triplanar material bakes onto
MATCH, MANUAL = "MATCH", "MANUAL"  # tile size measured from the original UVs, or typed in
MIN_MEASURED = 0.5  # share of faces whose original UVs must be usable to be measured
BOX_DIRECTIONS = (
    Vector((1, 0, 0)),
    Vector((-1, 0, 0)),
    Vector((0, 1, 0)),
    Vector((0, -1, 0)),
    Vector((0, 0, 1)),
)
BOX_AXES = (*BOX_DIRECTIONS, Vector((0, 0, -1)))


@dataclass
class Span:
    faces: list[int]
    seam_edges: list[int] = field(default_factory=list)
    tube: bool = False
    axis: Vector | None = None  # Standard Box: the world direction the island faces


def missing_maps(material: bpy.types.Material) -> list[str]:
    """Normal and mask maps `material` does not have."""
    missing = []
    if io.role_node(material, "Normal") is None:
        missing.append(f"{material.name}: no normal map")
    if not any(io.role_node(material, role) for role in MASK_ROLES):
        missing.append(f"{material.name}: no mask map")
    return missing


def assign_tiling(
    obj: bpy.types.Object,
    slot: int,
    material: bpy.types.Material,
    faces: list[int] | None = None,
) -> list[str]:
    """Put `material` on `slot` (or only on `faces`) and leave UVs alone. Returns maps the
    material lacks."""
    _require_mesh(obj)
    _require_slot(obj, slot)
    if material is None:
        raise ValueError("Choose a tiling material")
    if faces is None:
        obj.material_slots[slot].material = material
    else:
        io.assign_material(obj, faces, material)
    return missing_maps(material)


def retile(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    slot: int,
    material: bpy.types.Material,
    tile_size: float | None = None,
    method: str = TREE,
    faces: list[int] | None = None,
) -> tuple[int, list[str], float]:
    """Rebuild repeating UVs on the slot's faces (or only `faces` of it) by `method` and assign
    `material` to them (Standard Triplanar: a copy of it projecting from three axes). Without
    `tile_size`, the texture repeats as often as on the original UVs.

    Returns the number of tube strips, warnings (missing maps) and the tile size used.
    """
    _require_mesh(obj)
    _require_slot(obj, slot)
    if material is None:
        raise ValueError("Choose a tiling material")
    if tile_size is not None and tile_size <= 0:
        raise ValueError("Tile size must be positive")
    if method not in METHODS:
        raise ValueError(f"unknown unwrap method {method}")
    if obj.data.users > 1:
        raise ValueError(f"{obj.name}: mesh data is shared; make it single-user first")
    slot_faces = [poly.index for poly in obj.data.polygons if poly.material_index == slot]
    if not slot_faces:
        raise ValueError(f"{obj.name}: the slot has no faces")
    scope = set(slot_faces if faces is None else faces)
    if not scope:
        raise ValueError(f"{obj.name}: no faces to retile")
    aspect = _tile_aspect(material)
    context.view_layer.update()  # matrix_world is stale right after an import
    if tile_size is None:
        tile_size = original_tile_size(obj, sorted(scope), obj.material_slots[slot].material)

    spans = _mark_seams(obj, scope, method)
    _in_edit_mode(context, obj, scope, _unwrap)
    if method == TREE and _split_distorted(obj, spans):
        _in_edit_mode(context, obj, scope, _unwrap)
    _scale_strips(obj, spans, tile_size, aspect)
    if method == TRIPLANAR:
        _in_edit_mode(context, obj, scope, _pack)
        material = _triplanar_copy(material, obj, tile_size, aspect)
    if scope == set(slot_faces):
        obj.material_slots[slot].material = material
    else:
        io.assign_material(obj, sorted(scope), material)
    return sum(span.tube for span in spans), missing_maps(material), tile_size


def original_tile_size(
    obj: bpy.types.Object, faces: list[int], material: bpy.types.Material | None
) -> float:
    """World size of one texture repeat on the faces' current UVs: the median over the faces,
    corrected for the proportions of the texture those UVs were made for. A face without UV area,
    or spanning exactly the whole 0-1 square as a newly created UV map does, can't be measured."""
    mesh, matrix = obj.data, obj.matrix_world
    layer = mesh.uv_layers.active
    node = io.role_node(material, "BaseColor")
    aspect = _tile_aspect(material) if node is not None and all(node.image.size) else 1.0
    densities = []
    for index in faces:
        poly = mesh.polygons[index]
        points = [matrix @ mesh.vertices[vertex].co for vertex in poly.vertices]
        world = sum(
            (
                (b - points[0]).cross(c - points[0]).length / 2
                for b, c in zip(points[1:], points[2:])
            ),
            0.0,
        )
        uvs = [Vector(layer.data[loop].uv) for loop in poly.loop_indices] if layer else []
        area = abs(
            sum(a.x * b.y - b.x * a.y for a, b in zip(uvs, uvs[1:] + uvs[:1], strict=True)) / 2
        )
        if world > 1e-12 and area > 1e-12 and not _fills_unit_square(uvs):
            densities.append(math.sqrt(area * aspect / world))
    if not faces or len(densities) < MIN_MEASURED * len(faces):
        raise ValueError(
            f"{obj.name}: its original UVs can't be measured; choose Manual and set a Tile Size"
        )
    densities.sort()
    return 1 / densities[len(densities) // 2]


def _fills_unit_square(uvs: list[Vector]) -> bool:
    xs, ys = [uv.x for uv in uvs], [uv.y for uv in uvs]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    return all(abs(value - edge) < 1e-6 for value, edge in zip(bounds, (0, 0, 1, 1), strict=True))


def _tile_aspect(material: bpy.types.Material) -> float:
    """Height over width of the tiling texture, so a tall bark strip keeps its proportions."""
    node = io.role_node(material, "BaseColor")
    if node is None or not all(node.image.size):
        raise ValueError(f"{material.name}: no base-color image to size the tile")
    width, height = node.image.size
    return height / width


def _require_mesh(obj: bpy.types.Object) -> None:
    if obj.type != "MESH":
        raise ValueError(f"{obj.name}: not a mesh")


def _require_slot(obj: bpy.types.Object, slot: int) -> None:
    if slot < 0 or slot >= len(obj.material_slots):
        raise ValueError(f"{obj.name}: choose a bark slot")


def _mark_seams(obj: bpy.types.Object, scope: set[int], method: str) -> list[Span]:
    mesh = obj.data
    if mesh.uv_layers.active is None:
        mesh.uv_layers.new(name="UVMap")
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.normal_update()
    chosen = [bm.faces[index] for index in sorted(scope)]
    _mark_borders(chosen)
    if method == TREE:
        spans = _spans(chosen, obj.matrix_world)
    else:
        spans = _box_spans(chosen, obj.matrix_world)
    bm.faces.index_update()
    bm.edges.index_update()
    face_span: dict[int, int] = {}
    trunk = _trunk_axis(spans, obj.matrix_world)
    for index, span in enumerate(spans):
        for face in span.faces:
            face_span[face.index] = index
        if span.tube:
            _mark_lengthwise(span, obj.matrix_world, trunk)
    for edge in bm.edges:
        ids = {face_span[face.index] for face in edge.link_faces if face.index in face_span}
        if len(ids) > 1:
            edge.seam = True
    stored = [
        Span(
            faces=list(dict.fromkeys(face.index for face in span.faces)),
            seam_edges=list(span.seam_edges),
            tube=span.tube,
            axis=span.axis,
        )
        for span in spans
    ]
    bm.to_mesh(mesh)
    bm.free()
    return stored


def _mark_borders(bark: list[bmesh.types.BMFace]) -> None:
    bark_set = set(bark)
    seen: set[bmesh.types.BMEdge] = set()
    for face in bark:
        for edge in face.edges:
            if edge in seen:
                continue
            seen.add(edge)
            if sum(linked in bark_set for linked in edge.link_faces) == 1:
                edge.seam = True


def _spans(faces: list[bmesh.types.BMFace], matrix: Matrix) -> list[Span]:
    """One span per tube and per flat island. A shell that is not one tube is swept into
    branches; what is left of it is cut as flat pieces, not a scatter of slivers."""
    spans: list[Span] = []
    for component in _components(faces):
        if _is_tube(component, matrix):
            spans.append(Span(faces=list(component), tube=True))
            continue
        rest: list[bmesh.types.BMFace] = []
        for part in _sweep(component, *_axis(_points(component, matrix)), matrix):
            if _is_tube(part.faces, matrix):
                part.tube = True
                spans.append(part)
            else:
                rest.extend(part.faces)
        if rest:
            spans.extend(_flat_spans(rest, matrix))
    return spans


def _box_spans(faces: list[bmesh.types.BMFace], matrix: Matrix) -> list[Span]:
    """Standard Box: one island per connected group of faces facing the same world axis."""
    normals = _world_normals(faces, matrix)
    groups: dict[int, list[bmesh.types.BMFace]] = {}
    for face in faces:
        axis = max(range(len(BOX_AXES)), key=lambda i: normals[face].dot(BOX_AXES[i]))
        groups.setdefault(axis, []).append(face)
    return [
        Span(faces=part, axis=BOX_AXES[axis])
        for axis, group in sorted(groups.items())
        for part in _components(group)
    ]


def _split_distorted(obj: bpy.types.Object, spans: list[Span]) -> bool:
    """Cut flat islands that unwrapped too unevenly (a rock's dome) into one island per box
    side, replacing them in `spans`. Returns whether anything was cut."""
    mesh, layer, matrix = obj.data, obj.data.uv_layers.active, obj.matrix_world
    distorted = []
    for span in spans:
        ratios = sorted(_polygon_ratios(mesh, layer, span.faces, matrix, 1.0))
        if not span.tube and ratios:
            low, high = ratios[len(ratios) // 10], ratios[len(ratios) * 9 // 10]
            if low > 0 and high / low > MAX_DISTORTION:
                distorted.append(span)
    if not distorted:
        return False
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    for span in distorted:
        faces = [bm.faces[index] for index in span.faces]
        parts = _box_sides(faces, _world_normals(faces, matrix))
        owner = {face: i for i, part in enumerate(parts) for face in part}
        for face in faces:
            for edge in face.edges:
                if len({owner[f] for f in edge.link_faces if f in owner}) > 1:
                    edge.seam = True
        spans.remove(span)
        spans.extend(Span(faces=[face.index for face in part]) for part in parts)
    bm.to_mesh(mesh)
    bm.free()
    return True


def _world_normals(
    faces: list[bmesh.types.BMFace], matrix: Matrix
) -> dict[bmesh.types.BMFace, Vector]:
    normal_matrix = matrix.inverted_safe().transposed().to_3x3()
    return {face: (normal_matrix @ face.normal).normalized() for face in faces}


def _box_sides(
    faces: list[bmesh.types.BMFace], normals: dict[bmesh.types.BMFace, Vector]
) -> list[list[bmesh.types.BMFace]]:
    """Connected pieces of the faces grouped by the box side (4 around, top) they face most."""
    sides: dict[int, list[bmesh.types.BMFace]] = {}
    for face in faces:
        side = max(range(len(BOX_DIRECTIONS)), key=lambda i: normals[face].dot(BOX_DIRECTIONS[i]))
        sides.setdefault(side, []).append(face)
    return [part for group in sides.values() for part in _components(group)]


def _flat_spans(faces: list[bmesh.types.BMFace], matrix: Matrix) -> list[Span]:
    """The underside (where a rock meets the ground) as its own islands, so the seam runs
    around it; the rest as one island when it is a disk, else one island per box side."""
    normals = _world_normals(faces, matrix)
    down = Vector((0, 0, -1))
    under = [face for face in faces if normals[face].dot(down) > UNDERSIDE]
    upper = [face for face in faces if normals[face].dot(down) <= UNDERSIDE]
    spans = [Span(faces=group) for group in _components(under)]
    for region in _components(upper):
        if _euler(region) == 1:
            spans.append(Span(faces=region))
        else:
            spans.extend(Span(faces=part) for part in _box_sides(region, normals))
    return spans


def _euler(faces: list[bmesh.types.BMFace]) -> int:
    """V - E + F of the faces: 1 for a disk, 0 for an open tube."""
    verts = {vert for face in faces for vert in face.verts}
    edges = {edge for face in faces for edge in face.edges}
    return len(verts) - len(edges) + len(faces)


def _sweep(
    faces: list[bmesh.types.BMFace],
    axis: Vector,
    origin: Vector,
    matrix: Matrix,
) -> list[Span]:
    step = _mean_edge(faces, matrix)
    projections = [
        ((matrix @ face.calc_center_median() - origin).dot(axis), face) for face in faces
    ]
    start = min(item[0] for item in projections)
    end = max(item[0] for item in projections)
    if end - start > step * _MAX_SLABS:
        step = (end - start) / _MAX_SLABS
    slabs = []
    cursor = start
    while cursor < end - 1e-8:
        nxt = cursor + step
        last = nxt >= end - 1e-9
        group = [
            face
            for position, face in projections
            if cursor <= position < nxt or (last and position >= cursor)
        ]
        if group:
            slabs.append(_components(group))
        cursor = end if last else nxt
    if not slabs:
        return [Span(faces=list(faces), tube=False)]

    spans: list[Span] = []
    previous: list[tuple[Span, set[bmesh.types.BMFace]]] = []
    for groups in slabs:
        matched: dict[int, list[list[bmesh.types.BMFace]]] = {}
        fresh: list[list[bmesh.types.BMFace]] = []
        for group in groups:
            parents = [
                index for index, (_, faceset) in enumerate(previous) if _touches(group, faceset)
            ]
            if len(parents) == 1:
                matched.setdefault(parents[0], []).append(group)
            else:
                fresh.append(group)
        current: list[tuple[Span, set[bmesh.types.BMFace]]] = []
        for index, groups_for_parent in matched.items():
            if len(groups_for_parent) == 1:
                span, _faceset = previous[index]
                span.faces.extend(groups_for_parent[0])
                current.append((span, set(groups_for_parent[0])))
                continue
            for group in groups_for_parent:
                span = Span(faces=list(group))
                spans.append(span)
                current.append((span, set(group)))
        for group in fresh:
            span = Span(faces=list(group))
            spans.append(span)
            current.append((span, set(group)))
        previous = current
    return spans or [Span(faces=list(faces), tube=False)]


def _trunk_axis(spans: list[Span], matrix: Matrix) -> tuple[Vector, Vector] | None:
    """Axis and center of the largest tube, taken as the tree's trunk."""
    tubes = [span for span in spans if span.tube]
    if len(tubes) < 2:
        return None
    trunk = max(tubes, key=lambda span: sum(face.calc_area() for face in span.faces))
    return _axis(_points(trunk.faces, matrix))


def _inward(axis: Vector, origin: Vector, trunk: tuple[Vector, Vector] | None) -> Vector | None:
    """The side of a branch that faces the trunk's axis, so its seam is hidden; None for the
    trunk itself or a branch pointing straight at the axis, where no side faces it."""
    if trunk is None:
        return None
    trunk_axis, trunk_origin = trunk
    offset = trunk_origin - origin
    to_axis = offset - trunk_axis * offset.dot(trunk_axis)
    across = to_axis - axis * to_axis.dot(axis)
    if to_axis.length < 1e-6 or across.length < MIN_INWARD * to_axis.length:
        return None
    return across.normalized()


def _mark_lengthwise(span: Span, matrix: Matrix, trunk: tuple[Vector, Vector] | None) -> None:
    """Seam the span from end to end (to the tip when it is capped) along the side facing the
    trunk, by the shortest path that stays near that side, so it unwraps as one strip."""
    face_set = set(span.faces)
    verts = list({vert for face in span.faces for vert in face.verts})
    world = {vert: matrix @ vert.co for vert in verts}
    axis, origin = _axis(list(world.values()))
    side = _inward(axis, origin, trunk) or _perpendicular(axis)
    along = {vert: (world[vert] - origin).dot(axis) for vert in verts}
    low, high = min(along.values()), max(along.values())
    band = (high - low) * END_BAND

    normal_matrix = matrix.inverted_safe().transposed().to_3x3()

    def facing(vert: bmesh.types.BMVert) -> float:
        return (normal_matrix @ vert.normal).normalized().dot(side)

    boundary = {
        vert
        for face in span.faces
        for edge in face.edges
        if sum(linked in face_set for linked in edge.link_faces) == 1
        for vert in edge.verts
    }

    def end(near: Callable[[float], bool]) -> bmesh.types.BMVert:
        pool = [v for v in boundary if near(along[v])] or [v for v in verts if near(along[v])]
        return max(pool, key=facing)

    start = end(lambda position: position <= low + band)
    goal = end(lambda position: position >= high - band)
    for edge in _side_path(start, goal, face_set, world, facing):
        edge.seam = True
        span.seam_edges.append(edge.index)


def _side_path(
    start: bmesh.types.BMVert,
    goal: bmesh.types.BMVert,
    face_set: set[bmesh.types.BMFace],
    world: dict[bmesh.types.BMVert, Vector],
    facing: Callable[[bmesh.types.BMVert], float],
) -> list[bmesh.types.BMEdge]:
    """Dijkstra over the span's edges; an edge costs its length, more the further it turns
    away from the chosen side."""
    best = {start: 0.0}
    came: dict[bmesh.types.BMVert, bmesh.types.BMEdge] = {}
    queue = [(0.0, 0, start)]
    order = count(1)
    while queue:
        cost, _, vert = heapq.heappop(queue)
        if vert is goal:
            break
        if cost > best[vert]:
            continue
        for edge in vert.link_edges:
            if not any(face in face_set for face in edge.link_faces):
                continue
            other = edge.other_vert(vert)
            weight = 1 + SIDE_PULL * (1 - (facing(vert) + facing(other)) / 2)
            step = cost + (world[other] - world[vert]).length * weight
            if step < best.get(other, math.inf):
                best[other] = step
                came[other] = edge
                heapq.heappush(queue, (step, next(order), other))
    if goal not in came and goal is not start:
        raise ValueError("a bark span has no path from end to end for its seam")
    path = []
    vert = goal
    while vert is not start:
        edge = came[vert]
        path.append(edge)
        vert = edge.other_vert(vert)
    return path


def _unwrap(obj: bpy.types.Object) -> None:
    if "FINISHED" not in bpy.ops.uv.unwrap(method="ANGLE_BASED", correct_aspect=False):
        raise ValueError(f"{obj.name}: unwrap failed")


def _pack(obj: bpy.types.Object) -> None:
    """Fit the islands into 0-1 without turning them, keeping their relative sizes."""
    bpy.ops.uv.select_all(action="SELECT")
    if "FINISHED" not in bpy.ops.uv.pack_islands(rotate=False, margin=PACK_MARGIN):
        raise ValueError(f"{obj.name}: packing the UV islands failed")


def _in_edit_mode(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    scope: set[int],
    action: Callable[[bpy.types.Object], None],
) -> None:
    """Run `action` in Edit Mode with only the scope's faces selected, then put back the mode
    and selection."""
    view = context.view_layer
    active = view.objects.active
    selected = [item for item in view.objects if item.select_get()]
    mode = obj.mode
    try:
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for item in view.objects:
            item.select_set(False)
        obj.select_set(True)
        view.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="DESELECT")
        bm = bmesh.from_edit_mesh(obj.data)
        for face in bm.faces:
            face.select = face.index in scope
        bmesh.update_edit_mesh(obj.data)
        action(obj)
    finally:
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for item in view.objects:
            item.select_set(False)
        for item in selected:
            item.select_set(True)
        view.objects.active = active
        if mode != "OBJECT" and active is not None:
            bpy.ops.object.mode_set(mode=mode)


def _scale_strips(
    obj: bpy.types.Object, spans: list[Span], tile_size: float, aspect: float
) -> None:
    """Scale every span to the same texel density (one U unit = tile_size in world), never
    stretched per span, so the texture tiles evenly and the seam may not meet. A tube is
    turned so its seam runs along V; a box island is turned and placed as its axis would
    project it, so the texture stands upright on the sides."""
    layer = obj.data.uv_layers.active
    matrix = obj.matrix_world
    for span in spans:
        loops = _span_loops(obj.data, layer, span.faces)
        ratios = _polygon_ratios(obj.data, layer, span.faces, matrix, tile_size)
        if not loops or not ratios:
            continue
        transform = Matrix.Scale(sorted(ratios)[len(ratios) // 2], 2)
        if span.tube:
            across = transform @ _seam_vector(obj.data, layer, span.seam_edges, span.faces)
            if across.length > 1e-6:
                transform = Matrix.Rotation(-math.atan2(across.y, across.x), 2) @ transform
        uvs = [transform @ Vector(loop.uv) for loop in loops]
        if span.axis is not None:
            uvs = _to_projection(
                uvs, _projected(obj.data, span.faces, matrix, span.axis, tile_size)
            )
        else:
            low = Vector((min(uv.x for uv in uvs), min(uv.y for uv in uvs)))
            uvs = [uv - low for uv in uvs]
        squash = Matrix.Diagonal((1.0, 1 / aspect))
        for loop, uv in zip(loops, uvs, strict=True):
            loop.uv = squash @ uv


def _projected(
    mesh: bpy.types.Mesh, faces: list[int], matrix: Matrix, axis: Vector, tile_size: float
) -> list[Vector]:
    """Each loop's position seen along `axis`, in tiles: right and up as the viewer sees them."""
    up = Vector((0, 1, 0)) * (1 if axis.z > 0 else -1) if abs(axis.z) > 0.5 else Vector((0, 0, 1))
    right = up.cross(axis)
    points = []
    for index in faces:
        for loop_index in mesh.polygons[index].loop_indices:
            world = matrix @ mesh.vertices[mesh.loops[loop_index].vertex_index].co
            points.append(Vector((world.dot(right), world.dot(up))) / tile_size)
    return points


def _to_projection(uvs: list[Vector], target: list[Vector]) -> list[Vector]:
    """Turn and move `uvs` (same scale) to lie best over `target`, without mirroring."""
    center = sum(uvs, Vector((0, 0))) / len(uvs)
    goal = sum(target, Vector((0, 0))) / len(target)
    pairs = [(uv - center, point - goal) for uv, point in zip(uvs, target, strict=True)]
    angle = math.atan2(sum(a.cross(b) for a, b in pairs), sum(a.dot(b) for a, b in pairs))
    turn = Matrix.Rotation(angle, 2)
    return [turn @ (uv - center) + goal for uv in uvs]


def _triplanar_copy(
    material: bpy.types.Material, obj: bpy.types.Object, tile_size: float, aspect: float
) -> bpy.types.Material:
    """A copy of `material` whose images are box-projected on object coordinates and blended,
    one repeat per tile_size in world. Marked so export bakes it onto the packed Box UVs."""
    copy = material.copy()
    copy.name = material.name + TRIPLANAR_SUFFIX
    copy[io.TRIPLANAR_PROPERTY] = tile_size
    tree = copy.node_tree
    coords = tree.nodes.new("ShaderNodeTexCoord")
    mapping = tree.nodes.new("ShaderNodeMapping")
    repeats = obj.matrix_world.median_scale / tile_size
    mapping.inputs["Scale"].default_value = (repeats, repeats, repeats / aspect)
    tree.links.new(coords.outputs["Object"], mapping.inputs["Vector"])
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image:
            node.projection = "BOX"
            node.projection_blend = TRIPLANAR_BLEND
            tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    return copy


def _span_loops(mesh: bpy.types.Mesh, layer: bpy.types.MeshUVLoopLayer, faces: list[int]):
    loops = []
    for index in faces:
        if index >= len(mesh.polygons):
            continue
        loops.extend(layer.data[loop] for loop in mesh.polygons[index].loop_indices)
    return loops


def _polygon_ratios(
    mesh: bpy.types.Mesh,
    layer: bpy.types.MeshUVLoopLayer,
    faces: list[int],
    matrix: Matrix,
    tile_size: float,
) -> list[float]:
    ratios = []
    seen: set[tuple[int, int]] = set()
    for index in faces:
        if index >= len(mesh.polygons):
            continue
        poly = mesh.polygons[index]
        loops = list(poly.loop_indices)
        for position, loop_index in enumerate(loops):
            nxt = loops[(position + 1) % len(loops)]
            key = tuple(sorted((mesh.loops[loop_index].vertex_index, mesh.loops[nxt].vertex_index)))
            if key in seen:
                continue
            seen.add(key)
            world = (matrix @ mesh.vertices[key[0]].co - matrix @ mesh.vertices[key[1]].co).length
            uv_length = (Vector(layer.data[loop_index].uv) - Vector(layer.data[nxt].uv)).length
            if world > 1e-8 and uv_length > 1e-8:
                ratios.append((world / tile_size) / uv_length)
    return ratios


def _seam_vector(
    mesh: bpy.types.Mesh,
    layer: bpy.types.MeshUVLoopLayer,
    edges: list[int],
    faces: list[int],
) -> Vector:
    """Mean UV offset between the two sides of the lengthwise seam: the way around the tube."""
    face_set = set(faces)
    offsets: list[Vector] = []
    for index in edges:
        edge = mesh.edges[index]
        for vert_index in edge.vertices:
            uvs = [
                Vector(layer.data[loop_index].uv)
                for poly_index in face_set
                for loop_index in mesh.polygons[poly_index].loop_indices
                if mesh.loops[loop_index].vertex_index == vert_index
                and _loop_on_edge(mesh, mesh.polygons[poly_index], loop_index, edge)
            ]
            if len(uvs) >= 2:
                offset = uvs[0] - uvs[1]
                offsets.append(-offset if offsets and offset.dot(offsets[0]) < 0 else offset)
    if not offsets:
        return Vector((0.0, 0.0))
    return sum(offsets, Vector((0.0, 0.0))) / len(offsets)


def _loop_on_edge(
    mesh: bpy.types.Mesh, poly: bpy.types.MeshPolygon, loop_index: int, edge: bpy.types.MeshEdge
) -> bool:
    loops = list(poly.loop_indices)
    position = loops.index(loop_index)
    this = mesh.loops[loop_index].vertex_index
    verts = set(edge.vertices)
    if this not in verts:
        return False
    neighbors = (
        mesh.loops[loops[(position + 1) % len(loops)]].vertex_index,
        mesh.loops[loops[(position - 1) % len(loops)]].vertex_index,
    )
    return any({this, neighbor} == verts for neighbor in neighbors)


def _components(faces: list[bmesh.types.BMFace]) -> list[list[bmesh.types.BMFace]]:
    remaining = set(faces)
    groups = []
    while remaining:
        start = remaining.pop()
        stack = [start]
        group = [start]
        while stack:
            face = stack.pop()
            for edge in face.edges:
                for other in edge.link_faces:
                    if other in remaining:
                        remaining.remove(other)
                        stack.append(other)
                        group.append(other)
        groups.append(group)
    return groups


def _touches(group: list[bmesh.types.BMFace], other: set[bmesh.types.BMFace]) -> bool:
    for face in group:
        for edge in face.edges:
            for linked in edge.link_faces:
                if linked in other:
                    return True
    return False


def _points(faces: list[bmesh.types.BMFace], matrix: Matrix) -> list[Vector]:
    seen: set[bmesh.types.BMVert] = set()
    points = []
    for face in faces:
        for vert in face.verts:
            if vert not in seen:
                seen.add(vert)
                points.append(matrix @ vert.co)
    return points


def _mean_edge(faces: list[bmesh.types.BMFace], matrix: Matrix) -> float:
    lengths = []
    seen: set[bmesh.types.BMEdge] = set()
    for face in faces:
        for edge in face.edges:
            if edge in seen:
                continue
            seen.add(edge)
            lengths.append((matrix @ edge.verts[0].co - matrix @ edge.verts[1].co).length)
    lengths.sort()
    return lengths[len(lengths) // 2] if lengths else 1.0


def _axis(points: list[Vector]) -> tuple[Vector, Vector]:
    origin = sum(points, Vector()) / len(points)
    cov = [[0.0, 0.0, 0.0] for _ in range(3)]
    for point in points:
        offset = point - origin
        for row in range(3):
            for col in range(3):
                cov[row][col] += offset[row] * offset[col]
    axis = Vector((0.0, 0.0, 1.0))
    for _ in range(16):
        multiplied = Vector(
            tuple(sum(cov[row][col] * axis[col] for col in range(3)) for row in range(3))
        )
        if multiplied.length < 1e-12:
            break
        axis = multiplied.normalized()
    return axis, origin


def _is_tube(faces: list[bmesh.types.BMFace], matrix: Matrix) -> bool:
    """An open or capped cylinder whose side normals surround its long axis. Taper, a gentle
    bend and coarse rings still count; a flat patch, a half pipe or a fork does not."""
    verts = {vert for face in faces for vert in face.verts}
    edges = {edge for face in faces for edge in face.edges}
    face_set = set(faces)
    # an edge with a third face is a stray duplicate face, not a fork: each adds one to the
    # Euler characteristic, so discount them
    extra = sum(len([f for f in edge.link_faces if f in face_set]) > 2 for edge in edges)
    if len(verts) < 6 or len(verts) - len(edges) + len(faces) - extra not in (0, 1):
        return False
    verts = list(verts)
    points = [matrix @ vert.co for vert in verts]
    axis, origin = _axis(points)
    along = [(point - origin).dot(axis) for point in points]
    radii = sorted(
        (point - origin - axis * a).length for point, a in zip(points, along, strict=True)
    )
    length = max(along) - min(along)
    if length < MIN_ELONGATION * 2 * radii[len(radii) // 2]:
        return False
    # a tube is open only at its ends; a rock open underneath has its rim along the side
    position = dict(zip(verts, along, strict=True))
    rim = {
        vert
        for edge in edges
        if len([f for f in edge.link_faces if f in face_set]) == 1
        for vert in edge.verts
    }
    if any(END_RIM < (position[vert] - min(along)) / length < 1 - END_RIM for vert in rim):
        return False
    side = _perpendicular(axis)
    other = axis.cross(side)
    rotation = matrix.to_3x3()
    angles = []
    for face in faces:
        normal = (rotation @ face.normal).normalized()
        across = normal - axis * normal.dot(axis)
        if across.length > SIDE_NORMAL:  # caps face along the axis
            angles.append(math.degrees(math.atan2(across.dot(other), across.dot(side))) % 360)
    if len(angles) < 3:
        return False
    angles.sort()
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [360 - angles[-1] + angles[0]]
    return max(gaps) <= TUBE_GAP_DEGREES


def _perpendicular(axis: Vector) -> Vector:
    side = axis.cross(Vector((1.0, 0.0, 0.0)))
    if side.length < 1e-6:
        side = axis.cross(Vector((0.0, 1.0, 0.0)))
    return side.normalized()
