"""Import library meshes into Blender and export assets to Unity target projects."""

import array
import json
import logging
import math
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import library, licensing, unity

log = logging.getLogger(__name__)

TYPE_PROPERTY = "bat_type"
MATERIALS_DIR = "Materials"
NATIVE_IMPORTER_META = "NativeFormatImporter:\n  externalObjects: {}\n  mainObjectFileID: 2100000\n"
SOURCE_PROPERTY = "bat_source_mat"  # Unity .mat a Blender material was wired from
LOOK_COLOR_PROPERTY = "bat_look_color"  # sRGB foliage color over the mask (Apply Look)
TRIPLANAR_PROPERTY = "bat_triplanar"  # tile size of a triplanar material; baked on export
BAKE_SIZES = (256, 4096)  # smallest and largest baked texture edge, in pixels
BAKE_SAMPLES = 4
BAKE_MARGIN = 8  # pixels the bake bleeds past each island against mip seams
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
FBX_ANIM_OFFSET = 1.0  # bpy.ops.wm.fbx_import default; Unity frame 0 -> Blender 1


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


def unit_correction(scene: bpy.types.Scene) -> float:
    """Import scale that cancels the scene's unit scale (FBX holds meters). Export needs none:
    FBX_SCALE_ALL already writes scene units as meters."""
    return 1.0 / scene.unit_settings.scale_length


def unit_note(scene: bpy.types.Scene) -> str | None:
    """Operator message when the scene unit scale isn't 1 (None otherwise)."""
    scale = scene.unit_settings.scale_length
    if scale == 1.0:
        return None
    correction = unit_correction(scene)
    return f"Scene unit scale {scale:g}: FBX imported x{correction:g}, exported in meters"


DATA_FREE = {"EMPTY", "LIGHT", "CAMERA", "LIGHT_PROBE", "SPEAKER"}


def _slot_fcurves(actions: list[bpy.types.Action], obj: bpy.types.Object) -> list:
    """F-curves of every action slot made for obj."""
    from bpy_extras import anim_utils

    curves = []
    for action in actions:
        for slot in action.slots:
            if slot.target_id_type == "OBJECT" and slot.name_display == obj.name:
                if bag := anim_utils.action_get_channelbag_for_slot(action, slot):
                    curves += bag.fcurves
    return curves


def _scale_fcurve(fc: bpy.types.FCurve, factor: float) -> None:
    for key in fc.keyframe_points:
        key.co[1] *= factor
        key.handle_left[1] *= factor
        key.handle_right[1] *= factor


def _normalize_root_scale(objects: list[bpy.types.Object], actions: list[bpy.types.Action]) -> None:
    """Fold a uniform root scale (FBX in centimeters comes in at 0.01) into the data below it,
    so Unity sees scale 1 on the model's top objects with the same world size and motion:
    mesh and armature data and every location under the root (static and keyed, bones too)
    are multiplied by the root scale, and the root's scale becomes 1."""
    done: dict = {}
    for root in [o for o in objects if o.parent is None]:
        factor = root.scale[0]
        if abs(factor - 1) < 1e-6:
            continue
        if max(root.scale) - min(root.scale) > 1e-6 * abs(factor):
            raise ValueError(f"{root.name}: non-uniform scale {tuple(root.scale)}; apply it first")
        scale = Matrix.Scale(factor, 4)
        tree = [root, *root.children_recursive]
        for obj in tree:
            if obj.constraints or (obj.pose and any(b.constraints for b in obj.pose.bones)):
                raise ValueError(f"{obj.name}: constraints block unit scale normalization")
            if any(m.type != "ARMATURE" for m in obj.modifiers):
                raise ValueError(f"{obj.name}: modifiers block unit scale normalization")
            if obj.data is not None and obj.type not in DATA_FREE:
                if not hasattr(obj.data, "transform"):
                    raise ValueError(f"{obj.name}: can't rescale {obj.type} data")
                if done.setdefault(obj.data, factor) != factor:
                    raise ValueError(f"{obj.name}: data shared at different scales")
        for data in {o.data for o in tree if o.data is not None and o.type not in DATA_FREE}:
            if isinstance(data, bpy.types.Mesh):
                data.transform(scale, shape_keys=True)
            else:
                data.transform(scale)
        for obj in tree:
            for fc in _slot_fcurves(actions, obj):
                if obj is root and fc.data_path == "scale":
                    _scale_fcurve(fc, 1 / factor)
                elif obj is not root and fc.data_path == "location":
                    _scale_fcurve(fc, factor)
                elif fc.data_path.startswith("pose.bones[") and fc.data_path.endswith(".location"):
                    _scale_fcurve(fc, factor)
            if obj.pose:
                for bone in obj.pose.bones:
                    bone.location *= factor
            if obj is root:
                obj.scale /= factor
            else:
                obj.location *= factor
                obj.matrix_parent_inverse = scale @ obj.matrix_parent_inverse @ scale.inverted()


def _load(path: Path) -> tuple[list[bpy.types.Object], dict[bpy.types.Material, str]]:
    """Import path; returns new objects and each new material's name as stored in the file.

    Existing materials are renamed during import so Blender can't suffix clashing names.
    """
    before = set(bpy.data.objects.keys())  # names: cheap even with thousands of objects
    actions_before = set(bpy.data.actions.keys())
    existing = [m for m in bpy.data.materials if not m.library]
    names = [m.name for m in existing]
    for i, material in enumerate(existing):
        material.name = f"bat_tmp_{i}"
    try:
        ext = path.suffix.lower()
        if ext == ".fbx":
            scale = unit_correction(bpy.context.scene)
            bpy.ops.wm.fbx_import(filepath=str(path), global_scale=scale)
        elif ext == ".obj":
            bpy.ops.wm.obj_import(
                filepath=str(path),
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
                global_scale=unit_correction(bpy.context.scene),
            )
        elif ext == ".blend":
            with bpy.data.libraries.load(str(path), link=False) as (src, dst):
                dst.objects = src.objects
                dst.scenes = src.scenes[:1]
            blend_scale = 1.0
            for scene in dst.scenes:
                blend_scale = scene.unit_settings.scale_length * unit_correction(bpy.context.scene)
                bpy.data.scenes.remove(scene)
            if blend_scale != 1.0:
                for obj in dst.objects:
                    if obj.parent is None:
                        obj.matrix_world = Matrix.Scale(blend_scale, 4) @ obj.matrix_world
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
    new = [bpy.data.objects[n] for n in set(bpy.data.objects.keys()) - before]
    _normalize_root_scale(new, [a for a in bpy.data.actions if a.name not in actions_before])
    return new, source


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


def srgb_to_linear(c: float) -> float:
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
            value = (*(srgb_to_linear(c) for c in value[:3]), 1.0)
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


TRANSFORM_PATHS = {"location", "rotation_euler", "rotation_quaternion", "scale"}


