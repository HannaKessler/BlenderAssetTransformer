"""Blender Asset Transformer: Unity library project -> Blender -> Unity target project."""

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import library, looks, operators, tiling, ui


class BAT_PG_path(bpy.types.PropertyGroup):
    path: StringProperty(name="Path", subtype="DIR_PATH")


class BAT_PG_entry(bpy.types.PropertyGroup):
    pack: StringProperty()
    type: StringProperty()
    path: StringProperty(subtype="FILE_PATH")


class BAT_PG_staged(bpy.types.PropertyGroup):
    kind: EnumProperty(
        name="Kind",
        items=(
            ("COLLECTION", "Collection", "Blender collection to export as FBX"),
            ("FILE", "File", "Library file to copy into the export"),
        ),
        default="COLLECTION",
    )
    object_count: IntProperty(name="Objects")
    path: StringProperty(name="Path", subtype="FILE_PATH")
    pack: StringProperty(name="Pack")


class BAT_PG_bone_name(bpy.types.PropertyGroup):
    """A bone name; `name` is the bone."""


def _is_mesh(_self: object, obj: bpy.types.Object) -> bool:
    return obj.type == "MESH"


class BAT_PG_bind_mesh(bpy.types.PropertyGroup):
    object: PointerProperty(name="Mesh", type=bpy.types.Object, poll=_is_mesh)


class BAT_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    library_path: StringProperty(name="Library Project", subtype="DIR_PATH")
    unity_editors_path: StringProperty(
        name="Unity Editors Folder",
        description="Folder with one subfolder per Unity version; blank = Unity Hub default",
        subtype="DIR_PATH",
    )
    export_root: StringProperty(name="Export Root", default="Assets/BlenderAssetTransformer")
    recent_targets: CollectionProperty(type=BAT_PG_path)
    active_target: IntProperty()

    def draw(self, _context: bpy.types.Context) -> None:
        for name in ("library_path", "unity_editors_path", "export_root"):
            self.layout.prop(self, name)


SCOPE_ITEMS = [
    ("SLOT", "Slot", "Every face of the slot"),
    ("SELECTED", "Selected", "The slot's faces selected in Edit Mode"),
    ("GROUP", "Group", "The slot's faces whose vertices are all in a vertex group"),
]
METHOD_ITEMS = [
    (
        tiling.TREE,
        "Tree",
        "Trunks and branches seamed on the side facing the trunk; other pieces cut underneath",
    ),
    (tiling.BOX, "Standard Box", "One island per group of faces facing the same world axis"),
    (
        tiling.TRIPLANAR,
        "Standard Triplanar",
        "Projected from three axes and blended, no seams; baked onto Box UVs on export",
    ),
]
DENSITY_ITEMS = [
    (tiling.MATCH, "Match original UVs", "Repeat the texture about as often as the current UVs do"),
    (tiling.MANUAL, "Manual", "Repeat the texture once per Tile Size"),
]

