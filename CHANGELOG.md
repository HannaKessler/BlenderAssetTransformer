# Changelog

## 0.1.0 - Unreleased
- Library browser (Pack > Type > file), filename search, Open in Unity.
- Import into `Pack > Meshes` with Principled BSDF wiring from Unity `.mat`/`.meta`.
- Foliage export: when a texture (e.g. PSD) has no Blender pixels, convert/copy from the library filepath on disk instead of failing.
- Foliage export: convert PSD leaf masks to PNG with verified alpha; copy library texture `.meta` for `alphaIsTransparency`; leave FBX Opacity at 1 (Opaque Lit — no Transparent/glassy cards). True cutout needs ASFolliage remap or a Unity importer.
- Foliage export: tint leaf-mask BaseColor with ASFolliage `_Color0` and name FBX materials after the source `.mat` (Search and Remap).
- Export staging: stage collections (sidebar or Outliner), export to the active target as FBX, named textures, and `ATTRIBUTION.md`.
- Stage library Other files (shaders, etc.) from the browser and copy them into the export under `export_root/<Pack>/…` (with sidecar `.meta`).
- License detection and records on imported objects.