def _unshare_lod_actions(lods: list[bpy.types.Object]) -> None:
    """LODs often share one take that only holds their transform (e.g. scale 0.001). A static
    one is baked into each LOD's transform and dropped; one with motion is copied per LOD, so
    no LOD depends on another's action slot."""
    from bpy_extras import anim_utils

    shared = [o.animation_data.action for o in lods if o.animation_data and o.animation_data.action]
    for obj in lods:
        anim = obj.animation_data
        if anim is None or anim.action is None:
            continue
        action, slot = anim.action, anim.action_slot
        bag = anim_utils.action_get_channelbag_for_slot(action, slot) if slot else None
        fcurves = list(bag.fcurves) if bag else []
        static = all(
            fc.data_path in TRANSFORM_PATHS
            and max(k.co[1] for k in fc.keyframe_points) - min(k.co[1] for k in fc.keyframe_points)
            < 1e-6
            for fc in fcurves
            if fc.keyframe_points
        )
        if static:
            for fc in fcurves:
                if fc.keyframe_points:
                    getattr(obj, fc.data_path)[fc.array_index] = fc.keyframe_points[0].co[1]
            obj.animation_data_clear()
        elif shared.count(action) > 1:
            own = action.copy()
            own.name = f"{obj.name}|{_mat_name(action.name).split('|')[-1]}"
            anim.action = own
            anim.action_slot = own.slots[slot.identifier]
    for action in set(shared):
        if action.users <= int(action.use_fake_user):
            bpy.data.actions.remove(action)


def import_mesh(scene: bpy.types.Scene, path: Path, index: library.Index) -> ImportResult:
    """Import a library mesh into `Pack > Meshes`, wire textures, and record its license."""
    pack = library.pack_of(index, path)
    images_before = set(bpy.data.images.keys())
    actions_before = set(bpy.data.actions.keys())
    objects, source_names = _load(path)
    _unshare_lod_actions([o for o in objects if LOD_NAME.search(o.name)])
    new_actions = [bpy.data.actions[n] for n in set(bpy.data.actions.keys()) - actions_before]
    if new_actions and path.suffix.lower() == ".fbx":
        owner = next((o.name for o in objects if o.type == "ARMATURE"), path.stem)
        log.info("%s: %s", path.name, apply_unity_clip_loops(path, new_actions, owner))
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
        obj[SOURCE_MODEL_PROPERTY] = str(path)
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


def _action_fcurves(action: bpy.types.Action):
    """Yield every F-Curve in a (layered) action."""
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield from bag.fcurves


def _set_cycles(action: bpy.types.Action, ping_pong: bool) -> None:
    """Make the action evaluate as a repeating cycle (Cycles F-Modifier on each curve)."""
    mode = "MIRROR" if ping_pong else "REPEAT"
    for fcurve in _action_fcurves(action):
        cycles = next((m for m in fcurve.modifiers if m.type == "CYCLES"), None)
        if cycles is None:
            cycles = fcurve.modifiers.new(type="CYCLES")
        cycles.mode_before = mode
        cycles.mode_after = mode
        cycles.cycles_before = 0  # infinite
        cycles.cycles_after = 0


def _replace_previous_actions(actions: list[bpy.types.Action]) -> None:
    """Swap earlier imports of the same clips for the new ones so re-imports take effect."""
    for action in actions:
        base = _mat_name(action.name)
        old = bpy.data.actions.get(base)
        if old is not None and old != action:
            old.user_remap(action)
            bpy.data.actions.remove(old)
            action.name = base


def _clip_action(take: bpy.types.Action, clip: unity.ClipAnimation, name: str) -> bpy.types.Action:
    """Copy of the whole-take action holding only this clip's keys, starting at frame 1."""
    action = take.copy()
    start = clip.first_frame + FBX_ANIM_OFFSET
    end = clip.last_frame + FBX_ANIM_OFFSET
    for fcurve in _action_fcurves(action):
        for frame in (start, end):  # keep the curve's value at the clip edges
            fcurve.keyframe_points.insert(frame, fcurve.evaluate(frame), options={"FAST"})
        for point in reversed(fcurve.keyframe_points):
            if not start <= point.co.x <= end:
                fcurve.keyframe_points.remove(point, fast=True)
        for point in fcurve.keyframe_points:
            point.co.x -= start - FBX_ANIM_OFFSET
            point.handle_left.x -= start - FBX_ANIM_OFFSET
            point.handle_right.x -= start - FBX_ANIM_OFFSET
        fcurve.update()
    old = bpy.data.actions.get(name)
    if old is not None:  # re-import: replace the earlier copy of this clip
        old.user_remap(action)
        bpy.data.actions.remove(old)
    action.name = name
    action.use_fake_user = True
    return action


SOURCE_MODEL_PROPERTY = "bat_source_model"  # library model an object came from (rig settings)
SOURCE_META_PROPERTY = "bat_source_meta"  # model .meta an action's clip settings came from
SOURCE_CLIP_PROPERTY = "bat_source_clip"  # that clip's name there
TAKE_PROPERTY = "bat_take"  # source take a clip action was cut from (multi-object takes)
TAKE_CLIPS_PROPERTY = "bat_take_clips"  # every clip action cut from that take, in order
CLIP_PROPERTY = "bat_clip"  # the clip's name in Unity


def _split_takes(
    actions: list[bpy.types.Action], clips: list[unity.ClipAnimation], owner: str
) -> int:
    """Replace each whole-take action that Unity splits into several clips with one action
    per clip (`<owner>|<clip>`), in place in `actions`. Returns the number of clips made."""
    takes = [clip.take_name for clip in clips]
    made = 0
    for take in [a for a in actions if takes.count(_mat_name(a.name)) > 1]:
        cut = [clip for clip in clips if clip.take_name == _mat_name(take.name)]
        parts = [_clip_action(take, clip, f"{owner}|{clip.name}") for clip in cut]
        if len(take.slots) > 1:  # one take moving several objects: export it back as one
            for part, clip in zip(parts, cut, strict=True):
                part[TAKE_PROPERTY] = clip.take_name
                part[TAKE_CLIPS_PROPERTY] = [p.name for p in parts]
                part[CLIP_PROPERTY] = clip.name
        take.user_remap(parts[0])
        actions.remove(take)
        bpy.data.actions.remove(take)
        actions.extend(parts)
        made += len(parts)
    return made


