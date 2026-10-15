"""Operators wiring the UI to the library, Unity, and io modules."""

import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import io, library, licensing, unity

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
            result = io.import_mesh(context.scene, path, index)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
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


class BAT_OT_export(bpy.types.Operator, ExportHelper):
    """Export selected objects as an asset folder in the target Unity project"""

    bl_idname = "bat.export_asset"
    bl_label = "Export to Unity"
    bl_options = {"UNDO", "PRESET"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})
    overwrite: BoolProperty(name="Overwrite Existing", default=False)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return any(o.type == "MESH" for o in context.selected_objects)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        p = prefs(context)
        if p.recent_targets and context.active_object:
            target = Path(p.recent_targets[p.active_target].path)
            self.filepath = str(target / p.export_root / f"{context.active_object.name}.fbx")
        return ExportHelper.invoke(self, context, event)

    def execute(self, context: bpy.types.Context) -> set[str]:
        asset = Path(self.filepath).stem
        out_dir = Path(self.filepath).parent / asset
        if out_dir.exists() and not self.overwrite:
            self.report({"ERROR"}, f"{out_dir} exists; enable Overwrite Existing to replace it")
            return {"CANCELLED"}
        library_root = Path(bpy.path.abspath(prefs(context).library_path))
        try:
            warnings = io.export_asset(context.selected_objects, out_dir, asset, library_root)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for warning in warnings:
            self.report({"WARNING"}, warning)
        self.report({"INFO"}, f"Exported {asset} to {out_dir}")
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


class BAT_FH_import(bpy.types.FileHandler):
    bl_idname = "BAT_FH_import"
    bl_label = "Import from Library"
    bl_import_operator = BAT_OT_import.bl_idname
    bl_file_extensions = ".fbx;.obj;.blend"

    @classmethod
    def poll_drop(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"
