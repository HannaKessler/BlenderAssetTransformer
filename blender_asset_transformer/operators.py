"""Operators wiring the UI to the library, Unity, and io modules."""

import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import bind, io, library, licensing, looks, tiling, unity

MESH_GLOB = "*.fbx;*.obj;*.blend"
MAX_RECENT_TARGETS = 5
_state: dict = {}  # runtime only: index, pack_items, scan/batch progress, cancel flag


def prefs(context: bpy.types.Context) -> bpy.types.AddonPreferences:
    return context.preferences.addons[__package__].preferences


def current_index() -> library.Index | None:
    return _state.get("index")


def pack_items(_self: object, _context: bpy.types.Context) -> list[tuple[str, str, str]]:
    index = current_index()
    # Blender requires the returned list to stay referenced, hence the cache.
    _state["pack_items"] = [(p, p, "") for p in index.packs()] if index else []
    return _state["pack_items"] or [("", "(scan library)", "")]


def slot_items(_self: object, context: bpy.types.Context) -> list[tuple[str, str, str]]:
    """Material slots on the active mesh."""
    obj = context.active_object if context else None
    slots = obj.material_slots if obj and obj.type == "MESH" else []
    _state["slot_items"] = [
        (str(i), slot.material.name if slot.material else f"Slot {i}", "")
        for i, slot in enumerate(slots)
    ]
    return _state["slot_items"] or [("-1", "(choose a mesh)", "")]


SURFACE = "SURFACE"
NONE_ITEM = " "  # "" would draw as a separator; names are stripped, so none is called " "
SECTIONS = [(SURFACE, "Surface", ""), (looks.FOLIAGE, "Foliage", "")]
STORES = [(looks.PALETTES, "Palette", ""), (looks.TEXTURE_SETS, "Texture Set", "")]


def _surface_category(context: bpy.types.Context | None) -> str:
    return context.window_manager.bat_surface_category.strip().lower() if context else ""


def surface_palette_items(_self: object, context: bpy.types.Context) -> list:
    return _look_items(looks.PALETTES, _surface_category(context), "surface_palettes")


def surface_texture_items(_self: object, context: bpy.types.Context) -> list:
    return _look_items(looks.TEXTURE_SETS, _surface_category(context), "surface_textures")


def foliage_palette_items(_self: object, _context: bpy.types.Context) -> list:
    return _look_items(looks.PALETTES, looks.FOLIAGE, "foliage_palettes")


def foliage_texture_items(_self: object, _context: bpy.types.Context) -> list:
    return _look_items(looks.TEXTURE_SETS, looks.FOLIAGE, "foliage_textures")


def _look_items(store: str, category: str, key: str) -> list[tuple[str, str, str]]:
    _state[key] = [(NONE_ITEM, "(none)", "")] + [(n, n, "") for n in looks.names(store, category)]
    return _state[key]


def library_root(context: bpy.types.Context) -> Path:
    return Path(bpy.path.abspath(prefs(context).library_path))


def scan(context: bpy.types.Context) -> library.Index:
    return publish(context, library.scan(library_root(context)))


def publish(context: bpy.types.Context, index: library.Index) -> library.Index:
    """Make index current and refill the browser list."""
    _state["index"] = index
    items = context.window_manager.bat_entries
    items.clear()
    for entry in index.entries:
        item = items.add()
        item.name = entry.path.name
        item.pack = entry.pack
        item.type = entry.type
        item.path = str(entry.path)
    return index


class BAT_OT_open_unity(bpy.types.Operator):
    """Open the library project in its matching Unity editor"""

    bl_idname = "bat.open_unity"
    bl_label = "Open in Unity"

    def execute(self, context: bpy.types.Context) -> set[str]:
        p = prefs(context)
        editors = Path(bpy.path.abspath(p.unity_editors_path)) if p.unity_editors_path else None
        try:
            exe = unity.launch(Path(bpy.path.abspath(p.library_path)), editors)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Launched {exe}")
        return {"FINISHED"}


def clear_progress() -> None:
    for key in ("batch", "scan", "cancel"):
        _state.pop(key, None)


def redraw(context: bpy.types.Context) -> None:
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