classes = (
    BAT_PG_path,
    BAT_PG_entry,
    BAT_PG_staged,
    BAT_PG_bone_name,
    BAT_PG_bind_mesh,
    BAT_AP_preferences,
    ui.BAT_UL_entries,
    ui.BAT_UL_targets,
    ui.BAT_UL_staged,
    ui.BAT_UL_bind_meshes,
    ui.BAT_UL_excluded,
    ui.BAT_PT_main,
    ui.BAT_PT_bind,
    ui.BAT_PT_surface,
    ui.BAT_PT_look,
    operators.BAT_OT_open_unity,
    operators.BAT_OT_scan_library,
    operators.BAT_OT_add_target,
    operators.BAT_OT_import,
    operators.BAT_OT_import_all,
    operators.BAT_OT_cancel_batch,
    operators.BAT_OT_import_material,
    operators.BAT_OT_stage,
    operators.BAT_OT_stage_file,
    operators.BAT_OT_unstage,
    operators.BAT_OT_clear_staged,
    operators.BAT_OT_export_staged,
    operators.BAT_OT_join,
    operators.BAT_OT_bind_use_selected,
    operators.BAT_OT_bind_remove_mesh,
    operators.BAT_OT_exclude_bones,
    operators.BAT_OT_include_bones,
    operators.BAT_OT_bind,
    operators.BAT_OT_assign_tiling,
    operators.BAT_OT_retile,
    operators.BAT_OT_save_palette,
    operators.BAT_OT_save_texture_set,
    operators.BAT_OT_remove_look,
    operators.BAT_OT_apply_look,
    operators.BAT_FH_import,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.bat_entries = CollectionProperty(type=BAT_PG_entry)
    wm.bat_entry_index = IntProperty()
    wm.bat_pack = EnumProperty(name="Pack", items=operators.pack_items)
    wm.bat_type = EnumProperty(name="Type", items=[(t, t, "") for t in library.TYPES])
    wm.bat_staged = CollectionProperty(type=BAT_PG_staged)
    wm.bat_staged_index = IntProperty()
    bpy.types.Mesh.bat_excluded_bones = CollectionProperty(type=BAT_PG_bone_name)
    bpy.types.Scene.bat_bind_meshes = CollectionProperty(type=BAT_PG_bind_mesh)
    bpy.types.Scene.bat_bind_mesh_index = IntProperty()
    wm.bat_excluded_index = IntProperty()
    wm.bat_surface_slot = EnumProperty(name="Slot", items=operators.slot_items)
    wm.bat_foliage_slot = EnumProperty(name="Foliage Slot", items=operators.slot_items)
    wm.bat_scope = EnumProperty(name="Scope", items=SCOPE_ITEMS)
    wm.bat_scope_group = EnumProperty(name="Vertex Group", items=operators.group_items)
    wm.bat_tiling_material = PointerProperty(name="Tiling Material", type=bpy.types.Material)
    wm.bat_tile_size = FloatProperty(name="Tile Size", default=1.0, min=0.001, unit="LENGTH")
    wm.bat_unwrap_method = EnumProperty(name="Unwrap", items=METHOD_ITEMS)
    wm.bat_tile_density = EnumProperty(name="Density", items=DENSITY_ITEMS)
    wm.bat_look_name = StringProperty(name="Name")
    wm.bat_surface_category = StringProperty(name="Category", default="bark")
    wm.bat_surface_palette = EnumProperty(name="Palette", items=operators.surface_palette_items)
    wm.bat_surface_textures = EnumProperty(
        name="Texture Set", items=operators.surface_texture_items
    )
    wm.bat_foliage_palette = EnumProperty(name="Palette", items=operators.foliage_palette_items)
    wm.bat_foliage_textures = EnumProperty(
        name="Texture Set", items=operators.foliage_texture_items
    )
    looks.load()
    bpy.types.OUTLINER_MT_collection.append(operators.draw_outliner_collection_menu)


def unregister() -> None:
    operators.clear_progress()
    bpy.types.OUTLINER_MT_collection.remove(operators.draw_outliner_collection_menu)
    for name in ("bat_bind_mesh_index", "bat_bind_meshes"):
        delattr(bpy.types.Scene, name)
    del bpy.types.Mesh.bat_excluded_bones
    wm = bpy.types.WindowManager
    for name in (
        "bat_foliage_textures",
        "bat_foliage_palette",
        "bat_surface_textures",
        "bat_surface_palette",
        "bat_surface_category",
        "bat_look_name",
        "bat_tile_density",
        "bat_unwrap_method",
        "bat_tile_size",
        "bat_tiling_material",
        "bat_scope_group",
        "bat_scope",
        "bat_foliage_slot",
        "bat_surface_slot",
        "bat_excluded_index",
        "bat_staged_index",
        "bat_staged",
        "bat_type",
        "bat_pack",
        "bat_entry_index",
        "bat_entries",
    ):
        delattr(wm, name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
