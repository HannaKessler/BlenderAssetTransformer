"""Unity project validation, editor launching, and .mat/.meta parsing."""

import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
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
    "_MaskMap": "Metallic",  # HDRP mask map: R metallic, A smoothness, like a metallic map
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


def material_values(mat: Path | str) -> dict[str, float | tuple[float, ...]]:
    """Float and color properties (`_Name: value` / `{r, g, b, a}`) from a Unity .mat
    (path or text)."""
    text = mat if isinstance(mat, str) else mat.read_text(encoding="utf-8", errors="replace")
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


# ModelImporterClipAnimation wrapMode (UnityEngine.WrapMode)
WRAP_DEFAULT = 0
WRAP_ONCE = 1
WRAP_LOOP = 2
WRAP_PING_PONG = 4
WRAP_CLAMP_FOREVER = 8

_CLIP_FIELD_RE = {
    "name": re.compile(r"^\s*name: (.+)$", re.M),
    "takeName": re.compile(r"^\s*takeName: (.+)$", re.M),
    "firstFrame": re.compile(r"^\s*firstFrame: (-?[\d.]+(?:[eE][-+]?\d+)?)\s*$", re.M),
    "lastFrame": re.compile(r"^\s*lastFrame: (-?[\d.]+(?:[eE][-+]?\d+)?)\s*$", re.M),
    "wrapMode": re.compile(r"^\s*wrapMode: (-?\d+)\s*$", re.M),
    "loop": re.compile(r"^\s*loop: ([01])\s*$", re.M),
    "loopTime": re.compile(r"^\s*loopTime: ([01])\s*$", re.M),
}


@dataclass(frozen=True)
class ClipAnimation:
    """One ModelImporter clipAnimations entry from a model .meta."""

    name: str
    take_name: str
    first_frame: float
    last_frame: float
    wrap_mode: int
    loop: bool
    loop_time: bool

    @property
    def loops(self) -> bool:
        """True when Unity plays this clip as a loop (loopTime, legacy loop, or wrapMode)."""
        return self.loop_time or self.loop or self.wrap_mode in (WRAP_LOOP, WRAP_PING_PONG)

    @property
    def ping_pong(self) -> bool:
        return self.wrap_mode == WRAP_PING_PONG


def clip_animations(model_meta: Path) -> list[ClipAnimation]:
    """Parse ModelImporter clipAnimations from a model .meta; raises if fields are missing."""
    if not model_meta.is_file():
        raise ValueError(f"Missing model .meta (needed for animation clip settings): {model_meta}")
    text = model_meta.read_text(encoding="utf-8", errors="replace")
    marker = re.search(r"^(\s*)clipAnimations:[ \t]*(.*)$", text, re.M)
    if not marker:
        raise ValueError(
            f"{model_meta.name}: no clipAnimations (needed for animation loop settings)"
        )
    indent, rest = marker.group(1), marker.group(2).strip()
    if rest == "[]":
        return []
    # Body runs until the next key at the same indent as clipAnimations.
    start = marker.end()
    end_match = re.search(rf"^{indent}[a-zA-Z]", text[start:], re.M)
    body = text[start : start + end_match.start()] if end_match else text[start:]
    # List dashes sit at the same indent as clipAnimations; fields are indented further.
    parts = re.split(rf"\n{re.escape(indent)}- serializedVersion: \d+\n", "\n" + body)
    clips: list[ClipAnimation] = []
    for i, part in enumerate(parts[1:], start=1):
        fields: dict[str, str] = {}
        for key, pattern in _CLIP_FIELD_RE.items():
            match = pattern.search(part)
            if not match:
                raise ValueError(f"{model_meta.name}: clipAnimations[{i}] missing {key}")
            fields[key] = match.group(1).strip()
        clips.append(
            ClipAnimation(
                name=_yaml_str(fields["name"]),
                take_name=_yaml_str(fields["takeName"]),
                first_frame=float(fields["firstFrame"]),
                last_frame=float(fields["lastFrame"]),
                wrap_mode=int(fields["wrapMode"]),
                loop=fields["loop"] == "1",
                loop_time=fields["loopTime"] == "1",
            )
        )
    return clips