def apply_unity_clip_loops(path: Path, actions: list[bpy.types.Action], owner: str) -> str:
    """Map ModelImporter clipAnimations loop settings onto matching Blender actions.

    Matches actions by Unity takeName (FBX import names, ignoring `.001`). A take that Unity
    splits into several clips becomes one action per clip named `<owner>|<clip name>` (the
    whole-take action is removed; `actions` is updated in place). Sets manual frame range from
    firstFrame/lastFrame (plus FBX_ANIM_OFFSET). Looping clips get use_cyclic and a Cycles
    F-Modifier so playback repeats. Raises if the .meta or required fields are missing, or if
    no action matches a clip.
    """
    if not actions:
        return "0 actions"
    meta = path.with_name(path.name + unity.META_SUFFIX)
    clips = unity.clip_animations(meta)
    split = _split_takes(actions, clips, owner)
    by_take = {clip.take_name: clip for clip in clips}
    by_name = {f"{owner}|{clip.name}": clip for clip in clips}
    applied = cyclic = 0
    for action in actions:
        if clip := by_take.get(_mat_name(action.name)):  # re-imports arrive as `.001`
            first, last = clip.first_frame, clip.last_frame
        elif clip := by_name.get(action.name):
            first, last = 0.0, clip.last_frame - clip.first_frame  # split clips start at 1
        else:
            continue
        action.use_frame_range = True
        action.frame_start = first + FBX_ANIM_OFFSET
        action.frame_end = last + FBX_ANIM_OFFSET
        action.use_cyclic = clip.loops
        action[SOURCE_META_PROPERTY] = str(meta)  # root motion settings for export
        action[SOURCE_CLIP_PROPERTY] = clip.name
        applied += 1
        if clip.loops:
            _set_cycles(action, clip.ping_pong)
            cyclic += 1
    if not applied:
        raise ValueError(f"{path.name}: no action matches a clipAnimations takeName in the .meta")
    unmatched = len(actions) - applied
    return (
        f"{cyclic} looping / {applied} ranged of {len(actions)} actions"
        f" ({len(clips)} clips in .meta"
        + (f", {split} split from shared takes" if split else "")
        + (f", {unmatched} unmatched" if unmatched else "")
        + ")"
    )


EMPTY_RIG_MATCH = 0.9  # share of an armature's bones an empties-only rig must name


def _armature_for_empties(
    empties: list[bpy.types.Object], existing: list[bpy.types.Object]
) -> bpy.types.Object | None:
    """Scene armature whose bones the empties name (some FBX rigs store bones as Null nodes)."""
    names = {_mat_name(o.name) for o in empties}
    matches = [a for a in existing if len(_bones(a) & names) >= EMPTY_RIG_MATCH * len(a.data.bones)]
    if len(matches) > 1:
        found = ", ".join(a.name for a in matches)
        raise ValueError(f"several armatures match this rig ({found}); keep one in the scene")
    return matches[0] if matches else None


def _bake_onto_armature(
    empties: list[bpy.types.Object], armature: bpy.types.Object
) -> list[bpy.types.Action]:
    """Bake the empties' animation onto the armature's pose bones (and the root empty's onto
    the armature object) by copying world transforms frame by frame. Returns the new action,
    named after the take."""
    from bpy_extras import anim_utils

    takes = {e.animation_data.action for e in empties if e.animation_data}
    takes.discard(None)
    if len(takes) != 1:
        raise ValueError(f"empties-only rig with {len(takes)} takes; expected exactly one")
    take = takes.pop()
    by_name = {_mat_name(e.name): e for e in empties}
    added = []
    for bone in armature.pose.bones:
        if source := by_name.get(bone.name):
            added.append((bone, bone.constraints.new("COPY_TRANSFORMS")))
            added[-1][1].target = source
    root = by_name.get(_mat_name(armature.name))
    if root is not None:
        added.append((armature, armature.constraints.new("COPY_TRANSFORMS")))
        added[-1][1].target = root
    anim = armature.animation_data or armature.animation_data_create()
    previous = anim.action, anim.action_slot
    options = anim_utils.BakeOptions(
        only_selected=False,
        do_pose=True,
        do_object=root is not None,
        do_visual_keying=True,
        do_constraint_clear=False,
        do_parents_clear=False,
        do_clean=False,
        do_location=True,
        do_rotation=True,
        do_scale=True,
        do_bbone=False,
        do_custom_props=False,
    )
    start, end = take.frame_range
    try:
        baked = anim_utils.bake_action(
            armature, action=None, frames=range(int(start), int(end) + 1), bake_options=options
        )
    finally:
        for owner, constraint in added:
            owner.constraints.remove(constraint)
        anim.action = previous[0]
        if previous[0]:
            anim.action_slot = previous[1]
    baked.name = take.name
    return [baked]


def import_animation(
    scene: bpy.types.Scene, path: Path, index: library.Index
) -> tuple[str, ImportResult]:
    """Add the file's actions to the matching scene armature without a second mesh;
    full mesh import if no armature matches. A rig stored as empties (Null nodes) is baked onto
    the scene armature whose bones it names. Returns (what happened, result)."""
    existing = [o for o in scene.objects if o.type == "ARMATURE"]
    kinds = ("objects", "meshes", "armatures", "materials", "images", "actions")
    before = {k: set(getattr(bpy.data, k).keys()) for k in kinds}
    objects, _ = _load(path)
    new = {
        k: [getattr(bpy.data, k)[n] for n in set(getattr(bpy.data, k).keys()) - before[k]]
        for k in kinds
    }
    armatures = [o for o in objects if o.type == "ARMATURE"]
    empty_rig = bool(objects) and all(o.type == "EMPTY" for o in objects)
    try:
        if empty_rig and (target := _armature_for_empties(objects, existing)):
            baked = _bake_onto_armature(objects, target)
            bpy.data.batch_remove([i for k in kinds for i in new[k]])
            new = {k: [] for k in kinds} | {"actions": baked}
        else:
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
    _replace_previous_actions(actions)
    loops = apply_unity_clip_loops(path, actions, target.name)
    anim = target.animation_data or target.animation_data_create()
    if actions and anim.action is None:  # keep the clip a re-import just replaced
        first = min(actions, key=lambda a: (not a.use_cyclic, a.name))  # show a loop if any
        anim.action = first
        if first.slots and anim.action_slot is None:
            anim.action_slot = first.slots[0]
    return f"{len(actions)} actions added to {target.name}; {loops}", ImportResult()


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


def role_node(material: bpy.types.Material | None, role: str) -> bpy.types.Node | None:
    """The image node wired for `role` (one of ROLE_SUFFIXES) on `material`, or None."""
    if material is None or material.node_tree is None:
        return None
    return next(
        (
            node
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image and _role_of(node) == role
        ),
        None,
    )


def scoped_faces(obj: bpy.types.Object, slot: int, scope: str, group: str = "") -> list[int]:
    """Indices of the slot's faces in scope: all of them, those selected in Edit Mode (sync the
    mesh first), or those whose vertices all carry weight in vertex group `group`."""
    faces = [poly for poly in obj.data.polygons if poly.material_index == slot]
    if scope == "SELECTED":
        faces = [poly for poly in faces if poly.select]
    elif scope == "GROUP":
        found = obj.vertex_groups.get(group)
        if found is None:
            raise ValueError(f"{obj.name}: choose a vertex group")
        weighted = {
            vert.index
            for vert in obj.data.vertices
            if any(g.group == found.index and g.weight > 0 for g in vert.groups)
        }
        faces = [poly for poly in faces if all(v in weighted for v in poly.vertices)]
    elif scope != "SLOT":
        raise ValueError(f"unknown scope {scope}")
    if not faces:
        raise ValueError(f"{obj.name}: no faces of slot {slot} in the chosen scope")
    return [poly.index for poly in faces]


