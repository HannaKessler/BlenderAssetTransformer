"""Sidebar panels and the library file list."""

import bpy

from . import bind, library, looks, operators, tiling


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


class BAT_UL_targets(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _prop) -> None:
        layout.label(text=item.path or "(empty)", icon="FILE_FOLDER")


class BAT_UL_staged(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _prop) -> None:
        row = layout.row(align=True)
        if item.kind == "FILE":
            row.label(text=item.name, icon="FILE")
            row.label(text=item.pack)
        else:
            row.label(text=item.name, icon="OUTLINER_COLLECTION")
            row.label(text=str(item.object_count))


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
        elif wm.bat_type == library.ANIMATIONS and 0 <= wm.bat_entry_index < len(entries):
            entry = entries[wm.bat_entry_index]
            if entry.pack == wm.bat_pack and entry.type == library.ANIMATIONS:
                if entry.name.lower().endswith(".fbx"):
                    op = box.operator("bat.import_mesh", text=f"Import {entry.name}")
                    op.filepath, op.animation = entry.path, True
        elif wm.bat_type == library.MATERIALS and 0 <= wm.bat_entry_index < len(entries):
            entry = entries[wm.bat_entry_index]
            if entry.pack == wm.bat_pack and entry.type == library.MATERIALS:
                box.operator(
                    "bat.import_material", text=f"Import {entry.name}"
                ).filepath = entry.path
        elif wm.bat_type == library.OTHER and 0 <= wm.bat_entry_index < len(entries):
            entry = entries[wm.bat_entry_index]
            if entry.pack == wm.bat_pack and entry.type == library.OTHER:
                op = box.operator("bat.stage_file", text="Add to Export", icon="EXPORT")
                op.filepath = entry.path
                op.pack = entry.pack

        box = layout.box()
        box.label(text="Export Staging")
        box.template_list("BAT_UL_staged", "", wm, "bat_staged", wm, "bat_staged_index", rows=3)
        row = box.row(align=True)
        row.operator("bat.stage_collection", icon="ADD", text="Add to Export")
        row.operator("bat.unstage_collection", icon="REMOVE", text="")
        row.operator("bat.clear_staged", icon="TRASH", text="")
        box.operator("bat.join", icon="AUTOMERGE_ON")

        box = layout.box()
        box.label(text="Target Projects")
        box.template_list(
            "BAT_UL_targets", "", prefs, "recent_targets", prefs, "active_target", rows=2
        )
        box.operator("bat.add_target", icon="ADD")
        box.prop(prefs, "export_root")

        row = layout.row()
        row.scale_y = 1.4
        row.operator("bat.export_staged", icon="EXPORT", depress=True)


class BAT_UL_bind_meshes(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _prop) -> None:
        layout.label(text=item.object.name if item.object else "(missing)", icon="MESH_DATA")


class BAT_UL_excluded(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _prop) -> None:
        layout.label(text=item.name, icon="BONE_DATA")


class KitbashPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Kitbash"


class BAT_PT_bind(KitbashPanel, bpy.types.Panel):
    bl_label = "Bind to Rig"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        layout.template_list(
            "BAT_UL_bind_meshes", "", scene, "bat_bind_meshes", scene, "bat_bind_mesh_index", rows=4
        )
        row = layout.row(align=True)
        row.operator("bat.bind_use_selected", icon="ADD")
        row.operator("bat.bind_remove_mesh", icon="REMOVE", text="")
        if meshes := bind.targets(scene):
            layout.label(text=f"Excluded on all {len(meshes)} meshes")
            layout.template_list(
                "BAT_UL_excluded",
                "",
                meshes[0].data,
                "bat_excluded_bones",
                context.window_manager,
                "bat_excluded_index",
                rows=4,
            )
        row = layout.row(align=True)
        row.operator("bat.exclude_bones", icon="REMOVE")
        row.operator("bat.include_bones", icon="ADD")
        layout.operator("bat.bind_to_rig", icon="ARMATURE_DATA")


def _draw_scope(layout: bpy.types.UILayout, wm: bpy.types.WindowManager) -> None:
    row = layout.row(align=True)
    row.prop(wm, "bat_scope", expand=True)
    if wm.bat_scope == "GROUP":
        layout.prop(wm, "bat_scope_group")


class BAT_PT_surface(KitbashPanel, bpy.types.Panel):
    bl_label = "Unify Surface"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        wm = context.window_manager
        layout.prop(wm, "bat_tiling_material")
        layout.prop(wm, "bat_surface_slot")
        _draw_scope(layout, wm)
        layout.prop(wm, "bat_tile_density")
        if wm.bat_tile_density == tiling.MANUAL:
            layout.prop(wm, "bat_tile_size")
        layout.prop(wm, "bat_unwrap_method")
        row = layout.row(align=True)
        row.operator("bat.assign_tiling")
        row.operator("bat.retile")


def _draw_look_rows(layout: bpy.types.UILayout, wm: bpy.types.WindowManager, section: str) -> None:
    props = (
        ("bat_foliage_palette", "bat_foliage_textures")
        if section == looks.FOLIAGE
        else ("bat_surface_palette", "bat_surface_textures")
    )
    for prop, store in zip(props, (looks.PALETTES, looks.TEXTURE_SETS), strict=True):
        row = layout.row(align=True)
        row.prop(wm, prop)
        remove = row.operator("bat.remove_look", text="", icon="X")
        remove.section, remove.store = section, store
    row = layout.row(align=True)
    row.operator("bat.save_palette").section = section
    row.operator("bat.save_texture_set").section = section
    layout.operator("bat.apply_look", icon="COLOR").section = section


class BAT_PT_look(KitbashPanel, bpy.types.Panel):
    bl_label = "Apply Look"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        wm = context.window_manager
        if message := looks.error():
            layout.label(text=message, icon="ERROR")
        layout.prop(wm, "bat_look_name")
        _draw_scope(layout, wm)
        box = layout.box()
        box.label(text="Surface")
        box.prop(wm, "bat_surface_category")
        box.prop(wm, "bat_surface_slot")
        _draw_look_rows(box, wm, operators.SURFACE)
        box = layout.box()
        box.label(text="Foliage")
        box.prop(wm, "bat_foliage_slot")
        _draw_look_rows(box, wm, looks.FOLIAGE)
