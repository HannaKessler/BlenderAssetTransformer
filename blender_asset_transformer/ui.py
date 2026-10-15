"""Sidebar panels and the library file list."""

import bpy

from . import library, operators


class BAT_UL_entries(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _prop) -> None:
        layout.label(text=item.name)

    def filter_items(self, context: bpy.types.Context, data, prop: str):
        entries = getattr(data, prop)
        wm = context.window_manager
        flags = bpy.types.UI_UL_list.filter_items_by_name(
            self.filter_name, self.bitflag_filter_item, entries, "name"
        ) or [self.bitflag_filter_item] * len(entries)
        for i, entry in enumerate(entries):
            if entry.pack != wm.bat_pack or entry.type != wm.bat_type:
                flags[i] = 0
        return flags, bpy.types.UI_UL_list.sort_items_by_name(entries, "name")


class BAT_PT_main(bpy.types.Panel):
    bl_label = "Asset Transformer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Asset Transformer"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences
        wm = context.window_manager

        box = layout.box()
        box.prop(prefs, "library_path")
        row = box.row()
        if scan := operators._state.get("scan"):
            row.progress(
                factor=scan[0] / max(scan[1], 1), type="BAR", text=f"Scanning {scan[0]}/{scan[1]}"
            )
            row.operator("bat.cancel_batch", icon="CANCEL")
        else:
            row.operator("bat.scan_library", icon="FILE_REFRESH")
            row.operator("bat.open_unity", icon="WINDOW")

        box = layout.box()
        box.prop(wm, "bat_pack", text="Pack")
        box.row().prop(wm, "bat_type", expand=True)
        box.template_list("BAT_UL_entries", "", wm, "bat_entries", wm, "bat_entry_index")
        entries = wm.bat_entries
        if batch := operators._state.get("batch"):
            row = box.row()
            row.progress(
                factor=batch[0] / max(batch[1], 1), type="BAR", text=f"{batch[0]}/{batch[1]}"
            )
            row.operator("bat.cancel_batch", icon="CANCEL")
        elif wm.bat_type == library.MESHES and wm.bat_pack:
            if 0 <= wm.bat_entry_index < len(entries):
                entry = entries[wm.bat_entry_index]
                if entry.pack == wm.bat_pack and entry.type == library.MESHES:
                    box.operator(
                        "bat.import_mesh", text=f"Import {entry.name}"
                    ).filepath = entry.path
            box.operator("bat.import_all", icon="IMPORT").pack = wm.bat_pack
        elif wm.bat_type == library.MATERIALS and 0 <= wm.bat_entry_index < len(entries):
            entry = entries[wm.bat_entry_index]
            if entry.pack == wm.bat_pack and entry.type == library.MATERIALS:
                box.operator(
                    "bat.import_material", text=f"Import {entry.name}"
                ).filepath = entry.path

        box = layout.box()
        box.template_list(
            "UI_UL_list", "bat_targets", prefs, "recent_targets", prefs, "active_target", rows=2
        )
        box.operator("bat.add_target", icon="ADD")
        box.prop(prefs, "export_root")
        row = box.row()
        row.operator("bat.join", icon="AUTOMERGE_ON")
        row.operator("bat.export_asset", icon="EXPORT")