def assign_material(obj: bpy.types.Object, faces: list[int], material: bpy.types.Material) -> int:
    """Give `faces` the object's slot holding `material`, adding the slot if needed."""
    if obj.data.users > 1:
        raise ValueError(f"{obj.name}: mesh data is shared; make it single-user first")
    index = next((i for i, s in enumerate(obj.material_slots) if s.material == material), None)
    if index is None:
        obj.data.materials.append(material)
        index = len(obj.material_slots) - 1
    for face in faces:
        obj.data.polygons[face].material_index = index
    return index


def _export_names(objects: list[bpy.types.Object]) -> dict[bpy.types.Material, str]:
    """Unity name per exported material: its source .mat stem. A material sharing that stem
    with another (a copy such as `.001`) exports under its own Blender name instead, so it
    gets its own .mat and textures. The material named like the stem, else the first by
    name, keeps the stem."""
    materials = dict.fromkeys(s.material for o in objects for s in o.material_slots if s.material)
    ordered = sorted(materials, key=lambda m: (m.name != _source_mat_name(m), m.name))
    names: dict[bpy.types.Material, str] = {}
    taken: set[str] = set()
    for material in ordered:
        name = _source_mat_name(material)
        if name in taken:
            base = name = bpy.path.clean_name(material.name)
            suffix = 2
            while name in taken:
                name, suffix = f"{base}_{suffix}", suffix + 1
        taken.add(name)
        names[material] = name
    return names


def _texture_jobs(
    names: dict[bpy.types.Material, str], asset: str
) -> list[tuple[bpy.types.Image, str, bpy.types.Material, str]]:
    """(image, dest stem name, material, role) for each wired texture."""
    jobs = []
    for material, name in names.items():
        if not material.node_tree:
            continue
        own = name if name != _source_mat_name(material) else _mat_name(material.name)
        prefix = asset if len(names) == 1 else f"{asset}_{own}"
        for node in material.node_tree.nodes:
            role = _role_of(node) if node.type == "TEX_IMAGE" and node.image else None
            if role:
                jobs.append((node.image, f"{prefix}_{role}", material, role))
    return jobs


def _source_mat_name(material: bpy.types.Material) -> str:
    """Unity .mat stem when known, else the Blender material name without .001."""
    source = material.get(SOURCE_PROPERTY) or ""
    return Path(source).stem if source else _mat_name(material.name)


def _mask_fill_srgb(material: bpy.types.Material) -> tuple[float, float, float] | None:
    """RGB the exported base map is filled with, keeping its alpha: white under a foliage look
    (its .mat carries the color), else the ASFolliage leaf color."""
    if material.get(LOOK_COLOR_PROPERTY):
        return 1.0, 1.0, 1.0
    return _foliage_tint_srgb(material)


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


def read_pixels(image: bpy.types.Image) -> array.array:
    """Flat RGBA float buffer via foreach_get; bpy_prop_array does not support step slicing."""
    _ensure_pixels(image)
    buf = array.array("f", bytes(4 * len(image.pixels)))
    image.pixels.foreach_get(buf)
    return buf


