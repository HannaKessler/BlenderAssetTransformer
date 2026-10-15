"""Import library meshes into Blender and export assets to Unity target projects."""

import array
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import bpy

from . import library, licensing, unity

log = logging.getLogger(__name__)

TYPE_PROPERTY = "bat_type"
SOURCE_PROPERTY = "bat_source_mat"  # Unity .mat a Blender material was wired from
LOD_NAME = re.compile(r"_lod\d+(\.\d{3})?$", re.IGNORECASE)  # Unity's LOD Group naming
WIRED_NODES = {"TEX_IMAGE", "NORMAL_MAP", "SEPARATE_COLOR", "MATH", "GROUP"}
SHADER_PROPERTY = "bat_unity_shader"  # Unity shader GUID of the source .mat
VALUES_PROPERTY = "bat_unity_values"  # its shader values, kept so Unity can rebuild the look
# AnimeNaturalEnvironment "ASFolliage Standard": color = lerp(_Color1, _Color0,
# saturate((objectY - _HeightStartGradient) / _HeightGradient)), clip(mask alpha - _Clip).
FOLIAGE_SHADER = "ec957416e2e0fb44dabecd4041dcb5f8"
FOLIAGE_GROUP = "BAT Foliage Preview"
FOLIAGE_INPUTS = {  # group input -> (socket type, .mat property)
    "Color 0": ("NodeSocketColor", "_Color0"),
    "Color 1": ("NodeSocketColor", "_Color1"),
    "Height Start": ("NodeSocketFloat", "_HeightStartGradient"),
    "Height Gradient": ("NodeSocketFloat", "_HeightGradient"),
    "Clip": ("NodeSocketFloat", "_Clip"),
    "Mask Alpha": ("NodeSocketFloat", None),
}
HEIGHT_AXIS = "Y"  # FBX meshes keep Unity's Y-up local coordinates (object is rotated)
ROLE_INPUTS = {
    "BaseColor": "Base Color",
    "Metallic": "Metallic",
    "Roughness": "Roughness",
    "Emission": "Emission Color",
}
ROLE_SUFFIXES = ("BaseColor", "Normal", "Metallic", "Roughness", "AO", "Emission")
ROLE_NAME_HINTS = {
    "BaseColor": ("basecolor", "base_color", "albedo", "diffuse", "maintex"),
    "Normal": ("normal", "nrm"),
    "Metallic": ("metallic", "metalness"),
    "Roughness": ("roughness",),
    "AO": ("occlusion", "_ao"),
    "Emission": ("emission", "emissive"),
}
NON_COLOR_ROLES = {"Normal", "Metallic", "Roughness", "AO"}


@dataclass
class ImportResult:
    objects: list[bpy.types.Object] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    relinked: list[str] = field(default_factory=list)


def _child_collection(parent: bpy.types.Collection, kind: str) -> bpy.types.Collection:
    for child in parent.children:
        if child.get(TYPE_PROPERTY) == kind:
            return child
    child = bpy.data.collections.new(kind)
    child[TYPE_PROPERTY] = kind
    parent.children.link(child)
    return child


def target_collection(scene: bpy.types.Scene, pack: str) -> bpy.types.Collection:
    """The `Pack > Meshes` collection, created if missing."""
    pack_col = next((c for c in scene.collection.children if c.get(TYPE_PROPERTY) == pack), None)
    if pack_col is None:
        pack_col = bpy.data.collections.new(pack)
        pack_col[TYPE_PROPERTY] = pack
        scene.collection.children.link(pack_col)
    return _child_collection(pack_col, library.MESHES)


def _load(path: Path) -> tuple[list[bpy.types.Object], dict[bpy.types.Material, str]]:
    """Import path; returns new objects and each new material's name as stored in the file.

    Existing materials are renamed during import so Blender can't suffix clashing names.
    """
    before = set(bpy.data.objects.keys())  # names: cheap even with thousands of objects
    existing = [m for m in bpy.data.materials if not m.library]
    names = [m.name for m in existing]
    for i, material in enumerate(existing):
        material.name = f"bat_tmp_{i}"
    try:
        ext = path.suffix.lower()
        if ext == ".fbx":
            bpy.ops.wm.fbx_import(filepath=str(path))
        elif ext == ".obj":
            bpy.ops.wm.obj_import(filepath=str(path), forward_axis="NEGATIVE_Z", up_axis="Y")
        elif ext == ".blend":
            with bpy.data.libraries.load(str(path), link=False) as (src, dst):
                dst.objects = src.objects
        else:
            raise ValueError(f"Unsupported mesh type: {path.name}")
    finally:
        source = {m: m.name for m in bpy.data.materials if m not in existing and not m.library}
        for i, material in enumerate(source):
            material.name = f"bat_new_{i}"
        for material, name in zip(existing, names, strict=True):
            material.name = name
        for material, name in source.items():
            material.name = name
    new = set(bpy.data.objects.keys()) - before
    return [bpy.data.objects[n] for n in new], source