# Only these clip fields are written; everything else in an existing .meta is kept as is.
CLIP_SERIALIZED_VERSION = 16
MODEL_IMPORTER_SERIALIZED_VERSION = 20300


def _clip_values(clip: ClipAnimation) -> dict[str, str]:
    return {
        "firstFrame": f"{clip.first_frame:g}",
        "lastFrame": f"{clip.last_frame:g}",
        "wrapMode": str(clip.wrap_mode),
        "loop": str(int(clip.loop)),
        "loopTime": str(int(clip.loop_time)),
    }


_PLAIN_YAML_RE = re.compile(r"^[\w.()-][\w .|()-]*(?<! )$")


def _yaml_quote(value: str) -> str:
    """Plain scalar when safe, else single-quoted (names may hold `: `, `#`, quotes)."""
    if _PLAIN_YAML_RE.match(value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _clip_entry(clip: ClipAnimation, indent: str) -> str:
    fields = {
        "name": _yaml_quote(clip.name),
        "takeName": _yaml_quote(clip.take_name),
        **_clip_values(clip),
    }
    lines = [f"{indent}- serializedVersion: {CLIP_SERIALIZED_VERSION}"]
    lines += [f"{indent}  {key}: {value}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def new_model_meta(clips: list[ClipAnimation], remaps: dict[str, str]) -> str:
    """Minimal ModelImporter .meta with a new GUID; Unity fills every other field with defaults."""
    remap_body = "".join(_remap_entry(name, guid) for name, guid in remaps.items())
    clip_body = "".join(_clip_entry(clip, "    ") for clip in clips)
    return (
        f"fileFormatVersion: 2\nguid: {uuid.uuid4().hex}\nModelImporter:\n"
        f"  serializedVersion: {MODEL_IMPORTER_SERIALIZED_VERSION}\n"
        + (f"  externalObjects:\n{remap_body}" if remaps else "")
        + (f"  animations:\n    clipAnimations:\n{clip_body}" if clips else "")
    )


def merge_clip_animations(text: str, clips: list[ClipAnimation], label: str) -> str:
    """Update frame range and loop fields of clips matched by takeName; append the rest.

    Drops clips whose take is no longer exported (deleted or renamed actions). A take that
    the user split into several clips in Unity is left as is. Keeps the GUID and every other
    field. Raises if the .meta layout is not recognized.
    """
    marker = re.search(r"^( *)clipAnimations:[ \t]*(.*)\n", text, re.M)
    if not marker or not _GUID_RE.search(text):
        raise ValueError(f"{label}: no guid or clipAnimations; can't update it safely")
    indent, rest = marker.group(1), marker.group(2).strip()
    if rest not in ("", "[]"):
        raise ValueError(f"{label}: unexpected clipAnimations value {rest!r}")
    start = end = marker.end()
    if not rest:  # block list: entries start with `- ` at this indent, fields go deeper
        for line in text[start:].splitlines(keepends=True):
            if not (line.startswith(f"{indent}- ") or line.startswith(f"{indent}  ")):
                break
            end += len(line)
    entries = re.split(rf"(?m)^(?={re.escape(indent)}- )", text[start:end])
    entries = [e for e in entries if e]
    by_take = {clip.take_name: clip for clip in clips}
    takes = []
    for entry in entries:
        take = re.search(r"^ *takeName: (.+)$", entry, re.M)
        if not take:
            raise ValueError(f"{label}: clipAnimations entry without takeName")
        takes.append(_yaml_str(take.group(1)))
    split = {take for take in takes if takes.count(take) > 1}
    updated: list[str] = []
    for entry, take in zip(entries, takes, strict=True):
        if take not in by_take and take not in split:
            continue
        clip = by_take.pop(take, None)
        if clip and take not in split:
            for key, value in _clip_values(clip).items():
                entry, count = re.subn(rf"(?m)^( *{key}): .*$", rf"\g<1>: {value}", entry, 1)
                if not count:
                    raise ValueError(f"{label}: clip {clip.take_name} has no {key} field")
        updated.append(entry)
    updated += [_clip_entry(clip, indent) for clip in by_take.values()]
    head = text[: marker.start()] + f"{indent}clipAnimations:\n"
    return head + "".join(updated) + text[end:]


def write_model_meta(meta: Path, clips: list[ClipAnimation], remaps: dict[str, str]) -> None:
    """Create the model .meta with these clips and material remaps, or merge them into the
    existing one (GUID and every other field kept)."""
    if not meta.is_file():
        meta.write_text(new_model_meta(clips, remaps), encoding="utf-8")
        return
    text = meta.read_text(encoding="utf-8")
    if clips:
        text = merge_clip_animations(text, clips, meta.name)
    if remaps:
        text = merge_material_remaps(text, remaps, meta.name)
    meta.write_text(text, encoding="utf-8")


URP_LIT_SHADER = "{fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}"
MATERIAL_FILE_ID = 2100000
TEXTURE_FILE_ID = 2800000
MATERIAL_TYPE = "UnityEngine:Material"
MATERIAL_ASSEMBLY = "UnityEngine.CoreModule"
CUTOUT_QUEUE = 2450
# Texture role -> URP Lit slot and the keyword that enables it.
LIT_SLOTS = {
    "BaseColor": ("_BaseMap", None),
    "Normal": ("_BumpMap", "_NORMALMAP"),
    "Metallic": ("_MetallicGlossMap", "_METALLICSPECGLOSSMAP"),
    "AO": ("_OcclusionMap", "_OCCLUSIONMAP"),
    "Emission": ("_EmissionMap", "_EMISSION"),
}
_MAIN_SLOTS = ("_BaseMap", "_MainTex", "_BaseColorMap")
_TILING_RE = r"- {}:\n\s+m_Texture: [^\n]*\n\s+m_Scale: \{{x: ([^,]+), y: ([^}}]+)\}}\n" + (
    r"\s+m_Offset: \{{x: ([^,]+), y: ([^}}]+)\}}"
)


def _is_cutout(text: str, values: dict) -> bool:
    """Alpha test as the source material declares it (Built-in, URP, or HDRP fields)."""
    return (
        "_ALPHATEST_ON" in text
        or "RenderType: TransparentCutout" in text
        or f"m_CustomRenderQueue: {CUTOUT_QUEUE}" in text
        or values.get("_Mode") == 1
        or values.get("_AlphaClip") == 1
        or values.get("_AlphaCutoffEnable") == 1
    )


def _is_two_sided(text: str, values: dict) -> bool:
    """Culling off as the source declares it (cull property, HDRP double-sided, or GI flag)."""
    return (
        values.get("_Cull", values.get("_CullMode")) == 0
        or values.get("_DoubleSidedEnable") == 1
        or "_DOUBLESIDED_ON" in text
        or "m_DoubleSidedGI: 1" in text
    )


def _tiling(text: str) -> tuple[str, str, str, str]:
    for slot in _MAIN_SLOTS:
        if match := re.search(_TILING_RE.format(re.escape(slot)), text):
            return match.groups()
    return ("1", "1", "0", "0")


def _rgba(color: tuple[float, ...]) -> str:
    r, g, b, a = (*color, 1.0, 1.0, 1.0, 1.0)[:4]
    return f"{{r: {r:g}, g: {g:g}, b: {b:g}, a: {a:g}}}"


def lit_material(
    name: str, source_mat: Path | str, textures: dict[str, str], *, color_baked: bool = False
) -> str:
    """URP Lit .mat translated from a source .mat of any shader, using exported texture GUIDs
    per role. source_mat may also be .mat text.
    color_baked: the base color texture already carries the tint, so the color stays white."""
    if isinstance(source_mat, Path):
        text = source_mat.read_text(encoding="utf-8", errors="replace")
    else:
        text = source_mat
    values = material_values(text)
    cutout, two_sided = _is_cutout(text, values), _is_two_sided(text, values)
    if values.get("_AlphaCutoffEnable") == 1 and "_AlphaCutoff" in values:
        cutoff = values["_AlphaCutoff"]
    else:
        cutoff = values.get("_Cutoff", values.get("_AlphaCutoff", 0.5))
    if "_Smoothness" in values:
        smoothness = values["_Smoothness"]
    elif "Metallic" in textures:
        smoothness = values.get("_GlossMapScale", 1.0)
    else:
        smoothness = values.get("_Glossiness", 0.5)
    emission = values.get("_EmissionColor", (0.0, 0.0, 0.0, 1.0))
    keywords = [LIT_SLOTS[role][1] for role in textures if LIT_SLOTS[role][1]]
    if any(c > 0 for c in emission[:3]) and "_EMISSION" not in keywords:
        keywords.append("_EMISSION")
    if cutout:
        keywords.append("_ALPHATEST_ON")
    sx, sy, ox, oy = _tiling(text)
    tex_envs = "".join(
        f"    - {LIT_SLOTS[role][0]}:\n"
        f"        m_Texture: {{fileID: {TEXTURE_FILE_ID}, guid: {guid}, type: 3}}\n"
        f"        m_Scale: {{x: {sx}, y: {sy}}}\n        m_Offset: {{x: {ox}, y: {oy}}}\n"
        for role, guid in textures.items()
    )
    floats = {
        "_WorkflowMode": 1,
        "_Surface": 0,
        "_AlphaClip": int(cutout),
        "_Cutoff": cutoff,
        "_Cull": 0 if two_sided else 2,
        "_Metallic": values.get("_Metallic", 0.0),
        "_Smoothness": smoothness,
        "_SmoothnessTextureChannel": 0,
        "_BumpScale": values.get("_BumpScale", 1.0),
        "_OcclusionStrength": values.get("_OcclusionStrength", 1.0),
    }
    color = values.get("_BaseColor") or values.get("_Color") or (1.0, 1.0, 1.0, 1.0)
    if color_baked:
        color = (1.0, 1.0, 1.0, color[3] if len(color) > 3 else 1.0)
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\nMaterial:\n"
        f"  serializedVersion: 8\n  m_Name: {_yaml_quote(name)}\n  m_Shader: {URP_LIT_SHADER}\n"
        "  m_ValidKeywords:"
        + "".join(f"\n  - {k}" for k in keywords)
        + ("" if keywords else " []")
        + "\n  m_InvalidKeywords: []\n"
        f"  m_DoubleSidedGI: {int(two_sided)}\n"
        f"  m_CustomRenderQueue: {CUTOUT_QUEUE if cutout else -1}\n"
        + (
            "  stringTagMap:\n    RenderType: TransparentCutout\n"
            if cutout
            else "  stringTagMap: {}\n"
        )
        + "  m_SavedProperties:\n    serializedVersion: 3\n"
        + "    m_TexEnvs:"
        + ("\n" + tex_envs if tex_envs else " []\n")
        + "    m_Ints: []\n    m_Floats:\n"
        + "".join(f"    - {k}: {v:g}\n" for k, v in floats.items())
        + f"    m_Colors:\n    - _BaseColor: {_rgba(color)}\n"
        + f"    - _EmissionColor: {_rgba(emission)}\n"
    )


def write_asset_meta(meta: Path, body: str) -> str:
    """Write a sidecar .meta, keeping the GUID it already has (references survive re-export).
    Returns the GUID."""
    guid = (meta_guid(meta) if meta.is_file() else None) or uuid.uuid4().hex
    meta.write_text(f"fileFormatVersion: 2\nguid: {guid}\n{body}", encoding="utf-8")
    return guid


def _remap_entry(name: str, guid: str) -> str:
    return (
        f"  - first:\n      type: {MATERIAL_TYPE}\n      assembly: {MATERIAL_ASSEMBLY}\n"
        f"      name: {_yaml_quote(name)}\n"
        f"    second: {{fileID: {MATERIAL_FILE_ID}, guid: {guid}, type: 2}}\n"
    )


def merge_material_remaps(text: str, remaps: dict[str, str], label: str) -> str:
    """Set ModelImporter externalObjects material remaps (FBX material name -> .mat GUID),
    replacing earlier entries for the same names and keeping every other entry."""
    marker = re.search(r"^  externalObjects:[ \t]*(.*)\n", text, re.M)
    new = "".join(_remap_entry(name, guid) for name, guid in remaps.items())
    if not marker:
        head = re.search(r"^ModelImporter:\n(  serializedVersion: \d+\n)?", text, re.M)
        if not head:
            raise ValueError(f"{label}: no ModelImporter section; can't add material remaps")
        return text[: head.end()] + "  externalObjects:\n" + new + text[head.end() :]
    rest = marker.group(1).strip()
    if rest not in ("", "{}", "[]"):
        raise ValueError(f"{label}: unexpected externalObjects value {rest!r}")
    start = end = marker.end()
    if not rest:
        for line in text[start:].splitlines(keepends=True):
            if not (line.startswith("  - ") or line.startswith("    ")):
                break
            end += len(line)
    kept = []
    for entry in re.split(r"(?m)^(?=  - )", text[start:end]):
        name = re.search(r"^      name: (.+)$", entry, re.M)
        is_ours = MATERIAL_TYPE in entry and name and _yaml_str(name.group(1)) in remaps
        if entry and not is_ours:
            kept.append(entry)
    return text[: marker.start()] + "  externalObjects:\n" + "".join(kept) + new + text[end:]


_GUID_RE = re.compile(r"guid\\?\"?\s*:\s*\\?\"?([0-9a-f]{32})")
_INCLUDE_RE = re.compile(r'^\s*#include(?:_with_pragmas)?\s+"([^"]+)"', re.M)
SHADER_SUFFIXES = (".shader", ".shadergraph", ".shadersubgraph")


def guid_index(folder: Path) -> dict[str, Path]:
    """GUID -> asset path for every .meta under folder."""
    index: dict[str, Path] = {}
    for meta in folder.rglob("*" + META_SUFFIX):
        asset = meta.with_suffix("")
        if asset.is_file() and (guid := meta_guid(meta)):
            index[guid] = asset
    return index


def texture_refs(text: str) -> dict[str, str]:
    """Texture property -> GUID for every texture slot of a .mat text."""
    refs: dict[str, str] = {}
    prop = None
    for line in text.splitlines():
        if prop_match := _TEX_PROP_RE.match(line):
            prop = prop_match.group(1)
        elif prop and (ref := _TEX_REF_RE.search(line)):
            refs.setdefault(prop, ref.group(1))
            prop = None
    return refs


def set_texture_refs(text: str, refs: dict[str, str]) -> str:
    """Point the named texture slots of a .mat text at new GUIDs ("" empties the slot);
    everything else is kept."""

    def swap(match: re.Match) -> str:
        guid = refs.get(match.group(2))
        if guid is None:
            return match.group(0)
        ref = f"{{fileID: {TEXTURE_FILE_ID}, guid: {guid}, type: 3}}" if guid else "{fileID: 0}"
        return match.group(1) + ref

    return re.sub(r"(?m)(^\s*- (\w+):\s*\n\s*m_Texture:\s*)\{[^}]*\}", swap, text)


def urp_shader_files(shader: Path, index: dict[str, Path]) -> list[Path] | None:
    """The shader plus every file it needs, when it targets URP and all its relative
    includes and sub graphs resolve; None otherwise (it won't compile in a URP project)."""
    files: list[Path] = []
    todo = [shader]
    while todo:
        path = todo.pop()
        if path in files:
            continue
        files.append(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix.lower()
        if suffix == ".shadergraph" and "UniversalTarget" not in text:
            return None
        if suffix == ".shader" and "UniversalPipeline" not in text:
            return None
        if suffix in (".shadergraph", ".shadersubgraph"):
            for guid in set(_GUID_RE.findall(text)):
                dep = index.get(guid)
                if dep is None:
                    return None
                if dep.suffix.lower() in (".shadersubgraph", ".hlsl", ".cginc"):
                    todo.append(dep)
        else:
            for include in _INCLUDE_RE.findall(text):
                if include.startswith("Packages/"):
                    continue
                dep = (path.parent / include).resolve()
                if not dep.is_file():
                    return None
                todo.append(dep)
    return files