def save_png(
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
            old_pixels = read_pixels(image)
            tinted = array.array("f", old_pixels)
            r, g, b = (srgb_to_linear(c) for c in tint_srgb)
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
        alphas = read_pixels(img)[3::4]
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


LINEAR_ROLES = {"Metallic", "Roughness", "AO"}  # data maps: never sRGB
# Without serializedVersion Unity reads a minimal TextureImporter as a legacy one
# (a color map imports as a single-channel texture and renders black)
TEXTURE_IMPORTER_VERSION = 10
DEFAULT_TEXTURE_TYPE = 0  # TextureImporter textureType for color and data maps
NORMAL_TEXTURE_TYPE = 1  # TextureImporter textureType for normal maps


def _write_texture_meta(src: Path | None, dst: Path, role: str) -> str:
    """Write dst's .meta: the library texture's import settings when there are any (keeps
    alphaIsTransparency, normal type), else minimal ones for the role. Data maps are made
    linear. The GUID is dst's own (kept across re-exports), so textures shared by several
    assets don't clash with each other or the library. Returns the GUID."""
    meta_src = Path(str(src) + unity.META_SUFFIX) if src else None
    if meta_src and meta_src.is_file():
        text, count = re.subn(
            r"(?m)^(fileFormatVersion|guid): .*\n", "", meta_src.read_text("utf-8")
        )
        if count != 2:
            raise ValueError(f"{meta_src.name}: expected fileFormatVersion and guid lines")
    else:
        texture_type = NORMAL_TEXTURE_TYPE if role == "Normal" else DEFAULT_TEXTURE_TYPE
        text = (
            f"TextureImporter:\n  serializedVersion: {TEXTURE_IMPORTER_VERSION}\n"
            f"  textureType: {texture_type}\n"
        )
        if role in LINEAR_ROLES:
            text += "  mipmaps:\n    sRGBTexture: 0\n"
    if role in LINEAR_ROLES:
        text = re.sub(r"(?m)^(\s+sRGBTexture:) 1$", r"\g<1> 0", text)
    return unity.write_asset_meta(Path(str(dst) + unity.META_SUFFIX), text)


SHADERS_DIR = "Shaders"
BUILTIN_GUID_PREFIX = "0000000000000000"  # Unity built-in resources (Standard shader...)


def _bsdf_mat_text(material: bpy.types.Material) -> str:
    """Pseudo .mat text from a Principled BSDF, for materials without a Unity source."""
    bsdf = next(
        (
            n
            for n in (material.node_tree.nodes if material.node_tree else ())
            if n.type == "BSDF_PRINCIPLED"
        ),
        None,
    )
    if bsdf is None:
        r, g, b, a = material.diffuse_color
        return f"_Color: {{r: {r}, g: {g}, b: {b}, a: {a}}}\n_Glossiness: 0.5\n"
    value = {i.name: i.default_value for i in bsdf.inputs if hasattr(i, "default_value")}
    r, g, b, a = value["Base Color"]
    return (
        f"_Color: {{r: {r}, g: {g}, b: {b}, a: {value['Alpha']}}}\n"
        f"_Metallic: {value['Metallic']}\n_Glossiness: {1 - value['Roughness']}\n"
    )


def _assets_dir(path: Path) -> Path:
    for parent in path.resolve().parents:
        if parent.name == "Assets":
            return parent
    raise ValueError(f"{path} is not inside a Unity project's Assets folder")


def _ship_shader(files: list[Path], pack_dir: Path, ship_dir: Path) -> None:
    """Copy shader files (with their .meta, so GUIDs match the material) into ship_dir,
    unless the target project already has that shader."""
    guid = unity.meta_guid(Path(str(files[0]) + unity.META_SUFFIX))
    assets = _assets_dir(ship_dir)
    for suffix in unity.SHADER_SUFFIXES:
        for meta in assets.rglob(f"*{suffix}{unity.META_SUFFIX}"):
            if (
                not meta.resolve().is_relative_to(ship_dir.resolve())
                and unity.meta_guid(meta) == guid
            ):
                return
    for src in files:
        dst = ship_dir / src.resolve().relative_to(pack_dir.resolve())
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        shutil.copy2(str(src) + unity.META_SUFFIX, str(dst) + unity.META_SUFFIX)


def _copy_library_texture(src: Path, dest_stem: Path, role: str) -> str:
    """Bring a library texture the Blender material doesn't use (custom-shader inputs)."""
    image = bpy.data.images.load(str(src), check_existing=False)
    try:
        return _write_export_texture(image, dest_stem, role=role)[1]
    finally:
        bpy.data.images.remove(image)


def _export_materials(
    out_dir: Path,
    fbx_names: dict[bpy.types.Material, str],
    names: dict[bpy.types.Material, str],
    textures: dict[tuple, str],
    asset: str,
    library_root: Path | None,
) -> tuple[dict[str, str], list[str]]:
    """Write a .mat per material: the source material with its URP pack shader shipped when
    that shader compiles under URP, else URP Lit. Textures the source .mat references but
    Blender didn't export are copied from the library. An existing .mat keeps everything
    but its texture references. Returns FBX material name -> .mat GUID, and notes."""
    remaps: dict[str, str] = {}
    notes: list[str] = []
    every: dict[str, Path] = {}  # whole-library GUID index, built when a pack lacks a file

    def find(index: dict[str, Path], guid: str) -> Path | None:
        if guid not in index and not every and library_root:
            every.update(unity.guid_index(library_root / "Assets"))
        return index.get(guid) or every.get(guid)

    for material, fbx_name in fbx_names.items():
        exported = {role: g for (m, role), g in textures.items() if m == material}
        source = material.get(SOURCE_PROPERTY)
        if not source:
            stem = bpy.path.clean_name(fbx_name)
            text = unity.lit_material(
                fbx_name,
                _bsdf_mat_text(material),
                {r: g for r, g in exported.items() if r in unity.LIT_SLOTS},
            )
        else:
            source_mat = Path(source)
            if not source_mat.is_file():
                raise ValueError(f"{material.name}: source material not found: {source_mat}")
            if library_root is None:
                raise ValueError(f"{material.name}: no library set for its source material")
            stem = names[material]  # the source stem unless this is a copy
            pack_dir = (
                library_root
                / "Assets"
                / source_mat.resolve().relative_to((library_root / "Assets").resolve()).parts[0]
            )
            index = unity.guid_index(pack_dir)
            source_text = source_mat.read_text(encoding="utf-8", errors="replace")
            look_color = material.get(LOOK_COLOR_PROPERTY)
            look_color = tuple(look_color) if look_color else None
            tinted = _foliage_tint_srgb(material) is not None and look_color is None
            shader_guid = unity.shader_guid(source_mat) or ""
            builtin = shader_guid.startswith(BUILTIN_GUID_PREFIX)
            # a look's flat color over the mask is a URP Lit cutout, whatever the pack shader
            shader = None if builtin or look_color else find(index, shader_guid)
            files = unity.urp_shader_files(shader, index) if shader else None
            if shader and files is None and library_root:  # dependencies in another pack?
                every.update(every or unity.guid_index(library_root / "Assets"))
                files = unity.urp_shader_files(shader, every)
            mapping: dict[str, str] = {}  # library texture GUID -> exported GUID
            for prop, guid in unity.texture_refs(source_text).items():
                role = unity.TEXTURE_SLOTS.get(prop, "")
                if files is None and role not in unity.LIT_SLOTS:
                    continue
                if role in exported and not (files and tinted and role == "BaseColor"):
                    mapping[guid] = exported[role]
                elif guid not in mapping:
                    src = find(index, guid)
                    if src is None:  # dangling in the library too: Unity shows no texture
                        notes.append(f"{stem}: {prop} texture missing from the library; left empty")
                        mapping[guid] = ""
                    else:
                        mapping[guid] = _copy_library_texture(
                            src, out_dir / f"{asset}_{stem}{prop}", role
                        )
                if files is None and mapping[guid]:
                    exported.setdefault(role, mapping[guid])
            if files:
                _ship_shader(files, library_root / "Assets", out_dir.parent / SHADERS_DIR)
                text = unity.set_texture_refs(
                    source_text,
                    {p: mapping[g] for p, g in unity.texture_refs(source_text).items()},
                )
                # a variant's parent stays in the library: keep it as a plain material
                text = re.sub(r"(?m)^(  m_Parent: )\{[^}]*\}", r"\1{fileID: 0}", text)
                text = unity.set_material_name(text, stem)
            else:
                if look_color:
                    notes.append(f"{stem}: foliage look exported as a URP Lit cutout")
                elif not builtin:
                    name = shader.name if shader else f"{shader_guid} (not in library)"
                    notes.append(f"{stem}: shader {name} isn't URP-ready; used URP Lit")
                text = unity.lit_material(
                    stem,
                    source_mat,
                    {r: g for r, g in exported.items() if r in unity.LIT_SLOTS},
                    color_baked=tinted,
                    base_color=look_color,
                )
        mat_path = out_dir / MATERIALS_DIR / f"{stem}.mat"
        mat_path.parent.mkdir(exist_ok=True)
        if mat_path.is_file():  # keep edits made in Unity; refresh texture refs only
            current = mat_path.read_text(encoding="utf-8")
            text = unity.set_texture_refs(current, unity.texture_refs(text))
            if text != current:
                mat_path.write_text(text, "utf-8")
        else:
            mat_path.write_text(text, "utf-8")
        remaps[fbx_name] = unity.write_asset_meta(
            Path(str(mat_path) + unity.META_SUFFIX), NATIVE_IMPORTER_META
        )
    return remaps, notes


def _write_export_texture(
    image: bpy.types.Image,
    dest_stem: Path,
    tint_srgb: tuple[float, float, float] | None = None,
    *,
    require_alpha: bool = False,
    role: str = "",
) -> tuple[Path, str]:
    """Write image beside the FBX; copy PNG/JPEG, otherwise convert to PNG from disk.

    Prefers the absolute library filepath over the scene image buffer (PSD often has no
    loaded pixels). tint_srgb replaces RGB (keeps alpha) so foliage leaf masks export
    green for Lit, matching ASFolliage _Color0 when the custom shader is not applied yet.
    require_alpha verifies the written PNG has varying alpha (foliage masks).
    Returns the written path and its .meta GUID.
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
                save_png(tmp, dst, tint_srgb)
            finally:
                bpy.data.images.remove(tmp)
        elif image.packed_file or image.has_data:
            if image.alpha_mode == "NONE":
                image.alpha_mode = "STRAIGHT"
            save_png(image, dst, tint_srgb)
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
    return dst, _write_texture_meta(src, dst, role)


def _rename_to_source_mats(
    names: dict[bpy.types.Material, str],
) -> list[tuple[bpy.types.Material, str]]:
    """Rename materials to their Unity names for FBX (helps Search and Remap)."""
    saved: list[tuple[bpy.types.Material, str]] = []
    for material, want in names.items():
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


def _unity_values(names: dict[bpy.types.Material, str]) -> dict:
    """Unity shader GUID + .mat values per material, so Unity can rebuild the look."""
    out: dict = {}
    for m, name in names.items():
        if not m.get(SHADER_PROPERTY):
            continue
        source = m.get(SOURCE_PROPERTY, "")
        guid = ""
        if source:
            meta = Path(source).with_name(Path(source).name + unity.META_SUFFIX)
            guid = unity.meta_guid(meta) or ""
        out[name] = {
            "shader": m.get(SHADER_PROPERTY, ""),
            "values": json.loads(m.get(VALUES_PROPERTY) or "{}"),
            "source": source,
            "mat_guid": guid,
        }
    return out


def _owned_slot(action: bpy.types.Action, obj: bpy.types.Object) -> bpy.types.ActionSlot | None:
    """The action's slot made for this object (by name), or None if it animates something else."""
    for slot in action.slots:
        if slot.target_id_type == "OBJECT" and _mat_name(slot.name_display) == _mat_name(obj.name):
            return slot
    return None


def _push_takes(objects: list[bpy.types.Object]) -> tuple[dict[str, bpy.types.Action], list]:
    """Put each object's own actions on temporary NLA strips named like the take
    (`<Object>|<Clip>`, without repeating an existing `<Object>|` prefix) and mute the
    object's other tracks, so the FBX exports only this asset's clips. Returns take -> action
    and what to undo."""
    takes: dict[str, bpy.types.Action] = {}
    undo: list = []
    for obj in objects:
        anim = obj.animation_data
        if anim is None:
            continue
        for track in anim.nla_tracks:
            if not track.mute:
                track.mute = True
                undo.append(track)
        for action in bpy.data.actions:
            slot = anim.action_slot if action == anim.action else _owned_slot(action, obj)
            if slot is None:
                continue
            prefix = f"{obj.name}|"
            take = action.name if action.name.startswith(prefix) else prefix + action.name
            track = anim.nla_tracks.new()
            undo.append((obj, track))
            strip = track.strips.new(take, int(action.frame_range[0]), action)
            strip.action_slot = slot
            strip.name = take
            takes[strip.name] = action
        if anim.action:  # the strips drive the object; an active shared action loses its slot
            undo.append((anim, anim.action, anim.action_slot))
            anim.action = None
    return takes, undo


def _pop_takes(undo: list) -> None:
    for item in reversed(undo):
        if isinstance(item, tuple) and len(item) == 3:
            anim, action, slot = item
            anim.action = action
            anim.action_slot = slot
        elif isinstance(item, tuple):
            obj, track = item
            obj.animation_data.nla_tracks.remove(track)
        else:
            item.mute = False


def _root_motion(clip: unity.ClipAnimation, action: bpy.types.Action) -> unity.ClipAnimation:
    """The clip with its source clip's root motion settings, when it came from the library."""
    meta = action.get(SOURCE_META_PROPERTY)
    if not meta or not Path(meta).is_file():
        return clip
    text = Path(meta).read_text(encoding="utf-8", errors="replace")
    return replace(clip, root_motion=unity.clip_root_motion(text, action[SOURCE_CLIP_PROPERTY]))


def _rig(
    objects: list[bpy.types.Object], asset: str, clips: list[unity.ClipAnimation]
) -> tuple[dict[str, str] | None, list[str]]:
    """Rig settings of the library model the objects and all their clips came from; raises
    if several models disagree. None (with a note) when the clips come from another file or
    the source copies an avatar from elsewhere in the library: that avatar isn't exported."""
    sources = {o[SOURCE_MODEL_PROPERTY] for o in objects if o.get(SOURCE_MODEL_PROPERTY)}
    clip_metas = {
        a.get(SOURCE_META_PROPERTY)
        for o in objects
        if o.animation_data
        for a in bpy.data.actions
        if _owned_slot(a, o)
    }
    if not clips or clip_metas != {p + unity.META_SUFFIX for p in sources}:
        return None, []
    rigs = []
    for path in sorted(sources):
        meta = Path(path + unity.META_SUFFIX)
        if meta.is_file():
            rigs.append(unity.rig_settings(meta.read_text(encoding="utf-8", errors="replace")))
    if not rigs:
        return None, []
    if any(r != rigs[0] for r in rigs):
        raise ValueError(f"{asset}: its source models have different rig settings")
    if rigs[0].get("avatarSetup") == str(unity.AVATAR_COPY):
        return None, [f"{asset}: source avatar is copied from another model; rig not carried"]
    names = {o.name for o in objects} | {b.name for o in objects for b in _bone_list(o)}
    return rigs[0], unity.check_rig(rigs[0], names, asset)


def _bone_list(obj: bpy.types.Object) -> list:
    return list(obj.data.bones) if obj.type == "ARMATURE" else []


def _shared_take(objects: list[bpy.types.Object]) -> tuple[str, list[bpy.types.Action]] | None:
    """The one multi-object source take these objects' clips were cut from, with its clip
    actions in order; None if the clips have no such origin. Raises if the clips no longer
    match the recorded set (deleted or renamed) or mix with other actions."""
    owned = {a for o in objects if o.animation_data for a in bpy.data.actions if _owned_slot(a, o)}
    cut = [a for a in owned if a.get(TAKE_PROPERTY)]
    if not cut:
        return None
    takes = {a[TAKE_PROPERTY] for a in cut}
    if len(takes) > 1 or len(cut) != len(owned):
        others = sorted(a.name for a in owned if a not in cut) or sorted(takes)
        raise ValueError(f"clips from one shared take mixed with others ({', '.join(others)})")
    names = list(cut[0][TAKE_CLIPS_PROPERTY])
    if sorted(a.name for a in cut) != sorted(names) or any(
        a.name.split("|")[-1] != a[CLIP_PROPERTY] for a in cut
    ):
        found = ", ".join(sorted(a.name for a in cut))
        raise ValueError(
            f"take {takes.pop()} was cut into {', '.join(names)}, but found {found}; "
            "restore the deleted or renamed clip actions"
        )
    return cut[0][TAKE_PROPERTY], [bpy.data.actions[n] for n in names]


def _push_shared_take(
    objects: list[bpy.types.Object], take: str, actions: list[bpy.types.Action]
) -> tuple[list[unity.ClipAnimation], list]:
    """Lay the clip actions end to end on NLA strips for every object, and set the scene up so
    the FBX gets one take named `take` covering them. Returns the Unity clips cutting that
    take, and what to undo."""
    scene = bpy.context.scene
    if take in bpy.data.scenes and bpy.data.scenes[take] != scene:
        raise ValueError(f"a scene is named {take}; rename it to export this take")
    undo: list = [(scene, scene.name, scene.frame_start, scene.frame_end, scene.frame_current)]
    clips, frame = [], 0
    tracks: dict[bpy.types.Object, bpy.types.NlaTrack] = {}  # one track: strips play in turn
    for action in actions:
        start, end = action.frame_range
        for obj in objects:
            slot = _owned_slot(action, obj)
            if slot is None:
                continue
            anim = obj.animation_data
            if not any(isinstance(u, tuple) and u[0] == "obj" and u[1] is obj for u in undo):
                undo.append(("obj", obj, anim.action, anim.action_slot))
                for track in anim.nla_tracks:
                    if not track.mute:
                        track.mute = True
                        undo.append(track)
                anim.action = None
            if obj not in tracks:
                tracks[obj] = anim.nla_tracks.new()
                undo.append((obj, tracks[obj]))
            strip = tracks[obj].strips.new(action[CLIP_PROPERTY], frame, action)
            strip.action_slot = slot
        clips.append(
            _root_motion(
                unity.ClipAnimation(
                    name=action[CLIP_PROPERTY],
                    take_name=take,
                    first_frame=float(frame),
                    last_frame=frame + end - start,
                    wrap_mode=unity.WRAP_LOOP if action.use_cyclic else unity.WRAP_DEFAULT,
                    loop=action.use_cyclic,
                    loop_time=action.use_cyclic,
                ),
                action,
            )
        )
        frame = math.ceil(frame + end - start) + 1
    scene.name = take
    scene.frame_start, scene.frame_end = 0, frame - 1
    return clips, undo


def _pop_shared_take(undo: list) -> None:
    for item in reversed(undo):
        if isinstance(item, tuple) and item[0] == "obj":
            _, obj, action, slot = item
            obj.animation_data.action = action
            obj.animation_data.action_slot = slot
        elif isinstance(item, tuple) and len(item) == 5:
            scene, name, start, end, current = item
            scene.name, scene.frame_start, scene.frame_end = name, start, end
            scene.frame_set(current)
        elif isinstance(item, tuple):
            obj, track = item
            obj.animation_data.nla_tracks.remove(track)
        else:
            item.mute = False


def _fbx_take_names(fbx: Path) -> list[str]:
    """AnimStack (take) names Unity will see in an FBX written by Blender."""
    from io_scene_fbx import parse_fbx  # bundled with Blender, like the FBX exporter itself
    from io_scene_fbx.fbx_utils import FBX_NAME_CLASS_SEP

    root, _ = parse_fbx.parse(str(fbx))
    objects = next(e for e in root.elems if e.id == b"Objects")
    stacks = (e for e in objects.elems if e.id == b"AnimationStack")
    return [e.props[1].split(FBX_NAME_CLASS_SEP)[0].decode() for e in stacks]


def _bake_triplanar(
    objects: list[bpy.types.Object],
) -> tuple[list[tuple[bpy.types.Node, bpy.types.Image]], list[str]]:
    """Bake each triplanar material's textures onto its mesh's packed UVs, so the export ships
    plain textures URP Lit can show. Each baked image replaces the original on its node until
    `_unswap`. Returns the swaps and notes."""
    materials = sorted(
        {
            slot.material
            for obj in objects
            for slot in obj.material_slots
            if slot.material and slot.material.get(TRIPLANAR_PROPERTY)
        },
        key=lambda material: material.name,
    )
    if not materials:
        return [], []
    scene, view = bpy.context.scene, bpy.context.view_layer
    engine, samples = scene.render.engine, scene.cycles.samples
    active, selected = view.objects.active, [obj for obj in view.objects if obj.select_get()]
    swaps: list[tuple[bpy.types.Node, bpy.types.Image]] = []
    notes = []
    try:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = BAKE_SAMPLES
        for material in materials:
            users = [obj for obj in objects if material.name in obj.data.materials]
            if len(users) != 1:
                raise ValueError(
                    f"{material.name}: a triplanar material bakes onto one mesh; retile each mesh"
                )
            size = _bake_material(material, users[0], swaps)
            notes.append(f"{material.name}: triplanar textures baked at {size} px")
    except Exception:
        _unswap(swaps)
        raise
    finally:
        scene.render.engine, scene.cycles.samples = engine, samples
        for obj in view.objects:
            obj.select_set(obj in selected)
        view.objects.active = active
    return swaps, notes


def _bake_material(
    material: bpy.types.Material,
    obj: bpy.types.Object,
    swaps: list[tuple[bpy.types.Node, bpy.types.Image]],
) -> int:
    """Bake every role image of `material` on a throwaway copy of `obj` holding only its faces
    (Cycles would otherwise bake into other materials' images too). Returns the texture size."""
    view = bpy.context.view_layer
    temp = obj.copy()
    temp.data = obj.data.copy()
    bpy.context.scene.collection.objects.link(temp)
    try:
        slots = {i for i, slot in enumerate(obj.material_slots) if slot.material == material}
        bm = bmesh.new()
        bm.from_mesh(temp.data)
        bmesh.ops.delete(
            bm, geom=[f for f in bm.faces if f.material_index not in slots], context="FACES"
        )
        for face in bm.faces:
            face.material_index = 0
        bm.to_mesh(temp.data)
        bm.free()
        temp.data.materials.clear()
        temp.data.materials.append(material)
        for item in view.objects:
            item.select_set(item == temp)
        view.objects.active = temp
        size = _bake_size(temp, material)
        for node in [n for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]:
            role = _role_of(node)
            if role is None:
                continue
            # Cycles premultiplies Color by an unlinked alpha (often smoothness): keep them apart
            mode, node.image.alpha_mode = node.image.alpha_mode, "CHANNEL_PACKED"
            try:
                baked = _bake_socket(material, node.outputs["Color"], size, role)
                alpha = None
                if node.image.channels == 4:
                    alpha = _bake_socket(material, node.outputs["Alpha"], size, "Alpha")
            finally:
                node.image.alpha_mode = mode
            if alpha:
                pixels = array.array("f", bytes(4 * 4 * size * size))
                alpha.pixels.foreach_get(pixels)
                values = array.array("f", bytes(4 * 4 * size * size))
                baked.pixels.foreach_get(values)
                values[3::4] = pixels[0::4]
                baked.pixels.foreach_set(values)
                bpy.data.images.remove(alpha)
            baked.pack()
            swaps.append((node, node.image))
            node.image = baked
    finally:
        mesh = temp.data
        bpy.data.objects.remove(temp)
        bpy.data.meshes.remove(mesh)
    return size


def _bake_size(obj: bpy.types.Object, material: bpy.types.Material) -> int:
    """Texture edge that keeps the source texels per meter on the packed layout."""
    mesh, layer = obj.data, obj.data.uv_layers.active
    world = sum(poly.area for poly in mesh.polygons) * obj.matrix_world.median_scale**2
    uv = 0.0
    for poly in mesh.polygons:
        points = [Vector(layer.data[loop].uv) for loop in poly.loop_indices]
        uv += abs(sum(a.cross(b) for a, b in zip(points, points[1:] + points[:1], strict=True)))
    if world <= 0 or uv <= 0:
        raise ValueError(f"{material.name}: nothing to bake onto (no area or UVs)")
    widths = [
        n.image.size[0] for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image
    ]
    texels = max(widths) / material[TRIPLANAR_PROPERTY] * math.sqrt(world / (uv / 2))
    low, high = BAKE_SIZES
    return min(high, max(low, 2 ** math.ceil(math.log2(max(texels, 1)))))


def _bake_socket(
    material: bpy.types.Material, socket: bpy.types.NodeSocket, size: int, role: str
) -> bpy.types.Image:
    """Bake what `socket` outputs, unlit, into a new size x size image."""
    tree = material.node_tree
    output = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output)
    surface = output.inputs["Surface"]
    previous = [link.from_socket for link in surface.links]
    image = bpy.data.images.new(f"{material.name}_{role}_Baked", size, size, alpha=True)
    color = role in ROLE_SUFFIXES and role not in NON_COLOR_ROLES
    image.colorspace_settings.name = "sRGB" if color else "Non-Color"
    emission = tree.nodes.new("ShaderNodeEmission")
    target = tree.nodes.new("ShaderNodeTexImage")
    target.image = image
    active = tree.nodes.active
    try:
        tree.links.new(socket, emission.inputs["Color"])
        tree.links.new(emission.outputs["Emission"], surface)
        tree.nodes.active = target
        if "FINISHED" not in bpy.ops.object.bake(type="EMIT", margin=BAKE_MARGIN, use_clear=True):
            raise ValueError(f"{material.name}: baking {role} failed")
    except Exception:
        bpy.data.images.remove(image)
        raise
    finally:
        tree.nodes.remove(emission)
        tree.nodes.remove(target)
        for source in previous:
            tree.links.new(source, surface)
        tree.nodes.active = active
    return image


