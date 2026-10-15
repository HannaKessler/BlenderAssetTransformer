# BlenderAssetTransformer

A Blender 5.1+ extension that imports assets from a Unity asset library project, restyles them, and exports them into a target Unity project. Open source (GPL-3.0, required for Blender extensions).

## Principles
- Lean: small codebase, no features beyond what's listed here.
- Blender-only: no Unity editor scripts, no `.mat` or `.meta` writing. Shaders are set up in Unity.
- Import one mesh or a whole pack at once (batch import); transformation and review stay one asset at a time.
- Never guess: no content categorization, no guessed licenses.

## Decisions
1. Blender extension (`blender_manifest.toml`, min 5.1), UI in the 3D Viewport sidebar.
2. Sources are Unity Asset Store packs, imported into a dedicated Unity **library project** through Unity. No direct store downloads.
3. Targets the latest Unity LTS (Unity 6).
4. Build order: import/export first, transformation second.
5. Tooling: `ruff` (lint + format) in CI; `fake-bpy-module` for editor typing. No automated tests for now; testing is manual.
6. Blender conventions: one `classes` tuple (register in order, unregister in reverse), `ImportHelper`/`ExportHelper` operators with undo/preset support and a drag-and-drop `FileHandler`, warnings via `self.report`, preferences keyed on `__package__`.
7. Releases: version only in `blender_manifest.toml`, built with `blender --command extension build`, `CHANGELOG.md`, releases from git tags.
8. Out of scope: translations, version-specific code, auto-registration, custom build tools, custom logging.

Full coding rules are in `AGENTS.md`.

## Phase 1: Import/export

**Settings:** library project path, Unity editor path, recent target projects, export root folder (default `Assets/BlenderAssetTransformer`).

**Open in Unity:** button launches the library project with the matching editor version from `ProjectVersion.txt`.

**Browser:** scans the library's `Assets/` and lists `Pack > Type > file`, where type comes only from the extension (dual listing: an `.fbx` with clips and materials on appears under Meshes and Animations; an `.fbx` whose `.meta` has `importAnimation: 1`, defined animation clips, and materials off, i.e. `materialImportMode: 0` or legacy `importMaterials: 0`, is listed under Animations):
- Meshes: `.fbx`, `.obj`, `.blend`
- Materials: `.mat`
- Textures: `.png`, `.jpg`, `.tga`, `.psd`, `.tif`, `.exr`
- Animations: `.anim`
- Other: license, readme, shaders, and anything else (not imported into Blender; can be staged for export copy)

`.prefab` files are ignored. Search by file name.

**Importer:** imports the selected mesh into a `Pack > Meshes` collection, fixes scale and axes, and wires textures into a Principled BSDF using the Unity `.mat` texture slots (resolved through `.meta` GUIDs), falling back to texture names in the same folder. Anything unresolved is reported. Each material records its Unity shader GUID and `.mat` values (`bat_unity_shader`, `bat_unity_values`). Known custom shaders get a Blender-only preview from one shared node group (currently AnimeNaturalEnvironment foliage: height gradient + alpha clip); it is never baked, textures are untouched, and Unity's own `.mat` still defines the final look.

**Exporter:** stages collections and library Other files. Collections write to `<target>/Assets/<root>/<Asset>/` as an FBX (Unity axes and scale, applied transforms, embedded material data, relative texture paths) plus named textures (`<Asset>_BaseColor`, `_Normal`, `_Metallic`, `_Roughness`, `_AO`, `_Emission`). Staged Other files are copied under `<target>/<root>/<Pack>/…` (pack-relative layout, including sidecar `.meta` when present). Warns before overwriting.

**Licensing:** on import, reads license files in the pack and records pack, publisher, license, and attribution on the Blender objects. If none is found, the asset is marked "License unknown". Records are merged when assets are combined. On export, writes `ATTRIBUTION.md` next to the asset and warns about unknown or restrictive licenses. The add-on records license info but isn't legal advice.

**Done when:** an asset goes from a real pack through Blender into a fresh Unity 6 project with textures assigned and attribution included.

## Phase 2: Look transfer
- Pick a source asset (the look) and a target mesh with its own UVs.
- Bake base color, normal, roughness, metallic, and AO onto the target UVs, with a preview before applying.
- Save look settings as presets and apply them to new imports.

## Layout
```
blender_asset_transformer/
  blender_manifest.toml
  __init__.py      # registration, preferences
  ui.py            # sidebar panels
  operators.py     # Blender operators
  unity.py         # project validation, launching, .mat/.meta parsing
  library.py       # scan and index
  io.py            # import and export
  licensing.py
  presets.py       # phase 2
  transfer.py      # phase 2
README.md
LICENSE
CHANGELOG.md
AGENTS.md
pyproject.toml     # ruff config
.github/workflows/ # lint, release
```
