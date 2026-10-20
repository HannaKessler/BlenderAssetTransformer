# Blender Asset Transformer

Blender 5.1+ extension that imports assets from a Unity library project, lets you rework them in
Blender, and exports them into a Unity 6 project.

## Install
Download the release zip, then in Blender: Edit > Preferences > Get Extensions > Install from Disk.

## Usage
1. In the add-on preferences, set the library project (and the Unity editors folder if not the
   Unity Hub default).
2. In the 3D Viewport sidebar (Asset Transformer tab), click Scan Library, pick a pack and type,
   search by name, and import a mesh. It lands in a `Pack > Meshes` collection with textures wired.
3. Add a target Unity project. Select a collection in the Outliner, then Add to Export (sidebar
   button or Outliner right-click). Export Staged writes each staged collection to
   `<target>/Assets/BlenderAssetTransformer/<Collection>/` with the FBX, named textures, and
   `ATTRIBUTION.md`. Animated FBX also get a `.meta` with each clip's frame range and Loop Time
   (from the action's Cyclic Animation). Clips that came from one take moving several objects
   are exported back as that take, cut into the same named clips. Materials are written to `Materials/` and remapped:
   when the pack's shader works in URP it is copied to `Shaders/` and used, otherwise URP Lit
   is used and the export message says so. On re-export, materials keep your Unity edits (only
   texture references are refreshed) and GUIDs stay the same. Unity can batch-import the whole
   tree.

Use Join (Keep Licenses) instead of Ctrl+J so license records are merged.
The library project is never modified.

## License caveat
The add-on is GPL-3.0-or-later. It records the license information it finds in each pack, but it
is not legal advice: check each pack's terms (for example the Unity Asset Store EULA) before
redistributing anything you export.
