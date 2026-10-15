"""Unity project validation, editor launching, and .mat/.meta parsing."""

import re
import subprocess
import sys
from pathlib import Path

PROJECT_VERSION_FILE = Path("ProjectSettings") / "ProjectVersion.txt"
EDITOR_VERSION_KEY = "m_EditorVersion:"
META_SUFFIX = ".meta"

# Unity shader texture properties (Built-in, URP, HDRP) mapped to our texture roles.
TEXTURE_SLOTS = {
    "_MainTex": "BaseColor",
    "_BaseMap": "BaseColor",
    "_BaseColorMap": "BaseColor",
    "_BumpMap": "Normal",
    "_NormalMap": "Normal",
    "_MetallicGlossMap": "Metallic",
    "_OcclusionMap": "AO",
    "_EmissionMap": "Emission",
    "_EmissiveColorMap": "Emission",
}

_GUID_RE = re.compile(r"^guid:\s*([0-9a-f]{32})\s*$", re.MULTILINE)
_TEX_PROP_RE = re.compile(r"^\s*-\s*(_\w+):\s*$")
_REMAP_RE = re.compile(
    r"type: UnityEngine:Material.*?name: ([^\n]+)\n\s*second: \{[^}]*guid: ([0-9a-f]{32})", re.S
)
_IMPORT_ANIMATION_RE = re.compile(r"^\s*importAnimation: 1\s*$", re.M)
_MATERIAL_MODE_RE = re.compile(r"^\s*materialImportMode: (\d+)\s*$", re.M)
_CLIP_RE = re.compile(r"^\s*takeName: ", re.M)  # one per defined animation clip
_LEGACY_MATERIALS_RE = re.compile(r"^\s*importMaterials: (\d+)\s*$", re.M)
_SHADER_RE = re.compile(r"^\s*m_Shader:\s*\{[^}]*guid:\s*([0-9a-f]{32})", re.M)
_FLOAT_RE = re.compile(r"^\s*-\s*(_\w+):\s*(-?[\d.]+(?:[eE][-+]?\d+)?)\s*$", re.M)
_COLOR_RE = re.compile(
    r"^\s*-\s*(_\w+):\s*\{r:\s*([-\d.eE]+),\s*g:\s*([-\d.eE]+),\s*b:\s*([-\d.eE]+),\s*a:\s*([-\d.eE]+)\}",
    re.M,
)
_TEX_REF_RE = re.compile(r"m_Texture:\s*\{[^}]*guid:\s*([0-9a-f]{32})")

_HUB_EDITORS = {
    "win32": (Path("C:/Program Files/Unity/Hub/Editor"), Path("Editor/Unity.exe")),
    "darwin": (Path("/Applications/Unity/Hub/Editor"), Path("Unity.app/Contents/MacOS/Unity")),
    "linux": (Path.home() / "Unity/Hub/Editor", Path("Editor/Unity")),
}


def is_project(path: Path) -> bool:
    """True if path looks like a Unity project root."""
    return (path / "Assets").is_dir() and (path / PROJECT_VERSION_FILE).is_file()


def editor_version(project: Path) -> str:
    """Read the editor version from ProjectSettings/ProjectVersion.txt."""
    for line in (project / PROJECT_VERSION_FILE).read_text(encoding="utf-8").splitlines():
        if line.startswith(EDITOR_VERSION_KEY):
            return line.removeprefix(EDITOR_VERSION_KEY).strip()
    raise ValueError(f"No {EDITOR_VERSION_KEY} in {project / PROJECT_VERSION_FILE}")


def editor_executable(version: str, editors_dir: Path | None = None) -> Path:
    """Path to the Unity editor for version, under editors_dir or the Unity Hub default."""
    default_dir, exe = _HUB_EDITORS.get(sys.platform, _HUB_EDITORS["linux"])
    return (editors_dir or default_dir) / version / exe


def launch(project: Path, editors_dir: Path | None = None) -> Path:
    """Open project in its matching Unity editor; returns the executable used."""
    exe = editor_executable(editor_version(project), editors_dir)
    if not exe.is_file():
        raise FileNotFoundError(f"Unity editor not found: {exe}")
    subprocess.Popen([str(exe), "-projectPath", str(project)])
    return exe


def meta_guid(meta_path: Path) -> str | None:
    """GUID stored in a .meta file, or None if missing."""
    match = _GUID_RE.search(meta_path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def material_remap(model_meta: Path) -> dict[str, str]:
    """Map FBX material name to Unity .mat GUID from a model's .meta `externalObjects`."""
    if not model_meta.is_file():
        return {}
    text = model_meta.read_text(encoding="utf-8", errors="replace")
    return {_yaml_str(name): guid for name, guid in _REMAP_RE.findall(text)}


def _yaml_str(value: str) -> str:
    """Unquote a YAML scalar; Unity quotes names with special characters ('Material #45')."""
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].replace("''", "'")
    return value


def is_animation_only(model_meta: Path) -> bool:
    """True if Unity imports this model with clips defined and materials off (no .meta: False).

    importAnimation is on by default, so defined clips are required too.
    """
    if not model_meta.is_file():
        return False
    text = model_meta.read_text(encoding="utf-8", errors="replace")
    materials = _MATERIAL_MODE_RE.search(text) or _LEGACY_MATERIALS_RE.search(text)
    animated = _IMPORT_ANIMATION_RE.search(text) and _CLIP_RE.search(text)
    return bool(animated and materials and materials.group(1) == "0")


def has_clips(model_meta: Path) -> bool:
    """True if Unity imports animation clips from this model."""
    if not model_meta.is_file():
        return False
    text = model_meta.read_text(encoding="utf-8", errors="replace")
    return bool(_IMPORT_ANIMATION_RE.search(text) and _CLIP_RE.search(text))


def shader_guid(mat_path: Path) -> str | None:
    match = _SHADER_RE.search(mat_path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def material_values(mat_path: Path) -> dict[str, float | tuple[float, ...]]:
    """Float and color properties (`_Name: value` / `{r, g, b, a}`) from a Unity .mat."""
    text = mat_path.read_text(encoding="utf-8", errors="replace")
    values: dict[str, float | tuple[float, ...]] = {
        name: float(value) for name, value in _FLOAT_RE.findall(text)
    }
    for name, *rgba in _COLOR_RE.findall(text):
        values[name] = tuple(float(c) for c in rgba)
    return values


def material_textures(mat_path: Path) -> dict[str, str]:
    """Map texture role (BaseColor, Normal, ...) to texture GUID from a Unity .mat file."""
    slots: dict[str, str] = {}
    prop = None
    for line in mat_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if prop_match := _TEX_PROP_RE.match(line):
            prop = prop_match.group(1)
        elif prop and (ref := _TEX_REF_RE.search(line)):
            role = TEXTURE_SLOTS.get(prop)
            if role and role not in slots:
                slots[role] = ref.group(1)
            prop = None
    return slots
