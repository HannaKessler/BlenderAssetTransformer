# Coding rules

Read `PLAN.md` first. Build only what it lists.

## Scope
- Smallest change that does the job. No speculative features, options, or abstractions.
- No new files, classes, or dependencies unless clearly needed. Standard library + `bpy` only.
- Don't refactor unrelated code. Don't leave dead code, TODO stubs, or commented-out code.

## Structure
- Follow the flat layout in `PLAN.md`.
- Keep file parsing (`.mat`/`.meta`, license files) as plain functions; no wrappers or layers just to avoid `bpy`.
- `ui.py` only draws; `operators.py` only wires UI to logic. Logic lives in the modules.
- Functions do one thing and stay short. Prefer plain functions and dataclasses over class hierarchies.

## Style
- Python 3.11+, type hints on all functions, `pathlib.Path` for paths.
- Format and lint with `ruff` (`ruff format`, `ruff check` with rules F, E, W, I, N; line length 100), enforced in CI.
- Use `logging.getLogger(__name__)` for debug logs; no custom logging setup.
- Clear names over comments. Comment only *why*, never *what*. Short docstrings on public functions.
- No magic strings: Unity property names, extensions, and suffixes live as constants in one place.

## Blender
- Target Blender 5.1+ API only; no compatibility shims.
- Prefer `bpy.data` / direct API over `bpy.ops` where possible; never depend on UI context in logic.
- Every operator returns `{'FINISHED'}` or `{'CANCELLED'}` and reports problems with `self.report`.
- One `classes = (...)` tuple in `__init__.py`: register in order, unregister in reverse. No auto-registration magic.
- Import/export operators subclass `ImportHelper`/`ExportHelper`, use `bl_options = {'UNDO', 'PRESET'}`, and add a `FileHandler` for drag-and-drop.
- Operators that need a selection define `poll`.
- Preferences use `bl_idname = __package__`; never hard-code the package name.
- No `bpy.app.version` branches and no translations until needed.

## Errors
- Fail loudly with clear messages; never silently swallow exceptions.
- Never guess (licenses, categories, missing textures). Report what couldn't be resolved. Types come from the extension, plus Unity's own import settings for animation-only FBX; never from names.
- Never modify files in the source library project. Write only to the chosen export folder, and ask before overwriting.

## Tests
- No automated tests for now; testing is manual. Don't add tests unless asked.

## Releases
- Version lives only in `blender_manifest.toml` (license `SPDX:GPL-3.0-or-later`). Validate and build with `blender --command extension validate` / `build`.
- Keep a `CHANGELOG.md`; releases are built from git tags in CI.

## Commits
- Small, focused commits with imperative messages ("Add .meta GUID parser").
