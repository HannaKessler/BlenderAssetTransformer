"""Blender Asset Transformer: Unity library project -> Blender -> Unity target project."""

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)

from . import library, operators, ui


class BAT_PG_path(bpy.types.PropertyGroup):
    path: StringProperty(name="Path", subtype="DIR_PATH")


class BAT_PG_entry(bpy.types.PropertyGroup):
    pack: StringProperty()
    type: StringProperty()
    path: StringProperty(subtype="FILE_PATH")


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


classes = (
    BAT_PG_path,
    BAT_PG_entry,
    BAT_AP_preferences,
    ui.BAT_UL_entries,
    ui.BAT_PT_main,
    operators.BAT_OT_open_unity,
    operators.BAT_OT_scan_library,
    operators.BAT_OT_add_target,
    operators.BAT_OT_import,
    operators.BAT_OT_import_all,
    operators.BAT_OT_cancel_batch,
    operators.BAT_OT_import_material,
    operators.BAT_OT_export,
    operators.BAT_OT_join,
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


def unregister() -> None:
    operators.clear_progress()
    wm = bpy.types.WindowManager
    for name in ("bat_type", "bat_pack", "bat_entry_index", "bat_entries"):
        delattr(wm, name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