def _mat_name(name: str) -> str:
    """Name without Blender's `.001` style suffix."""
    return re.sub(r"\.\d{3}$", "", name)


def _unity_material(index: library.Index, name: str, pack: str) -> Path | None:
    matches = [e.path for e in index.entries if e.type == library.MATERIALS and e.path.stem == name]
    return next((p for p in matches if library.pack_of(index, p) == pack), None) or (
        matches[0] if matches else None
    )


def _textures_by_name(folder: Path, material_name: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    key = material_name.lower()
    for path in sorted(folder.iterdir()):
        stem = path.stem.lower()
        if library.file_type(path) != library.TEXTURES or key not in stem:
            continue
        for role, hints in ROLE_NAME_HINTS.items():
            if role not in found and any(h in stem for h in hints):
                found[role] = path
    return found


def resolve_textures(
    index: library.Index,
    material_name: str,
    pack: str,
    mesh_dir: Path,
    mat_guid: str | None = None,
) -> tuple[dict[str, Path], list[str], Path | None]:
    """Texture paths per role and the Unity .mat used (by the model .meta's GUID first,
    then matched by name), falling back to texture names in mesh_dir."""
    textures: dict[str, Path] = {}
    missing: list[str] = []
    remapped = index.guids.get(mat_guid or "")
    if remapped and remapped.suffix.lower() != ".mat":
        missing.append(f"{material_name}: .meta remap points to {remapped.name}, not a .mat")
        remapped = None
    mat_path = remapped or _unity_material(index, _mat_name(material_name), pack)
    if mat_path:
        for role, guid in unity.material_textures(mat_path).items():
            if guid in index.guids:
                textures[role] = index.guids[guid]
            else:
                missing.append(f"{material_name}: {role} GUID {guid} not in library")
    for role, path in _textures_by_name(mesh_dir, _mat_name(material_name)).items():
        textures.setdefault(role, path)
    if not textures:
        missing.append(f"{material_name}: no textures found")
    return textures, missing, mat_path


def _image_node(tree: bpy.types.NodeTree, path: Path, role: str) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(str(path), check_existing=True)
    node.label = role
    if role in NON_COLOR_ROLES:
        node.image.colorspace_settings.name = "Non-Color"
    return node


def _image_missing(image: bpy.types.Image | None) -> bool:
    return (
        bool(image)
        and not image.packed_file
        and not Path(bpy.path.abspath(image.filepath)).is_file()
    )


def clear_wiring(material: bpy.types.Material) -> None:
    """Remove texture wiring nodes."""
    if material.node_tree:
        for node in [n for n in material.node_tree.nodes if n.type in WIRED_NODES]:
            material.node_tree.nodes.remove(node)


def _find_texture(index: library.Index, name: str, pack: str, mesh_dir: Path) -> list[Path]:
    """Library textures named `name` (case-insensitive): the nearest unique match walking up
    from mesh_dir to the pack, else the unique match anywhere; several paths if ambiguous."""
    found = [
        e.path
        for e in index.entries
        if e.type == library.TEXTURES and e.path.name.lower() == name.lower()
    ]
    pack_root = library.pack_dir(index, pack)
    for folder in [mesh_dir, *mesh_dir.parents]:
        near = [p for p in found if p.is_relative_to(folder)]
        if near or folder == pack_root:
            return near or found
    return found


def _relink_missing(
    material: bpy.types.Material, index: library.Index, pack: str, mesh_dir: Path
) -> tuple[list[str], list[str]]:
    """Point importer image nodes with missing files at library textures of the same name;
    remove the ones that can't be relinked. Returns (relinked, unresolved)."""
    relinked, unresolved = [], []
    nodes = material.node_tree.nodes if material.node_tree else []
    for node in [n for n in nodes if n.type == "TEX_IMAGE"]:
        if node.image and not _image_missing(node.image):
            continue
        name = bpy.path.basename(node.image.filepath) if node.image else ""
        found = _find_texture(index, name, pack, mesh_dir) if name else []
        if len(found) == 1:
            node.image.filepath = str(found[0])
            node.image.reload()
            relinked.append(f"{material.name}: {name} -> {found[0]}")
            continue
        if found:
            unresolved.append(f"{material.name}: {name} ambiguous: {', '.join(map(str, found))}")
        elif name:
            unresolved.append(f"{material.name}: {name} not found in library")
        nodes.remove(node)
    return relinked, unresolved


def _use_vertex_colors(material: bpy.types.Material, objects: list[bpy.types.Object]) -> None:
    """Feed the mesh color attribute into Base Color when no texture drives it."""
    bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None or bsdf.inputs["Base Color"].is_linked:
        return
    for obj in objects:
        attrs = getattr(obj.data, "color_attributes", None)
        if attrs and material.name in obj.material_slots:
            node = material.node_tree.nodes.new("ShaderNodeVertexColor")
            node.layer_name = (attrs.active_color or attrs[0]).name
            material.node_tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
            return


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _foliage_group() -> bpy.types.NodeTree:
    """One shared preview group for every foliage material (Blender-only, never baked)."""
    if group := bpy.data.node_groups.get(FOLIAGE_GROUP):
        return group
    group = bpy.data.node_groups.new(FOLIAGE_GROUP, "ShaderNodeTree")
    for name, (kind, _) in FOLIAGE_INPUTS.items():
        group.interface.new_socket(name, in_out="INPUT", socket_type=kind)
    group.interface.new_socket("Base Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    group.interface.new_socket("Alpha", in_out="OUTPUT", socket_type="NodeSocketFloat")
    nodes, links = group.nodes, group.links
    inp, out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    coords, xyz = nodes.new("ShaderNodeTexCoord"), nodes.new("ShaderNodeSeparateXYZ")
    end, ramp = nodes.new("ShaderNodeMath"), nodes.new("ShaderNodeMapRange")
    mix, clip = nodes.new("ShaderNodeMix"), nodes.new("ShaderNodeMath")
    end.operation, clip.operation = "ADD", "GREATER_THAN"
    mix.data_type, ramp.clamp = "RGBA", True
    colors = [s for s in mix.inputs if s.type == "RGBA"]
    links.new(coords.outputs["Object"], xyz.inputs[0])
    links.new(xyz.outputs[HEIGHT_AXIS], ramp.inputs["Value"])
    links.new(inp.outputs["Height Start"], ramp.inputs["From Min"])
    links.new(inp.outputs["Height Start"], end.inputs[0])
    links.new(inp.outputs["Height Gradient"], end.inputs[1])
    links.new(end.outputs[0], ramp.inputs["From Max"])
    links.new(ramp.outputs["Result"], mix.inputs["Factor"])
    links.new(inp.outputs["Color 1"], colors[0])
    links.new(inp.outputs["Color 0"], colors[1])
    links.new(next(s for s in mix.outputs if s.type == "RGBA"), out.inputs["Base Color"])
    links.new(inp.outputs["Mask Alpha"], clip.inputs[0])
    links.new(inp.outputs["Clip"], clip.inputs[1])
    links.new(clip.outputs[0], out.inputs["Alpha"])
    return group


def apply_unity_shader(material: bpy.types.Material, mat_path: Path) -> None:
    """Record the .mat's shader and values; add the foliage preview for its known shader."""
    guid, values = unity.shader_guid(mat_path), unity.material_values(mat_path)
    material[SHADER_PROPERTY] = guid or ""
    material[VALUES_PROPERTY] = json.dumps(values)
    tree = material.node_tree
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    mask = next((n for n in tree.nodes if n.type == "TEX_IMAGE" and n.label == "BaseColor"), None)
    if guid != FOLIAGE_SHADER or bsdf is None or mask is None:
        return
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = _foliage_group()
    for name, (kind, prop) in FOLIAGE_INPUTS.items():
        if prop is None:
            continue
        if prop not in values:
            raise ValueError(f"{mat_path.name}: foliage shader value {prop} missing")
        value = values[prop]
        if kind == "NodeSocketColor":
            value = (*(_srgb_to_linear(c) for c in value[:3]), 1.0)
        node.inputs[name].default_value = value
    tree.links.new(mask.outputs["Alpha"], node.inputs["Mask Alpha"])
    tree.links.new(node.outputs["Base Color"], bsdf.inputs["Base Color"])
    tree.links.new(node.outputs["Alpha"], bsdf.inputs["Alpha"])


def _prune_dangling(material: bpy.types.Material) -> None:
    """Remove wiring nodes left unconnected after Unity textures replaced their links."""
    nodes = material.node_tree.nodes
    while dangling := [
        n for n in nodes if n.type in WIRED_NODES and not any(o.is_linked for o in n.outputs)
    ]:
        for node in dangling:
            nodes.remove(node)


def wire_material(material: bpy.types.Material, textures: dict[str, Path]) -> None:
    """Connect textures to the material's Principled BSDF (Unity metallic map: R, A=smooth)."""
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    links = tree.links
    for role, path in textures.items():
        image = _image_node(tree, path, role)
        if role == "Normal":
            normal_map = tree.nodes.new("ShaderNodeNormalMap")
            links.new(image.outputs["Color"], normal_map.inputs["Color"])
            links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
        elif role == "Metallic" and "Roughness" not in textures:
            split = tree.nodes.new("ShaderNodeSeparateColor")
            invert = tree.nodes.new("ShaderNodeMath")
            invert.operation = "SUBTRACT"
            invert.inputs[0].default_value = 1.0
            links.new(image.outputs["Color"], split.inputs["Color"])
            links.new(split.outputs["Red"], bsdf.inputs["Metallic"])
            links.new(image.outputs["Alpha"], invert.inputs[1])
            links.new(invert.outputs["Value"], bsdf.inputs["Roughness"])
        elif role in ROLE_INPUTS:
            links.new(image.outputs["Color"], bsdf.inputs[ROLE_INPUTS[role]])
            if role == "Emission":
                bsdf.inputs["Emission Strength"].default_value = 1.0


def import_mesh(scene: bpy.types.Scene, path: Path, index: library.Index) -> ImportResult:
    """Import a library mesh into `Pack > Meshes`, wire textures, and record its license."""
    pack = library.pack_of(index, path)
    images_before = set(bpy.data.images.keys())
    objects, source_names = _load(path)
    result = ImportResult(objects)
    collection = target_collection(scene, pack)
    if any(LOD_NAME.search(o.name) for o in objects):  # keep a LOD set together
        lods = bpy.data.collections.new(path.stem)
        collection.children.link(lods)
        collection = lods
    record = licensing.detect(pack, library.pack_dir(index, pack))
    if record.license == licensing.UNKNOWN:
        result.unresolved.append(f"{pack}: {licensing.UNKNOWN}")
    remap = unity.material_remap(path.with_name(path.name + ".meta"))
    done: set[bpy.types.Material] = set()
    for obj in result.objects:
        for col in obj.users_collection:
            col.objects.unlink(obj)
        collection.objects.link(obj)
        obj[licensing.PROPERTY] = licensing.dumps([record])
        for slot in obj.material_slots:
            if slot.material in source_names and slot.material not in done:
                done.add(slot.material)
                name = source_names[slot.material]
                textures, missing, mat_path = resolve_textures(
                    index, name, pack, path.parent, remap.get(name)
                )
                # Importer image nodes often point at the author's missing files (pink).
                relinked, unlinked = _relink_missing(slot.material, index, pack, path.parent)
                result.relinked += relinked
                result.unresolved += unlinked
                wire_material(slot.material, textures)
                if mat_path:
                    slot.material[SOURCE_PROPERTY] = str(mat_path)
                    apply_unity_shader(slot.material, mat_path)
                _prune_dangling(slot.material)
                result.unresolved += missing
    for material in done:  # untextured: show vertex colors like Unity would
        _use_vertex_colors(material, result.objects)
    new_images = [bpy.data.images[n] for n in set(bpy.data.images.keys()) - images_before]
    for image in [i for i in new_images if i.users == 0]:  # importer images we unlinked
        new_images.remove(image)
        bpy.data.images.remove(image)
    for image in new_images:  # drop `.001` suffixes freed by the removal
        if (base := _mat_name(image.name)) not in bpy.data.images:
            image.name = base
    return result


def _bones(obj: bpy.types.Object) -> frozenset[str]:
    return frozenset(b.name for b in obj.data.bones)


def _target_armature(
    new: bpy.types.Object, existing: list[bpy.types.Object]
) -> bpy.types.Object | None:
    """Existing armature for `new`: same name (ignoring `.001`), else unique same bone set."""
    for match in (
        [o for o in existing if _mat_name(o.name) == _mat_name(new.name)],
        [o for o in existing if _bones(o) == _bones(new)],
    ):
        if len(match) == 1:
            return match[0]
        if match:
            names = ", ".join(o.name for o in match)
            raise ValueError(f"{new.name}: several matching armatures ({names}); select one path")
    return None


def import_animation(
    scene: bpy.types.Scene, path: Path, index: library.Index
) -> tuple[str, ImportResult]:
    """Add the file's actions to the matching scene armature without a second mesh;
    full mesh import if no armature matches. Returns (what happened, result)."""
    existing = [o for o in scene.objects if o.type == "ARMATURE"]
    kinds = ("objects", "meshes", "armatures", "materials", "images", "actions")
    before = {k: set(getattr(bpy.data, k).keys()) for k in kinds}
    objects, _ = _load(path)
    new = {
        k: [getattr(bpy.data, k)[n] for n in set(getattr(bpy.data, k).keys()) - before[k]]
        for k in kinds
    }
    armatures = [o for o in objects if o.type == "ARMATURE"]
    try:
        target = _target_armature(armatures[0], existing) if len(armatures) == 1 else None
    except ValueError:
        bpy.data.batch_remove([i for k in kinds for i in new[k]])
        raise
    if len(armatures) > 1:
        raise ValueError(f"{path.name}: {len(armatures)} armatures; import it as a mesh instead")
    if target is None:
        bpy.data.batch_remove([i for k in kinds for i in new[k]])
        return "no matching armature, full import", import_mesh(scene, path, index)
    actions = new["actions"]
    bpy.data.batch_remove([i for k in kinds[:-1] for i in new[k]])
    for action in actions:
        action.use_fake_user = True  # keep every clip, not only the assigned one
    if actions:
        anim = target.animation_data or target.animation_data_create()
        anim.action = actions[0]
        if actions[0].slots and anim.action_slot is None:
            anim.action_slot = actions[0].slots[0]
    return f"{len(actions)} actions added to {target.name}", ImportResult()


def import_material(
    path: Path, index: library.Index
) -> tuple[list[bpy.types.Material], bool, list[str]]:
    """Rewire scene materials wired from this .mat in place, or create one if none exist.

    Returns (materials, reused, unresolved).
    """
    materials = [m for m in bpy.data.materials if m.get(SOURCE_PROPERTY) == str(path)]
    reused = bool(materials)
    if not reused:
        material = bpy.data.materials.new(path.stem)
        material[SOURCE_PROPERTY] = str(path)
        material.use_fake_user = True  # unassigned; keep it when the file is saved
        materials = [material]
    guid = unity.meta_guid(path.with_name(path.name + unity.META_SUFFIX))
    pack = library.pack_of(index, path)
    textures, missing, _ = resolve_textures(index, path.stem, pack, path.parent, guid)
    for material in materials:
        clear_wiring(material)
        wire_material(material, textures)
        apply_unity_shader(material, path)
    return materials, reused, missing


def collection_objects(collection: bpy.types.Collection) -> list[bpy.types.Object]:
    """All objects in collection and its child collections, in stable order."""
    found: list[bpy.types.Object] = []

    def walk(col: bpy.types.Collection) -> None:
        found.extend(col.objects)
        for child in col.children:
            walk(child)

    walk(collection)
    return list(dict.fromkeys(found))


def records_of(objects: list[bpy.types.Object]) -> list[licensing.Record]:
    return licensing.merge(*(licensing.loads(o.get(licensing.PROPERTY, "")) for o in objects))


def _role_of(node: bpy.types.Node) -> str | None:
    return node.label if node.label in ROLE_SUFFIXES else None


def _texture_jobs(
    objects: list[bpy.types.Object], asset: str
) -> list[tuple[bpy.types.Image, str, bpy.types.Material, str]]:
    """(image, dest stem name, material, role) for each wired texture."""
    materials = list(
        dict.fromkeys(s.material for o in objects for s in o.material_slots if s.material)
    )
    jobs = []
    for material in materials:
        if not material.node_tree:
            continue
        prefix = asset if len(materials) == 1 else f"{asset}_{_mat_name(material.name)}"
        for node in material.node_tree.nodes:
            role = _role_of(node) if node.type == "TEX_IMAGE" and node.image else None
            if role:
                jobs.append((node.image, f"{prefix}_{role}", material, role))
    return jobs


def _source_mat_name(material: bpy.types.Material) -> str:
    """Unity .mat stem when known, else the Blender material name without .001."""
    source = material.get(SOURCE_PROPERTY) or ""
    return Path(source).stem if source else _mat_name(material.name)


def _foliage_tint_srgb(material: bpy.types.Material) -> tuple[float, float, float] | None:
    """ASFolliage leaf color (_Color0) in sRGB, or None when not a foliage material."""
    if material.get(SHADER_PROPERTY) != FOLIAGE_SHADER:
        return None
    values = json.loads(material.get(VALUES_PROPERTY) or "{}")
    color = values.get("_Color0")
    if not color or len(color) < 3:
        return None
    return float(color[0]), float(color[1]), float(color[2])


# Unity's FBX importer resolves these next to the model; PSD often lands with
# alphaIsTransparency off and solid white cards, so we convert everything else to PNG.
_UNITY_COPY_EXT = {".png", ".jpg", ".jpeg"}


def _disk_texture_path(image: bpy.types.Image) -> Path | None:
    """Absolute filepath on disk when the image references an existing file."""
    if not image.filepath:
        return None
    src = Path(bpy.path.abspath(image.filepath))
    return src if src.is_file() else None


def _ensure_pixels(image: bpy.types.Image) -> None:
    """Force CPU pixel decode. PSD refs often report has_data False until .pixels is read."""
    if image.has_data:
        return
    _ = len(image.pixels)
    if not image.has_data and not image.packed_file:
        raise ValueError(f"texture {image.name} has no pixel data to export")


def _read_pixels(image: bpy.types.Image) -> array.array:
    """Flat RGBA float buffer via foreach_get; bpy_prop_array does not support step slicing."""
    _ensure_pixels(image)
    buf = array.array("f", bytes(4 * len(image.pixels)))
    image.pixels.foreach_get(buf)
    return buf


def _save_png(
    image: bpy.types.Image,
    dst: Path,
    tint_srgb: tuple[float, float, float] | None,
) -> None:
    """Write image as PNG, optionally replacing RGB with tint_srgb (keeps alpha)."""
    _ensure_pixels(image)
    old_format = image.file_format
    old_pixels = None
    try:
        if tint_srgb is not None:
            old_pixels = _read_pixels(image)
            tinted = array.array("f", old_pixels)
            r, g, b = (_srgb_to_linear(c) for c in tint_srgb)
            n = len(tinted) // 4
            tinted[0::4] = array.array("f", [r]) * n
            tinted[1::4] = array.array("f", [g]) * n
            tinted[2::4] = array.array("f", [b]) * n
            image.pixels.foreach_set(tinted)
        image.file_format = "PNG"
        image.save(filepath=str(dst))
    finally:
        if old_pixels is not None:
            image.pixels.foreach_set(old_pixels)
        image.file_format = old_format


def _assert_png_has_alpha(path: Path, label: str) -> None:
    """Fail loudly when a foliage mask PNG has no usable alpha channel."""
    if path.suffix.lower() != ".png":
        raise ValueError(f"{label}: foliage mask must be PNG with alpha, got {path.suffix}")
    img = bpy.data.images.load(str(path), check_existing=False)
    try:
        if img.channels < 4:
            raise ValueError(f"{label}: exported {path.name} has no alpha channel")
        alphas = _read_pixels(img)[3::4]
        if not alphas:
            raise ValueError(f"{label}: exported {path.name} has no pixels")
        amin, amax = min(alphas), max(alphas)
        if amax - amin < 0.05:
            raise ValueError(
                f"{label}: exported {path.name} alpha is flat ({amin:.3f}..{amax:.3f}); "
                "Unity cannot cut out leaves"
            )
    finally:
        bpy.data.images.remove(img)


def _copy_library_texture_meta(src: Path | None, dst: Path) -> None:
    """Copy the library texture's .meta beside dst (keeps alphaIsTransparency)."""
    if src is None:
        return
    meta_src = Path(str(src) + unity.META_SUFFIX)
    if meta_src.is_file():
        shutil.copy2(meta_src, Path(str(dst) + unity.META_SUFFIX))


def _write_export_texture(
    image: bpy.types.Image,
    dest_stem: Path,
    tint_srgb: tuple[float, float, float] | None = None,
    *,
    require_alpha: bool = False,
) -> Path:
    """Write image beside the FBX; copy PNG/JPEG, otherwise convert to PNG from disk.

    Prefers the absolute library filepath over the scene image buffer (PSD often has no
    loaded pixels). tint_srgb replaces RGB (keeps alpha) so foliage leaf masks export
    green for Lit, matching ASFolliage _Color0 when the custom shader is not applied yet.
    require_alpha verifies the written PNG has varying alpha (foliage masks).
    """
    src = _disk_texture_path(image)
    ext = src.suffix.lower() if src else ""
    if tint_srgb is None and not require_alpha and ext in _UNITY_COPY_EXT and src is not None:
        dst = dest_stem.with_suffix(ext)
        shutil.copy2(src, dst)
    else:
        dst = dest_stem.with_suffix(".png")
        if src is not None:
            # Throwaway load from disk - do not rely on the scene image's empty buffer.
            tmp = bpy.data.images.load(str(src), check_existing=False)
            try:
                if tmp.alpha_mode == "NONE":
                    tmp.alpha_mode = "STRAIGHT"
                _save_png(tmp, dst, tint_srgb)
            finally:
                bpy.data.images.remove(tmp)
        elif image.packed_file or image.has_data:
            if image.alpha_mode == "NONE":
                image.alpha_mode = "STRAIGHT"
            _save_png(image, dst, tint_srgb)
        else:
            missing = bpy.path.abspath(image.filepath) if image.filepath else "no filepath"
            raise ValueError(
                f"texture {image.name}: no pixel data and file missing on disk ({missing})"
            )
    if require_alpha:
        _assert_png_has_alpha(dst, image.name)
    # Drop stale siblings from earlier exports (e.g. .psd left after a PNG conversion).
    for other in dest_stem.parent.glob(dest_stem.name + ".*"):
        if other != dst and other.suffix.lower() in library.EXTENSIONS.get(library.TEXTURES, ()):
            other.unlink(missing_ok=True)
            meta = Path(str(other) + unity.META_SUFFIX)
            meta.unlink(missing_ok=True)
    _copy_library_texture_meta(src, dst)
    return dst


def _rename_to_source_mats(objects: list[bpy.types.Object]) -> list[tuple[bpy.types.Material, str]]:
    """Rename materials to their Unity .mat stems for FBX (helps Search and Remap)."""
    saved: list[tuple[bpy.types.Material, str]] = []
    materials = dict.fromkeys(s.material for o in objects for s in o.material_slots if s.material)
    for material in materials:
        want = _source_mat_name(material)
        if material.name == want:
            continue
        saved.append((material, material.name))
        material.name = want
    return saved


def _restore_names(saved: list[tuple[bpy.types.Material, str]]) -> None:
    for material, name in reversed(saved):
        material.name = name


# Blender FBX writes Opacity from Principled Alpha's default_value (not the texture link).
# Opacity < 1 makes Unity URP Lit import as Transparent (glassy leaf cards) — do not force it.
# True Alpha Clip / ASFolliage cutout is not available from Blender FBX alone.


def _foliage_export_links(objects: list[bpy.types.Object]) -> list[tuple]:
    """Wire leaf mask into BSDF Base Color / Alpha so FBX sees the texture (not the preview group).

    Leaves Principled Alpha default at 1.0 so FBX Opacity stays Opaque (no Transparent blend).
    Hard cutout needs ASFolliage (or a Unity importer) after remap — Lit cannot do Alpha Clip.
    """
    saved_links: list[tuple] = []
    materials = dict.fromkeys(s.material for o in objects for s in o.material_slots if s.material)
    for material in materials:
        if material.get(SHADER_PROPERTY) != FOLIAGE_SHADER or not material.node_tree:
            continue
        tree = material.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        mask = next(
            (n for n in tree.nodes if n.type == "TEX_IMAGE" and n.label == "BaseColor"), None
        )
        if bsdf is None or mask is None or mask.image is None:
            raise ValueError(f"{material.name}: foliage leaf mask (BaseColor texture) missing")
        if _image_missing(mask.image):
            raise ValueError(f"{material.name}: leaf mask file not found: {mask.image.filepath}")
        if mask.image.alpha_mode == "NONE":
            mask.image.alpha_mode = "STRAIGHT"
        for socket, output in (("Base Color", "Color"), ("Alpha", "Alpha")):
            target = bsdf.inputs[socket]
            old = [(link.from_socket, target) for link in target.links]
            for link in list(target.links):
                tree.links.remove(link)
            new = tree.links.new(mask.outputs[output], target)
            saved_links.append((tree, new, old))
    return saved_links


def _restore_links(saved: list[tuple]) -> None:
    for tree, new, old in reversed(saved):
        tree.links.remove(new)
        for from_socket, to_socket in old:
            tree.links.new(from_socket, to_socket)


def _unity_values(objects: list[bpy.types.Object], asset: str) -> dict:
    """Unity shader GUID + .mat values per material, so Unity can rebuild the look."""
    materials = dict.fromkeys(s.material for o in objects for s in o.material_slots if s.material)
    out: dict = {}
    for m in materials:
        if not m.get(SHADER_PROPERTY):
            continue
        source = m.get(SOURCE_PROPERTY, "")
        guid = ""
        if source:
            meta = Path(source).with_name(Path(source).name + unity.META_SUFFIX)
            guid = unity.meta_guid(meta) or ""
        out[_source_mat_name(m)] = {
            "shader": m.get(SHADER_PROPERTY, ""),
            "values": json.loads(m.get(VALUES_PROPERTY) or "{}"),
            "source": source,
            "mat_guid": guid,
        }
    return out


def export_dir(target: Path, root: str, asset: str) -> Path:
    return target / root / asset


def library_file_dest(
    target: Path, export_root: str, pack: str, src: Path, library_root: Path
) -> Path:
    """Destination path under export_root/<pack>/… preserving pack-relative layout."""
    rel = src.resolve().relative_to((library_root / "Assets" / pack).resolve())
    return target / export_root / pack / rel


def copy_library_file(src: Path, dest: Path, library_root: Path) -> list[Path]:
    """Copy a library file and its sidecar .meta (if any). Does not write new .meta content."""
    if dest.resolve().is_relative_to(library_root.resolve()):
        raise ValueError("Refusing to export into the library project")
    if not src.is_file():
        raise FileNotFoundError(f"Library file not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    written = [dest]
    meta_src = Path(str(src) + unity.META_SUFFIX)
    if meta_src.is_file():
        meta_dest = Path(str(dest) + unity.META_SUFFIX)
        shutil.copy2(meta_src, meta_dest)
        written.append(meta_dest)
    return written


def export_asset(
    objects: list[bpy.types.Object], out_dir: Path, asset: str, library_root: Path | None
) -> list[str]:
    """Write FBX, named textures, and ATTRIBUTION.md into out_dir; returns warnings."""
    if library_root and out_dir.resolve().is_relative_to(library_root.resolve()):
        raise ValueError("Refusing to export into the library project")
    out_dir.mkdir(parents=True, exist_ok=True)
    original_paths = {}
    saved_links: list[tuple] = []
    saved_names: list[tuple[bpy.types.Material, str]] = []
    try:
        for image, name, material, role in _texture_jobs(objects, asset):
            tint = _foliage_tint_srgb(material) if role == "BaseColor" else None
            dst = _write_export_texture(
                image, out_dir / name, tint_srgb=tint, require_alpha=tint is not None
            )
            original_paths[image] = image.filepath
            image.filepath = str(dst)
            if image.alpha_mode == "NONE" and tint is not None:
                image.alpha_mode = "STRAIGHT"
        saved_links = _foliage_export_links(objects)
        saved_names = _rename_to_source_mats(objects)
        bpy.ops.export_scene.fbx(
            filepath=str(out_dir / f"{asset}.fbx"),
            use_selection=True,
            apply_scale_options="FBX_SCALE_ALL",
            axis_forward="-Z",
            axis_up="Y",
            bake_space_transform=True,
            path_mode="RELATIVE",
            embed_textures=False,
        )
    finally:
        _restore_names(saved_names)
        _restore_links(saved_links)
        for image, path in original_paths.items():
            image.filepath = path
    if values := _unity_values(objects, asset):
        (out_dir / f"{asset}.materials.json").write_text(json.dumps(values, indent=2), "utf-8")
    records = records_of(objects)
    (out_dir / licensing.ATTRIBUTION_FILE).write_text(
        licensing.attribution_markdown(asset, records), encoding="utf-8"
    )
    if not records:
        return [f"{asset}: no license record ({licensing.UNKNOWN})"]
    return [f"{r.pack}: {r.license} (check terms)" for r in licensing.restrictive(records)]
