"""Bind a mesh to an imported armature and weight it."""

import json

import bpy
from mathutils import Matrix

from . import licensing

SAVED_COLOR = "bat_saved_bone_color"  # JSON of the bone color before the exclusion tint

# Orange, against Blender's blue bone theme, in pose mode and object mode.
EXCLUDED_NORMAL = (0.9, 0.22, 0.05)
EXCLUDED_SELECT = (1.0, 0.48, 0.12)
EXCLUDED_ACTIVE = (1.0, 0.7, 0.3)

_SCALE_EPSILON = 1e-5
_POSE_EPSILON = 1e-4


def targets(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """Meshes on the scene's Bind to Rig list, skipping any that were deleted."""
    return [item.object for item in scene.bat_bind_meshes if item.object]


def remember_meshes(
    collection: bpy.types.bpy_prop_collection, meshes: list[bpy.types.Object]
) -> list[str]:
    """Add meshes to the bind set. A mesh added later copies exclusions already on the set."""
    template = (
        excluded_names(collection[0].object) if len(collection) and collection[0].object else []
    )
    added = []
    for mesh in meshes:
        if mesh.type != "MESH" or any(item.object == mesh for item in collection):
            continue
        collection.add().object = mesh
        for name in template:
            _add_excluded(mesh, name)
        added.append(mesh.name)
    return added


def forget_mesh(collection: bpy.types.bpy_prop_collection, index: int) -> str:
    """Remove the bind-set entry at `index`. Returns its name."""
    if index < 0 or index >= len(collection):
        raise ValueError("No mesh is highlighted")
    obj = collection[index].object
    name = obj.name if obj else "(missing)"
    collection.remove(index)
    return name


def excluded_names(mesh: bpy.types.Object) -> list[str]:
    """Bone names this mesh must not be weighted to, in list order."""
    return [item.name for item in mesh.data.bat_excluded_bones]


def exclude_bones(
    mesh: bpy.types.Object, armature: bpy.types.Object, names: list[str]
) -> tuple[list[str], list[str]]:
    """Add `names` to the mesh's excluded bones. Returns names added and names already listed."""
    if mesh.type != "MESH":
        raise ValueError(f"{mesh.name}: not a mesh")
    if armature.type != "ARMATURE":
        raise ValueError(f"{armature.name}: not an armature")
    missing = [name for name in names if armature.data.bones.get(name) is None]
    if missing:
        raise ValueError(f"{armature.name}: no bone {_listed(missing)}")
    listed = set(excluded_names(mesh))
    added: list[str] = []
    already: list[str] = []
    for name in names:
        if name in listed or name in added:
            already.append(name)
            continue
        _add_excluded(mesh, name)
        added.append(name)
    refresh_exclusion_colors(mesh, armature)
    return added, already


def exclude_meshes(
    meshes: list[bpy.types.Object], armature: bpy.types.Object, names: list[str]
) -> tuple[list[str], list[str]]:
    """Add `names` to every mesh. Returns names added to any mesh, and names already listed."""
    if not meshes:
        raise ValueError("Choose the meshes to bind")
    added: list[str] = []
    already: list[str] = []
    for mesh in meshes:
        new, old = exclude_bones(mesh, armature, names)
        for name in new:
            if name not in added:
                added.append(name)
        for name in old:
            if name not in added and name not in already:
                already.append(name)
    return added, already


def include_meshes(meshes: list[bpy.types.Object], index: int) -> str:
    """Remove the highlighted bone from every mesh. Returns its name."""
    if not meshes:
        raise ValueError("Choose the meshes to bind")
    names = excluded_names(meshes[0])
    if index < 0 or index >= len(names):
        raise ValueError(f"{meshes[0].name}: no excluded bone is highlighted")
    name = names[index]
    for mesh in meshes:
        _remove_excluded(mesh, name)
    refresh_exclusion_colors(meshes[0], None)
    return name


def refresh_exclusion_colors(mesh: bpy.types.Object, armature: bpy.types.Object | None) -> None:
    """Tint `mesh`'s excluded bones on `armature` and restore any other tint we applied."""
    names = set(excluded_names(mesh))
    if armature is None and mesh.parent and mesh.parent.type == "ARMATURE":
        armature = mesh.parent
    marked = [obj for obj in bpy.data.objects if obj.type == "ARMATURE" and _marked(obj)]
    if armature is None and len(marked) == 1:
        armature = marked[0]
    elif armature is None:
        clear_exclusion_colors()
        return
    for obj in marked:
        if obj != armature:
            apply_excluded_colors(obj, set())
    apply_excluded_colors(armature, names)


def apply_excluded_colors(armature: bpy.types.Object, names: set[str]) -> None:
    """Tint `names` on this armature. Restore every other bone whose color we saved."""
    for bone in armature.data.bones:
        _sync_color(bone, bone.color, bone.name in names)
    for pose_bone in armature.pose.bones:
        _sync_color(pose_bone, pose_bone.color, pose_bone.name in names)


def bind_meshes(
    context: bpy.types.Context, armature: bpy.types.Object, meshes: list[bpy.types.Object]
) -> list[str]:
    """Weight each mesh to `armature`, skipping that mesh's excluded bones.

    Raises ValueError, having changed nothing, when a check fails.
    """
    if armature.type != "ARMATURE":
        raise ValueError(f"{armature.name}: not an armature")
    if not meshes:
        raise ValueError("Choose the meshes to bind")
    context.view_layer.update()
    _require_rest(armature)
    for mesh in meshes:
        _require_mesh(armature, mesh)
    active = context.view_layer.objects.active
    selected = [obj for obj in context.view_layer.objects if obj.select_get()]
    notes: list[str] = []
    try:
        for mesh in meshes:
            notes.append(_bind_one(context, armature, mesh))
    finally:
        _restore_selection(context, active, selected)
    return notes


def _add_excluded(mesh: bpy.types.Object, name: str) -> None:
    if name not in excluded_names(mesh):
        mesh.data.bat_excluded_bones.add().name = name


def _remove_excluded(mesh: bpy.types.Object, name: str) -> None:
    bones = mesh.data.bat_excluded_bones
    for index, item in enumerate(bones):
        if item.name == name:
            bones.remove(index)
            return


def _sync_color(owner: bpy.types.bpy_struct, color: bpy.types.BoneColor, excluded: bool) -> None:
    if excluded:
        _save_color(owner, color)
        color.palette = "CUSTOM"
        color.custom.normal = EXCLUDED_NORMAL
        color.custom.select = EXCLUDED_SELECT
        color.custom.active = EXCLUDED_ACTIVE
        return
    if SAVED_COLOR in owner:
        _restore_color(owner, color)


def _save_color(owner: bpy.types.bpy_struct, color: bpy.types.BoneColor) -> None:
    if SAVED_COLOR in owner:
        return
    owner[SAVED_COLOR] = json.dumps(
        {
            "palette": color.palette,
            "normal": list(color.custom.normal)[:3],
            "select": list(color.custom.select)[:3],
            "active": list(color.custom.active)[:3],
        }
    )


def _restore_color(owner: bpy.types.bpy_struct, color: bpy.types.BoneColor) -> None:
    saved = json.loads(owner[SAVED_COLOR])
    color.palette = saved["palette"]
    if saved["palette"] == "CUSTOM":
        color.custom.normal = saved["normal"]
        color.custom.select = saved["select"]
        color.custom.active = saved["active"]
    del owner[SAVED_COLOR]


def _marked(armature: bpy.types.Object) -> bool:
    return any(SAVED_COLOR in bone for bone in armature.data.bones) or any(
        SAVED_COLOR in pose_bone for pose_bone in armature.pose.bones
    )


def clear_exclusion_colors() -> None:
    """Restore every bone color this add-on tinted."""
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and _marked(obj):
            apply_excluded_colors(obj, set())


def _listed(names: list[str]) -> str:
    if len(names) <= 5:
        return ", ".join(names)
    return f"{', '.join(names[:5])}, and {len(names) - 5} more"


def _require_rest(armature: bpy.types.Object) -> None:
    identity = Matrix.Identity(4)
    posed = [
        bone.name
        for bone in armature.pose.bones
        if any(
            abs(bone.matrix_basis[row][col] - identity[row][col]) > _POSE_EPSILON
            for row in range(4)
            for col in range(4)
        )
    ]
    if posed:
        raise ValueError(f"{armature.name}: posed bones: {_listed(posed)}")


def _require_mesh(armature: bpy.types.Object, mesh: bpy.types.Object) -> None:
    if mesh.type != "MESH":
        raise ValueError(f"{mesh.name}: not a mesh")
    if not mesh.data.vertices:
        raise ValueError(f"{mesh.name}: mesh has no vertices")
    if mesh.data.users > 1:
        raise ValueError(f"{mesh.name}: mesh data is shared; make it single-user first")
    scale = mesh.scale
    uneven = abs(scale[0] - scale[1]) > _SCALE_EPSILON or abs(scale[1] - scale[2]) > _SCALE_EPSILON
    if min(scale) <= 0 or uneven:
        raise ValueError(f"{mesh.name}: object scale must be uniform and positive")
    if mesh.parent not in (None, armature):
        raise ValueError(f"{mesh.name}: parented to {mesh.parent.name}")
    missing = [name for name in excluded_names(mesh) if armature.data.bones.get(name) is None]
    if missing:
        raise ValueError(f"{mesh.name}: excluded bones not on {armature.name}: {_listed(missing)}")
    deform = [bone.name for bone in armature.data.bones if bone.use_deform]
    if not deform:
        raise ValueError(f"{armature.name}: no deform bones")
    if not any(name not in set(excluded_names(mesh)) for name in deform):
        raise ValueError(f"{mesh.name}: every deform bone is excluded")
    own = 0
    for modifier in mesh.modifiers:
        if modifier.type != "ARMATURE":
            continue
        if modifier.object != armature:
            other = modifier.object.name if modifier.object else "another armature"
            raise ValueError(f"{mesh.name}: already deformed by {other}")
        own += 1
    if own > 1:
        raise ValueError(f"{mesh.name}: more than one Armature deform for {armature.name}")


def _bind_one(
    context: bpy.types.Context, armature: bpy.types.Object, mesh: bpy.types.Object
) -> str:
    excluded = excluded_names(mesh)
    saved = {bone.name: bone.use_deform for bone in armature.data.bones}
    try:
        for bone in armature.data.bones:
            if bone.name in excluded:
                bone.use_deform = False
        _automatic_weights(context, armature, mesh)
    finally:
        for bone in armature.data.bones:
            bone.use_deform = saved[bone.name]
    _drop_groups(mesh, set(excluded))
    _merge_license(armature, mesh)
    if excluded:
        return f"{mesh.name}: bound to {armature.name}, excluded {_listed(excluded)}"
    return f"{mesh.name}: bound to {armature.name}"


def _automatic_weights(
    context: bpy.types.Context, armature: bpy.types.Object, mesh: bpy.types.Object
) -> None:
    for obj in context.view_layer.objects:
        obj.select_set(False)
    mesh.select_set(True)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    result = bpy.ops.object.parent_set(type="ARMATURE_AUTO", keep_transform=True)
    if "FINISHED" not in result:
        raise ValueError(f"{mesh.name}: automatic weights failed")
    matched = [
        modifier
        for modifier in mesh.modifiers
        if modifier.type == "ARMATURE" and modifier.object == armature
    ]
    if len(matched) != 1:
        raise ValueError(f"{mesh.name}: expected one Armature deform for {armature.name}")


def _drop_groups(mesh: bpy.types.Object, names: set[str]) -> None:
    groups = mesh.vertex_groups
    for name in names:
        group = groups.get(name)
        if group is not None:
            groups.remove(group)


def _merge_license(armature: bpy.types.Object, mesh: bpy.types.Object) -> None:
    records = licensing.merge(
        licensing.loads(mesh.get(licensing.PROPERTY, "")),
        licensing.loads(armature.get(licensing.PROPERTY, "")),
    )
    if records:
        mesh[licensing.PROPERTY] = licensing.dumps(records)


def _restore_selection(
    context: bpy.types.Context,
    active: bpy.types.Object | None,
    selected: list[bpy.types.Object],
) -> None:
    for obj in context.view_layer.objects:
        obj.select_set(False)
    for obj in selected:
        obj.select_set(True)
    context.view_layer.objects.active = active