def _unswap(swaps: list[tuple[bpy.types.Node, bpy.types.Image]]) -> None:
    """Put the original images back and drop the baked ones."""
    for node, original in reversed(swaps):
        baked = node.image
        node.image = original
        if baked is not original and baked.users == 0:
            bpy.data.images.remove(baked)
    swaps.clear()


def _export_clips(
    takes: dict[str, bpy.types.Action], written: list[str]
) -> list[unity.ClipAnimation]:
    """Unity clip settings per exported take: range from the action (FBX takes start at 0),
    looping from the action's Cyclic Animation flag."""
    clips = []
    for take in written:
        action = takes.get(take)
        if action is None:
            raise ValueError(f"FBX take {take} has no matching Blender action")
        start, end = action.frame_range
        clips.append(
            _root_motion(
                unity.ClipAnimation(
                    name=take.split("|")[-1],
                    take_name=take,
                    first_frame=0.0,
                    last_frame=end - start,
                    wrap_mode=unity.WRAP_LOOP if action.use_cyclic else unity.WRAP_DEFAULT,
                    loop=action.use_cyclic,
                    loop_time=action.use_cyclic,
                ),
                action,
            )
        )
    return clips


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
    """Write FBX (plus clip settings in its .meta), named textures, and ATTRIBUTION.md into
    out_dir; returns warnings."""
    if library_root and out_dir.resolve().is_relative_to(library_root.resolve()):
        raise ValueError("Refusing to export into the library project")
    if not objects:
        raise ValueError(f"{asset}: nothing to export (empty collection)")
    out_dir.mkdir(parents=True, exist_ok=True)
    original_paths = {}
    saved_links: list[tuple] = []
    saved_names: list[tuple[bpy.types.Material, str]] = []
    takes: dict[str, bpy.types.Action] = {}
    nla_undo: list = []
    shared_undo: list = []
    shared = None
    cut_clips: list[unity.ClipAnimation] = []
    fbx = out_dir / f"{asset}.fbx"
    texture_guids: dict[tuple, str] = {}
    fbx_names: dict[bpy.types.Material, str] = {}
    names = _export_names(objects)
    swaps: list[tuple[bpy.types.Node, bpy.types.Image]] = []
    bake_notes: list[str] = []
    try:
        swaps, bake_notes = _bake_triplanar(objects)
        for image, name, material, role in _texture_jobs(names, asset):
            tint = _mask_fill_srgb(material) if role == "BaseColor" else None
            dst, guid = _write_export_texture(
                image, out_dir / name, tint_srgb=tint, require_alpha=tint is not None, role=role
            )
            texture_guids[(material, role)] = guid
            original_paths[image] = image.filepath
            image.filepath = str(dst)
            if image.alpha_mode == "NONE" and tint is not None:
                image.alpha_mode = "STRAIGHT"
        saved_links = _foliage_export_links(objects)
        saved_names = _rename_to_source_mats(names)
        fbx_names = {
            s.material: s.material.name for o in objects for s in o.material_slots if s.material
        }
        if shared := _shared_take(objects):
            cut_clips, shared_undo = _push_shared_take(objects, *shared)
        else:
            takes, nla_undo = _push_takes(objects)
        bpy.ops.export_scene.fbx(
            filepath=str(fbx),
            use_selection=True,
            apply_scale_options="FBX_SCALE_ALL",
            axis_forward="-Z",
            axis_up="Y",
            bake_space_transform=True,
            path_mode="RELATIVE",
            embed_textures=False,
            bake_anim_use_all_actions=False,
            bake_anim_use_nla_strips=not shared,  # a shared take: the whole scene timeline
        )
    finally:
        _pop_shared_take(shared_undo)
        _pop_takes(nla_undo)
        _restore_names(saved_names)
        _restore_links(saved_links)
        for image, path in original_paths.items():
            image.filepath = path
        _unswap(swaps)
    if shared:
        if _fbx_take_names(fbx) != [shared[0]]:
            raise ValueError(f"{fbx.name}: expected the single take {shared[0]}")
        clips = cut_clips
    else:
        clips = _export_clips(takes, _fbx_take_names(fbx))
    remaps, notes = _export_materials(out_dir, fbx_names, names, texture_guids, asset, library_root)
    notes = bake_notes + notes
    rig, rig_notes = _rig(objects, asset, clips)
    notes += rig_notes
    if rig is None:  # root motion settings mean nothing without the source rig
        clips = [replace(c, root_motion=()) for c in clips]
    if clips or remaps or rig:
        unity.write_model_meta(fbx.with_name(fbx.name + unity.META_SUFFIX), clips, remaps, rig)
    if values := _unity_values(names):
        (out_dir / f"{asset}.materials.json").write_text(json.dumps(values, indent=2), "utf-8")
    records = records_of(objects)
    (out_dir / licensing.ATTRIBUTION_FILE).write_text(
        licensing.attribution_markdown(asset, records), encoding="utf-8"
    )
    if not records:
        return [*notes, f"{asset}: no license record ({licensing.UNKNOWN})"]
    return [
        *notes,
        *(f"{r.pack}: {r.license} (check terms)" for r in licensing.restrictive(records)),
    ]