class BAT_OT_scan_library(bpy.types.Operator):
    """Index the library project's Assets folder (Esc or Cancel stops)"""

    bl_idname = "bat.scan_library"
    bl_label = "Scan Library"

    TICK_BUDGET = 0.05  # seconds of scanning per timer tick

    def _report(self, index: library.Index) -> set[str]:
        self.report({"INFO"}, f"{len(index.entries)} files in {len(index.packs())} packs")
        return {"FINISHED"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            return self._report(scan(context))
        except OSError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        root = library_root(context)
        try:
            self.files = library.list_files(root)
        except OSError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.index, self.done = library.Index(root), 0
        clear_progress()
        _state["scan"] = (0, len(self.files))
        self.timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        cancelled = event.type == "ESC" or _state.pop("cancel", False)
        if not cancelled and event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            end = time.monotonic() + self.TICK_BUDGET
            while not cancelled and self.done < len(self.files) and time.monotonic() < end:
                library.add_file(self.index, self.files[self.done])
                self.done += 1
        except OSError as error:
            self.report({"ERROR"}, str(error))
            cancelled = True
        _state["scan"] = (self.done, len(self.files))
        redraw(context)
        if not cancelled and self.done < len(self.files):
            return {"RUNNING_MODAL"}
        context.window_manager.event_timer_remove(self.timer)
        clear_progress()
        redraw(context)
        if cancelled:  # keep the previous index
            self.report({"WARNING"}, "Scan cancelled")
            return {"CANCELLED"}
        return self._report(publish(context, self.index))


class BAT_OT_add_target(bpy.types.Operator):
    """Add a Unity target project"""

    bl_idname = "bat.add_target"
    bl_label = "Add Target Project"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        path = Path(self.directory).resolve()
        if not unity.is_project(path):
            self.report({"ERROR"}, f"Not a Unity project: {path}")
            return {"CANCELLED"}
        targets = prefs(context).recent_targets
        for i in reversed(range(len(targets))):
            if targets[i].path == str(path):
                targets.remove(i)
        targets.add().path = str(path)
        targets.move(len(targets) - 1, 0)
        while len(targets) > MAX_RECENT_TARGETS:
            targets.remove(len(targets) - 1)
        prefs(context).active_target = 0
        return {"FINISHED"}


class BAT_OT_import(bpy.types.Operator, ImportHelper):
    """Import a mesh from the library into Pack > Meshes"""

    bl_idname = "bat.import_mesh"
    bl_label = "Import from Library"
    bl_options = {"UNDO", "PRESET"}

    filter_glob: StringProperty(default=MESH_GLOB, options={"HIDDEN"})
    animation: BoolProperty(options={"HIDDEN"})  # add clips to a matching armature

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if self.filepath:
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context: bpy.types.Context) -> set[str]:
        path = Path(self.filepath).resolve()
        try:
            index = current_index() or scan(context)
            if not path.is_relative_to(index.root.resolve() / "Assets"):
                raise ValueError(f"{path.name} is not inside the library project's Assets")
            if self.animation:
                done, result = io.import_animation(context.scene, path, index)
                self.report({"INFO"}, f"{path.name}: {done}")
            else:
                result = io.import_mesh(context.scene, path, index)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if note := io.unit_note(context.scene):
            self.report({"INFO"}, note)
        for problem in result.unresolved:
            self.report({"WARNING"}, problem)
        for line in result.relinked:
            print(f"[{__package__}] relinked {line}")
        self.report(
            {"INFO"},
            f"Imported {len(result.objects)} objects from {path.name}"
            f" ({len(result.relinked)} textures relinked by name, details in console)",
        )
        return {"FINISHED"}


class BAT_OT_cancel_batch(bpy.types.Operator):
    """Stop the running scan or batch import"""

    bl_idname = "bat.cancel_batch"
    bl_label = "Cancel"

    def execute(self, _context: bpy.types.Context) -> set[str]:
        _state["cancel"] = True
        return {"FINISHED"}


