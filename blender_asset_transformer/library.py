"""Scan a Unity library project into Pack > Type > file entries and a GUID index."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import unity

MESHES = "Meshes"
MATERIALS = "Materials"
TEXTURES = "Textures"
ANIMATIONS = "Animations"
OTHER = "Other"
TYPES = (MESHES, MATERIALS, TEXTURES, ANIMATIONS, OTHER)

EXTENSIONS = {
    MESHES: {".fbx", ".obj", ".blend"},
    MATERIALS: {".mat"},
    TEXTURES: {".png", ".jpg", ".jpeg", ".tga", ".psd", ".tif", ".tiff", ".exr"},
    ANIMATIONS: {".anim"},
}
IGNORED_EXTENSIONS = {unity.META_SUFFIX, ".prefab"}


@dataclass(frozen=True)
class Entry:
    pack: str
    type: str
    path: Path


@dataclass
class Index:
    root: Path
    entries: list[Entry] = field(default_factory=list)
    guids: dict[str, Path] = field(default_factory=dict)

    def packs(self) -> list[str]:
        return sorted({e.pack for e in self.entries})


def file_type(path: Path) -> str:
    """Type from the file extension only."""
    ext = path.suffix.lower()
    return next((t for t, exts in EXTENSIONS.items() if ext in exts), OTHER)


def entry_type(path: Path) -> str:
    """file_type, except FBX that Unity imports as animation-only (per its .meta) is Animations."""
    kind = file_type(path)
    meta = path.with_name(path.name + unity.META_SUFFIX)
    if path.suffix.lower() == ".fbx" and unity.is_animation_only(meta):
        return ANIMATIONS
    return kind


def list_files(project: Path) -> list[Path]:
    """Every file under <project>/Assets/<pack>/, sorted (fast: no file reads)."""
    assets = project / "Assets"
    if not assets.is_dir():
        raise FileNotFoundError(f"Not a Unity project (no Assets folder): {project}")
    return sorted(
        Path(folder, name)
        for folder, _dirs, names in os.walk(assets)
        if Path(folder) != assets
        for name in names
    )


def add_file(index: Index, path: Path) -> None:
    """Classify one listed file into index (reads .meta files)."""
    ext = path.suffix.lower()
    if ext == unity.META_SUFFIX:
        if guid := unity.meta_guid(path):
            index.guids[guid] = path.with_suffix("")
    elif ext not in IGNORED_EXTENSIONS:
        pack = path.relative_to(index.root / "Assets").parts[0]
        index.entries.append(Entry(pack, entry_type(path), path))


def scan(project: Path) -> Index:
    """Index every file under <project>/Assets/<pack>/ (read-only)."""
    index = Index(project)
    for path in list_files(project):
        add_file(index, path)
    return index


def pack_dir(index: Index, pack: str) -> Path:
    return index.root / "Assets" / pack


def pack_of(index: Index, path: Path) -> str:
    return path.relative_to(index.root / "Assets").parts[0]
