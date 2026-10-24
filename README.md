# Blender Asset Transformer

Blender 5.1+ extension that imports assets from a Unity library project, lets you rework them in
Blender, and exports them into a Unity 6 project.

## Install
Download the release zip, then in Blender: Edit > Preferences > Get Extensions > Install from Disk.

The add-on has two parts, each in its own tab of the 3D Viewport sidebar:

- **Import/export pipeline** (Asset Transformer tab): brings assets in from a Unity library project
  and writes them out to a Unity project.
- **Kitbash toolkit** (Kitbash tab): Bind to Rig, Unify Surface and Apply Look, for reworking what you
  imported before you export it.

## Import/export pipeline
1. In the add-on preferences, set the library project (and the Unity editors folder if not the
   Unity Hub default).
2. In the Asset Transformer tab, click Scan Library, pick a pack and type, search by name, and
   import a mesh. It lands in a `Pack > Meshes` collection with textures wired.
3. Add a target Unity project. Select a collection in the Outliner, then Add to Export (sidebar
   button or Outliner right-click). Export Staged writes each staged collection to
   `<target>/Assets/BlenderAssetTransformer/<Collection>/` with the FBX, named textures, and
   `ATTRIBUTION.md`. Animated FBX also get a `.meta` with each clip's frame range and Loop Time
   (from the action's Cyclic Animation). Clips that came from one take moving several objects
   are exported back as that take, cut into the same named clips. Root motion and rig settings from
   the source model come along when the asset and its clips come from that one model. Materials are written to `Materials/` and remapped:
   when the pack's shader works in URP it is copied to `Shaders/` and used, otherwise URP Lit
   is used and the export message says so. On re-export, materials keep your Unity edits (only
   texture references are refreshed) and GUIDs stay the same. Unity can batch-import the whole
   tree.

Use Join (Keep Licenses) instead of Ctrl+J so license records are merged.
The library project is never modified.

## Kitbash toolkit
- **Bind to Rig** skins meshes to an armature with automatic weights. Select the meshes (a
  character can be several) and click Use Selected; the list is saved with the .blend file. To
  keep bones out of the weights (props, IK targets), select them in Pose Mode and click Exclude:
  they turn orange, still animate, and get no vertex group. Make the armature active and click
  Bind to Rig. A posed bone, a non-uniform or negative scale, or another parent stops the bind
  with a message.
- **Unify Surface** gives bark, rock, ground and similar surfaces one shared tiling material.
  Pick the tiling material and the mesh's slot. Assign Tiling swaps the material and leaves UVs
  alone; Retile rebuilds the UVs with the chosen Unwrap method:
  - Tree: each trunk and branch is seamed along one side from end to end, on a branch the side
    facing the trunk. A piece counts as a tube when it is long and open only at its ends; any
    other piece is cut around its underside, where it meets the ground, or once more along its
    box sides if that leaves it too uneven to lay flat (a tall dome).
  - Standard Box: faces are grouped by the world axis they face most (front, back, left, right,
    top, bottom) into large islands, laid out as seen from that axis so the texture stands
    upright on the sides. Seams run where the groups meet.
  - Standard Triplanar: no seams. The slot gets a copy of the material (named `_Triplanar`)
    that projects its images from three axes on the object and blends them. On export each
    image is baked onto the mesh's Box UVs, which Retile lays out for this, so Unity shows the
    same result with a plain material and no custom shader. The bake keeps the texture's
    detail per meter, up to 4096 px; a large mesh with a small tile size may come out softer.
    Retile each mesh (and LOD) on its own, since a bake fits one mesh.

  Every island gets the same texel density, corrected for the texture's proportions, and is
  never stretched, so a wrap seam may not line up exactly. Density "Match original UVs" (the
  default) measures how often the texture repeats on the faces' current UVs and keeps that;
  "Manual" repeats it once per Tile Size. Match fails with a message when there are no usable
  UVs to measure (say a freshly created UV map); set a Tile Size then. Only the slot's faces (or
  the chosen scope) change.
- **Apply Look** recolors and retextures from saved looks. A look is either a palette (colors
  sampled from a slot's base color) or a texture set (copies of a slot's textures), and you can
  mix any palette with any texture set. Looks are filed by category: the Surface section takes
  any category name (bark, rock, ground...); Foliage is its own section. Type a name, then save a
  palette or a texture set from the active mesh's slot. To apply, pick a palette, a texture set,
  or both, and click Apply. The slot gets a copy of its material, so other meshes using the
  original are unchanged and both versions export side by side:
  - Surface: the texture set's images replace the material's (a role the material has no
    texture for is reported), then the palette recolors the base-color image, keeping the
    texture's pattern. The images land on the mesh's UVs as they are, so a texture set fits
    meshes laid out like the one it came from, or a tiling texture on a surface you retiled.
  - Foliage: only the leaf mask (and normal map) is used; the palette's middle color becomes the
    leaves' material color. On export the mask is the base map, the color goes into the URP Lit
    material, and alpha clipping is on.

  Looks are kept in `looks.json` in your Blender config folder, with texture sets copied to
  `looks/textures/<category>/<name>/` next to it, so they are shared by all your files and never
  point back into the library.

Unify Surface and Apply Look work on a scope: the whole slot, the slot's faces selected in Edit
Mode, or the slot's faces whose vertices are all in a vertex group. A selected or grouped patch
gets its own material slot; applying the same look to another patch of the object reuses it. The
patch border is a hard material edge, with no blending.

## License caveat
The add-on is GPL-3.0-or-later. It records the license information it finds in each pack, but it
is not legal advice: check each pack's terms (for example the Unity Asset Store EULA) before
redistributing anything you export.