class BAT_OT_import_all(bpy.types.Operator):
    """Import every mesh in the selected pack into Pack > Meshes (Esc stops)"""

    bl_idname = "bat.import_all"
    bl_label = "Import All Meshes"
    bl_options = {"UNDO"}

    pack: StringProperty()

    def _start(self) -> bool:
        self.index = current_index()
        if self.index is None:
            self.report({"ERROR"}, "Scan the library first")
            return False
        entries = self.index.entries
        self.paths = [e.path for e in entries if e.pack == self.pack and e.type == library.MESHES]
        self.done, self.errors, self.warnings, self.relinked = 0, [], [], 0
        return True

    def _step(self, context: bpy.types.Context) -> None:
        path = self.paths[self.done]
        self.done += 1
        try:
            result = io.import_mesh(context.scene, path, self.index)
            self.warnings += [f"{path.name}: {w}" for w in result.unresolved]
            self.relinked += len(result.relinked)
        except Exception as error:  # isolate each file so one failure doesn't stop the batch
            self.errors.append(f"{path.name}: {error}")

    def _finish(self) -> set[str]:
        for line in self.errors + self.warnings:
            print(f"[{__package__}] {line}")
        for error in self.errors:
            self.report({"ERROR"}, error)
        self.report(
            {"WARNING" if self.errors or self.warnings else "INFO"},
            f"Imported {self.done - len(self.errors)}/{len(self.paths)} meshes from {self.pack}, "
            f"{len(self.errors)} failed, {self.relinked} textures relinked, "
            f"{len(self.warnings)} warnings (details in console)",
        )
        return {"FINISHED"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self._start():
            return {"CANCELLED"}
        while self.done < len(self.paths):
            self._step(context)
        return self._finish()

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        if not self._start():
            return {"CANCELLED"}
        wm = context.window_manager
        self.timer = wm.event_timer_add(0.01, window=context.window)
        wm.progress_begin(0, len(self.paths))
        wm.modal_handler_add(self)
        clear_progress()
        _state["batch"] = (0, len(self.paths))
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        # One file per timer tick keeps Blender responsive during long batches.
        if event.type == "ESC" or _state.pop("cancel", False):
            self.paths = self.paths[: self.done]
        elif event.type == "TIMER" and self.done < len(self.paths):
            self._step(context)
            context.window_manager.progress_update(self.done)
        else:
            return {"PASS_THROUGH"}  # keep the UI usable, e.g. the Cancel button
        _state["batch"] = (self.done, len(self.paths))
        redraw(context)
        if self.done < len(self.paths):
            return {"RUNNING_MODAL"}
        context.window_manager.event_timer_remove(self.timer)
        context.window_manager.progress_end()
        clear_progress()
        return self._finish()


class BAT_OT_import_material(bpy.types.Operator):
    """Rewire the scene materials made from this Unity .mat, or create it if none exist"""

    bl_idname = "bat.import_material"
    bl_label = "Import Material"
    bl_options = {"UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            index = current_index() or scan(context)
            materials, reused, missing = io.import_material(Path(self.filepath), index)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for problem in missing:
            self.report({"WARNING"}, problem)
        names = ", ".join(m.name for m in materials)
        self.report({"INFO"}, f"{'Updated' if reused else 'Created'} {names}")
        return {"FINISHED"}


def resolve_collection(context: bpy.types.Context, name: str) -> bpy.types.Collection:
    """Find a collection by name, including the scene master collection."""
    if context.scene.collection.name == name:
        return context.scene.collection
    collection = bpy.data.collections.get(name)
    if collection is None:
        raise ValueError(f"Collection not found: {name}")
    return collection


def active_collection(context: bpy.types.Context) -> bpy.types.Collection:
    return context.view_layer.active_layer_collection.collection


def draw_outliner_collection_menu(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    layout = self.layout
    layout.separator()
    op = layout.operator(BAT_OT_stage.bl_idname, text="Add to Export", icon="EXPORT")
    if context.collection:
        op.collection = context.collection.name


class BAT_OT_stage(bpy.types.Operator):
    """Add the active (or named) collection to the export staging list"""

    bl_idname = "bat.stage_collection"
    bl_label = "Add to Export"
    bl_options = {"UNDO"}

    collection: StringProperty(options={"HIDDEN"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        name = self.collection or active_collection(context).name
        try:
            col = resolve_collection(context, name)
            objects = io.collection_objects(col)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not any(obj.type == "MESH" for obj in objects):
            self.report({"ERROR"}, f"{name}: no mesh objects to export")
            return {"CANCELLED"}
        staged = context.window_manager.bat_staged
        for item in staged:
            if item.kind == "COLLECTION" and item.name == name:
                item.object_count = len(objects)
                self.report({"INFO"}, f"Already staged: {name}")
                return {"FINISHED"}
        item = staged.add()
        item.name = name
        item.kind = "COLLECTION"
        item.object_count = len(objects)
        item.path = ""
        item.pack = ""
        context.window_manager.bat_staged_index = len(staged) - 1
        self.report({"INFO"}, f"Staged {name} ({len(objects)} objects)")
        return {"FINISHED"}


class BAT_OT_stage_file(bpy.types.Operator):
    """Add a library Other file to the export staging list"""

    bl_idname = "bat.stage_file"
    bl_label = "Add to Export"
    bl_options = {"UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    pack: StringProperty()

    def execute(self, context: bpy.types.Context) -> set[str]:
        path = Path(self.filepath)
        if not path.is_file():
            self.report({"ERROR"}, f"File not found: {path}")
            return {"CANCELLED"}
        pack = self.pack
        if not pack:
            self.report({"ERROR"}, "Pack is required to stage a library file")
            return {"CANCELLED"}
        key = str(path.resolve())
        staged = context.window_manager.bat_staged
        for item in staged:
            if item.kind == "FILE" and item.path == key:
                self.report({"INFO"}, f"Already staged: {path.name}")
                return {"FINISHED"}
        item = staged.add()
        item.name = path.name
        item.kind = "FILE"
        item.path = key
        item.pack = pack
        item.object_count = 0
        context.window_manager.bat_staged_index = len(staged) - 1
        self.report({"INFO"}, f"Staged {path.name}")
        return {"FINISHED"}


class BAT_OT_unstage(bpy.types.Operator):
    """Remove the selected item from the export staging list"""

    bl_idname = "bat.unstage_collection"
    bl_label = "Remove from Export"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        wm = context.window_manager
        return bool(wm.bat_staged) and 0 <= wm.bat_staged_index < len(wm.bat_staged)

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        name = wm.bat_staged[wm.bat_staged_index].name
        wm.bat_staged.remove(wm.bat_staged_index)
        wm.bat_staged_index = min(wm.bat_staged_index, max(len(wm.bat_staged) - 1, 0))
        self.report({"INFO"}, f"Removed {name} from staging")
        return {"FINISHED"}


class BAT_OT_clear_staged(bpy.types.Operator):
    """Clear the export staging list"""

    bl_idname = "bat.clear_staged"
    bl_label = "Clear Staging"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return bool(context.window_manager.bat_staged)

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.window_manager.bat_staged.clear()
        context.window_manager.bat_staged_index = 0
        self.report({"INFO"}, "Cleared export staging")
        return {"FINISHED"}


class BAT_OT_export_staged(bpy.types.Operator):
    """Export staged collections and library files into the active target Unity project"""

    bl_idname = "bat.export_staged"
    bl_label = "Export Staged"
    bl_options = {"UNDO"}

    overwrite: BoolProperty(name="Overwrite Existing", default=False)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        p = prefs(context)
        return bool(context.window_manager.bat_staged and p.recent_targets)

    def _plans(
        self, context: bpy.types.Context
    ) -> list[tuple[str, str, Path, list[bpy.types.Object] | Path]]:
        """Each plan is (kind, label, dest, payload) where payload is objects or a source Path."""
        p = prefs(context)
        if not (0 <= p.active_target < len(p.recent_targets)):
            raise ValueError("No active target project selected")
        target = Path(bpy.path.abspath(p.recent_targets[p.active_target].path))
        if not unity.is_project(target):
            raise ValueError(f"Not a Unity project: {target}")
        library_root = Path(bpy.path.abspath(p.library_path))
        plans: list[tuple[str, str, Path, list[bpy.types.Object] | Path]] = []
        for item in context.window_manager.bat_staged:
            if item.kind == "FILE":
                src = Path(item.path)
                if not src.is_file():
                    raise ValueError(f"{item.name}: library file not found: {src}")
                dest = io.library_file_dest(target, p.export_root, item.pack, src, library_root)
                plans.append(("FILE", item.name, dest, src))
                continue
            col = resolve_collection(context, item.name)
            objects = io.collection_objects(col)
            if not any(obj.type == "MESH" for obj in objects):
                raise ValueError(f"{item.name}: no mesh objects to export")
            out_dir = io.export_dir(target, p.export_root, item.name)
            plans.append(("COLLECTION", item.name, out_dir, objects))
        return plans

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        try:
            plans = self._plans(context)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self._existing = [str(dest) for _, _, dest, _ in plans if dest.exists()]
        if self._existing and not self.overwrite:
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, _context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="These export paths already exist:")
        for path in self._existing:
            layout.label(text=path, icon="ERROR")
        layout.prop(self, "overwrite")

    def execute(self, context: bpy.types.Context) -> set[str]:
        library_root = Path(bpy.path.abspath(prefs(context).library_path))
        try:
            plans = self._plans(context)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        blocked = [dest for _, _, dest, _ in plans if dest.exists() and not self.overwrite]
        if blocked:
            self.report(
                {"ERROR"},
                f"{blocked[0]} exists; enable Overwrite Existing to replace it",
            )
            return {"CANCELLED"}
        collections = files = 0
        for kind, label, dest, payload in plans:
            if kind == "FILE":
                try:
                    written = io.copy_library_file(payload, dest, library_root)
                except (OSError, ValueError) as error:
                    self.report({"ERROR"}, str(error))
                    return {"CANCELLED"}
                self.report({"INFO"}, f"Copied {label} to {written[0]}")
                files += 1
                continue
            objects = payload
            for obj in context.view_layer.objects:
                obj.select_set(False)
            missing = [obj.name for obj in objects if obj.name not in context.view_layer.objects]
            if missing:
                self.report({"ERROR"}, f"{label}: not in view layer: {', '.join(missing)}")
                return {"CANCELLED"}
            for obj in objects:
                obj.select_set(True)
            context.view_layer.objects.active = objects[0]
            try:
                warnings = io.export_asset(objects, dest, label, library_root)
            except (OSError, ValueError, RuntimeError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            for warning in warnings:
                self.report({"WARNING"}, warning)
            self.report({"INFO"}, f"Exported {label} to {dest}")
            collections += 1
        parts = []
        if collections:
            parts.append(f"{collections} collection(s)")
        if files:
            parts.append(f"{files} file(s)")
        if note := io.unit_note(context.scene):
            self.report({"INFO"}, note)
        self.report({"INFO"}, f"Exported {', '.join(parts) or 'nothing'}")
        return {"FINISHED"}


class BAT_OT_join(bpy.types.Operator):
    """Join selected meshes into the active one, merging their license records"""

    bl_idname = "bat.join"
    bl_label = "Join (Keep Licenses)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        active = context.active_object
        return bool(active and active.type == "MESH" and len(context.selected_objects) > 1)

    def execute(self, context: bpy.types.Context) -> set[str]:
        records = io.records_of(context.selected_objects)
        context.active_object[licensing.PROPERTY] = licensing.dumps(records)
        return bpy.ops.object.join()


class BAT_OT_bind_use_selected(bpy.types.Operator):
    """Add the selected meshes to the Bind to Rig list"""

    bl_idname = "bat.bind_use_selected"
    bl_label = "Use Selected"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == "OBJECT" and any(
            obj.type == "MESH" for obj in context.selected_objects
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if added := bind.remember_meshes(context.scene.bat_bind_meshes, meshes):
            self.report({"INFO"}, f"Bind list: {', '.join(added)}")
        else:
            self.report({"INFO"}, "Those meshes are already in the bind list")
        return {"FINISHED"}


class BAT_OT_bind_remove_mesh(bpy.types.Operator):
    """Remove the highlighted mesh from the Bind to Rig list"""

    bl_idname = "bat.bind_remove_mesh"
    bl_label = "Remove"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        scene = context.scene
        return 0 <= scene.bat_bind_mesh_index < len(scene.bat_bind_meshes)

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        try:
            name = bind.forget_mesh(scene.bat_bind_meshes, scene.bat_bind_mesh_index)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        last = max(len(scene.bat_bind_meshes) - 1, 0)
        scene.bat_bind_mesh_index = min(scene.bat_bind_mesh_index, last)
        if meshes := bind.targets(scene):
            armature = context.active_object
            bind.refresh_exclusion_colors(
                meshes[0], armature if armature and armature.type == "ARMATURE" else None
            )
        else:
            bind.clear_exclusion_colors()
        self.report({"INFO"}, f"Removed {name} from the bind list")
        return {"FINISHED"}


class BAT_OT_exclude_bones(bpy.types.Operator):
    """Add the selected pose bones to every mesh in the Bind to Rig list"""

    bl_idname = "bat.exclude_bones"
    bl_label = "Exclude"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        return bool(
            bind.targets(context.scene)
            and armature
            and armature.type == "ARMATURE"
            and context.mode == "POSE"
            and context.selected_pose_bones
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        meshes = bind.targets(context.scene)
        names = [bone.name for bone in context.selected_pose_bones]
        try:
            added, already = bind.exclude_meshes(meshes, context.active_object, names)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if added:
            joined = ", ".join(mesh.name for mesh in meshes)
            self.report({"INFO"}, f"Excluded {', '.join(added)} on {joined}")
        if already:
            self.report({"INFO"}, f"Already excluded: {', '.join(already)}")
        return {"FINISHED"}


class BAT_OT_include_bones(bpy.types.Operator):
    """Remove the highlighted bone from every mesh in the Bind to Rig list"""

    bl_idname = "bat.include_bones"
    bl_label = "Include"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        meshes = bind.targets(context.scene)
        index = context.window_manager.bat_excluded_index
        return bool(meshes) and 0 <= index < len(bind.excluded_names(meshes[0]))

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        meshes = bind.targets(context.scene)
        try:
            name = bind.include_meshes(meshes, wm.bat_excluded_index)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        wm.bat_excluded_index = max(
            min(wm.bat_excluded_index, len(bind.excluded_names(meshes[0])) - 1), 0
        )
        self.report({"INFO"}, f"Included {name} on {', '.join(mesh.name for mesh in meshes)}")
        return {"FINISHED"}


class BAT_OT_bind(bpy.types.Operator):
    """Weight every mesh in the Bind to Rig list to the active armature"""

    bl_idname = "bat.bind_to_rig"
    bl_label = "Bind to Rig"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        armature = context.active_object
        return bool(
            armature
            and armature.type == "ARMATURE"
            and context.mode == "OBJECT"
            and bind.targets(context.scene)
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            notes = bind.bind_meshes(context, context.active_object, bind.targets(context.scene))
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for note in notes:
            self.report({"INFO"}, note)
        return {"FINISHED"}


def group_items(_self: object, context: bpy.types.Context) -> list[tuple[str, str, str]]:
    """Vertex groups on the active mesh."""
    obj = context.active_object if context else None
    groups = obj.vertex_groups if obj and obj.type == "MESH" else []
    _state["group_items"] = [(g.name, g.name, "") for g in groups]
    return _state["group_items"] or [(NONE_ITEM, "(no vertex groups)", "")]


def _scoped(context: bpy.types.Context, slot: int) -> list[int] | None:
    """Faces the scope picks on the active mesh, or None for the whole slot. Leaves Edit Mode,
    since face selection and material changes live on the object's mesh."""
    wm = context.window_manager
    if context.mode == "EDIT_MESH":
        bpy.ops.object.mode_set(mode="OBJECT")
    if wm.bat_scope == "SLOT":
        return None
    return io.scoped_faces(context.active_object, slot, wm.bat_scope, wm.bat_scope_group.strip())


class BAT_OT_assign_tiling(bpy.types.Operator):
    """Put the tiling material on the slot (or the scope's faces) without changing UVs"""

    bl_idname = "bat.assign_tiling"
    bl_label = "Assign Tiling"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return bool(obj and obj.type == "MESH" and context.window_manager.bat_tiling_material)

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        slot = int(wm.bat_surface_slot)
        try:
            faces = _scoped(context, slot)
            warnings = tiling.assign_tiling(
                context.active_object, slot, wm.bat_tiling_material, faces
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for warning in warnings:
            self.report({"WARNING"}, warning)
        self.report({"INFO"}, f"Assigned {wm.bat_tiling_material.name}")
        return {"FINISHED"}


class BAT_OT_retile(bpy.types.Operator):
    """Seam, unwrap and scale the slot (or the scope's faces) so the tiling texture repeats at
    one texel density"""

    bl_idname = "bat.retile"
    bl_label = "Retile"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return bool(obj and obj.type == "MESH" and context.window_manager.bat_tiling_material)

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        obj = context.active_object
        slot = int(wm.bat_surface_slot)
        try:
            faces = _scoped(context, slot)
            manual = wm.bat_tile_density == tiling.MANUAL
            count, warnings, tile_size = tiling.retile(
                context,
                obj,
                slot,
                wm.bat_tiling_material,
                wm.bat_tile_size if manual else None,
                wm.bat_unwrap_method,
                faces,
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for warning in warnings:
            self.report({"WARNING"}, warning)
        self.report(
            {"INFO"}, f"Retiled {obj.name}: {count} tube strip(s), one repeat per {tile_size:.3g} m"
        )
        return {"FINISHED"}


def _section(wm: bpy.types.WindowManager, section: str) -> tuple[int, str, str, str]:
    """(slot, category, chosen palette, chosen texture set) of a look section."""
    if section == looks.FOLIAGE:
        return (
            int(wm.bat_foliage_slot),
            looks.FOLIAGE,
            wm.bat_foliage_palette.strip(),
            wm.bat_foliage_textures.strip(),
        )
    category = wm.bat_surface_category.strip().lower()
    if category == looks.FOLIAGE:
        raise ValueError("Foliage has its own section; use another category here")
    palette, texture_set = wm.bat_surface_palette.strip(), wm.bat_surface_textures.strip()
    return int(wm.bat_surface_slot), category, palette, texture_set


def _can_save(context: bpy.types.Context) -> bool:
    obj = context.active_object
    return bool(obj and obj.type == "MESH" and context.window_manager.bat_look_name.strip())


class BAT_OT_save_palette(bpy.types.Operator):
    """Save a palette sampled from the active mesh's slot to the looks file"""

    bl_idname = "bat.save_palette"
    bl_label = "Save Palette"

    section: EnumProperty(items=SECTIONS)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _can_save(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        try:
            slot, category, _, _ = _section(wm, self.section)
            colors = looks.save_palette(context.active_object, slot, category, wm.bat_look_name)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved {category} palette {wm.bat_look_name} ({len(colors)} colors)")
        return {"FINISHED"}


class BAT_OT_save_texture_set(bpy.types.Operator):
    """Copy the active mesh slot's textures into the looks folder as a texture set"""

    bl_idname = "bat.save_texture_set"
    bl_label = "Save Texture Set"

    section: EnumProperty(items=SECTIONS)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _can_save(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        try:
            slot, category, _, _ = _section(wm, self.section)
            roles = looks.save_texture_set(context.active_object, slot, category, wm.bat_look_name)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report(
            {"INFO"}, f"Saved {category} texture set {wm.bat_look_name}: {', '.join(roles)}"
        )
        return {"FINISHED"}


class BAT_OT_remove_look(bpy.types.Operator):
    """Remove the chosen palette or texture set from the looks file"""

    bl_idname = "bat.remove_look"
    bl_label = "Remove"

    section: EnumProperty(items=SECTIONS)
    store: EnumProperty(items=STORES)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            _, category, palette, texture_set = _section(context.window_manager, self.section)
            name = palette if self.store == looks.PALETTES else texture_set
            looks.remove(self.store, category, name)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Removed {category} {name}")
        return {"FINISHED"}


class BAT_OT_apply_look(bpy.types.Operator):
    """Give the slot a copy of its material with the chosen texture set and palette"""

    bl_idname = "bat.apply_look"
    bl_label = "Apply"
    bl_options = {"UNDO"}

    section: EnumProperty(items=SECTIONS)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return bool(obj and obj.type == "MESH")

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            slot, category, palette, texture_set = _section(context.window_manager, self.section)
            faces = _scoped(context, slot)
            notes = looks.apply_look(
                context.active_object, slot, category, palette, texture_set, faces
            )
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for note in notes:
            self.report({"INFO"}, note)
        return {"FINISHED"}


class BAT_FH_import(bpy.types.FileHandler):
    bl_idname = "BAT_FH_import"
    bl_label = "Import from Library"
    bl_import_operator = BAT_OT_import.bl_idname
    bl_file_extensions = ".fbx;.obj;.blend"

    @classmethod
    def poll_drop(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"
