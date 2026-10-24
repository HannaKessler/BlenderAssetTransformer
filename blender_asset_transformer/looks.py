"""Saved looks: color palettes and texture sets, each under a category. Surface categories
(bark, rock, ...) recolor their base-color image; foliage keeps only its mask and takes a flat
leaf color. A palette and a texture set can be mixed freely."""

import json
import logging
import shutil
from array import array
from operator import itemgetter
from pathlib import Path

import bpy

from . import io

log = logging.getLogger(__name__)

LOOKS_FILE = "looks.json"
TEXTURES_DIR = Path("looks") / "textures"  # next to looks.json: <category>/<name>/<files>
PALETTES = "palettes"
TEXTURE_SETS = "texture_sets"
PALETTE_SIZE = 8
MIN_SATURATION = 0.08  # below this, a pixel is treated as gray and follows luminance only
SAMPLE_STEPS = 6  # samples per triangle edge when reading the texels a slot's UVs cover
FOLIAGE = "foliage"  # reserved category: mask plus a flat leaf color
FOLIAGE_ROLES = ("BaseColor", "Normal")  # the mask lives in the base color's alpha
SRGB = "sRGB"
NON_COLOR = "Non-Color"
LEGACY_KINDS = ("bark", "foliage")  # earlier files: palettes listed per kind
LOOK_SOURCE_PROPERTY = "bat_look_source"  # on a look's material: the material it was made from

_state: dict = {PALETTES: [], TEXTURE_SETS: [], "error": None}


def looks_path() -> Path:
    """`looks.json` in the Blender user config."""
    return Path(bpy.utils.user_resource("CONFIG")) / "blender_asset_transformer" / LOOKS_FILE


def error() -> str | None:
    """Why the looks file could not be read, if it could not."""
    return _state["error"]


def names(store: str, category: str) -> list[str]:
    """Saved palette or texture set names in `category`, in file order."""
    return [entry["name"] for entry in _state[store] if entry["category"] == category]


def load() -> None:
    """Read the looks file. A missing file means there are no looks. A bad file is left on disk
    and blocks saving until it is fixed."""
    file = looks_path()
    _state.update({PALETTES: [], TEXTURE_SETS: [], "error": None})
    if not file.is_file():
        return
    try:
        parsed = _parse(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _state["error"] = f"{LOOKS_FILE}: {exc}"
        log.error(_state["error"])
        return
    _state.update(parsed)


def save_palette(
    obj: bpy.types.Object, slot: int, category: str, name: str
) -> list[tuple[float, float, float]]:
    """Store a palette sampled from `slot`. The same category and name replaces it."""
    category, name = _key(category, name)
    material = _slot_material(obj, slot)
    colors = _palette(_samples(obj, slot, _base_image(material)))
    _store(PALETTES, {"name": name, "category": category, "colors": [list(c) for c in colors]})
    return colors


def save_texture_set(obj: bpy.types.Object, slot: int, category: str, name: str) -> list[str]:
    """Copy the slot's texture images into the looks folder; returns the roles saved. Images
    without a file (packed or generated) are written as PNG. Replaces a set of the same name."""
    category, name = _key(category, name)
    material = _slot_material(obj, slot)
    roles = FOLIAGE_ROLES if category == FOLIAGE else io.ROLE_SUFFIXES
    images = {role: node.image for role in roles if (node := _texture_node(material, role))}
    images = {role: image for role, image in images.items() if image}
    if not images:
        raise ValueError(f"{material.name}: no textures to save")
    folder = _set_folder(category, name)
    staging = folder.with_name(folder.name + ".saving")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    files: dict[bpy.types.Image, str] = {}
    try:
        for image in dict.fromkeys(images.values()):
            files[image] = _copy_image(image, staging, set(files.values()))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(folder, ignore_errors=True)
    staging.rename(folder)
    relative = folder.relative_to(looks_path().parent)
    textures = {role: (relative / files[image]).as_posix() for role, image in images.items()}
    _store(TEXTURE_SETS, {"name": name, "category": category, "textures": textures})
    return list(textures)


def remove(store: str, category: str, name: str) -> None:
    """Delete the named palette or texture set (with its folder) and rewrite the file."""
    _require_writable()
    if not name:
        raise ValueError("Choose what to remove")
    if name not in names(store, category):
        raise ValueError(f"No {category} {_label(store)} named {name}")
    _state[store] = [
        entry for entry in _state[store] if (entry["category"], entry["name"]) != (category, name)
    ]
    _write()
    if store == TEXTURE_SETS:
        shutil.rmtree(_set_folder(category, name), ignore_errors=True)


def apply_look(
    obj: bpy.types.Object,
    slot: int,
    category: str,
    palette: str,
    texture_set: str,
    faces: list[int] | None = None,
) -> list[str]:
    """Give the slot (or only `faces` of it, on a new slot) a copy of its material with the
    texture set's images (if chosen), then the palette (if chosen). Foliage gets its mask path;
    any other category the surface recolor. A patch whose look is already on the object reuses
    that material. Returns notes."""
    if not palette and not texture_set:
        raise ValueError("Choose a palette or a texture set")
    material = _slot_material(obj, slot)
    source = material.get(LOOK_SOURCE_PROPERTY, material.name)
    name = "_".join([source, *filter(None, (texture_set, palette))])
    if faces is not None:
        existing = next(
            (s.material for s in obj.material_slots if s.material and s.material.name == name), None
        )
        if existing:
            io.assign_material(obj, faces, existing)
            return [f"{obj.name}: {len(faces)} faces now use {name}"]
    copy = material.copy()
    copy.name = name
    copy[LOOK_SOURCE_PROPERTY] = source
    for role in io.ROLE_INPUTS:  # label a found-by-wiring node so export ships the look's image
        node = _texture_node(copy, role)
        if node is not None and not node.label:
            node.label = role
    try:
        notes = _load_set(copy, category, texture_set) if texture_set else []
        if palette:
            colors = [tuple(color) for color in _entry(PALETTES, category, palette)["colors"]]
            recolor = _foliage_color if category == FOLIAGE else _surface_recolor
            notes.append(recolor(copy, colors))
    except Exception:
        bpy.data.materials.remove(copy)
        raise
    if faces is None:
        obj.material_slots[slot].material = copy
        return [f"{obj.name} slot {slot}: {copy.name}", *notes]
    io.assign_material(obj, faces, copy)
    return [f"{obj.name}: {len(faces)} faces now use {copy.name}", *notes]


def _key(category: str, name: str) -> tuple[str, str]:
    _require_writable()
    category, name = category.strip().lower(), name.strip()
    if not category:
        raise ValueError("Enter a category")
    if not name:
        raise ValueError("Enter a name")
    return category, name


def _label(store: str) -> str:
    return "palette" if store == PALETTES else "texture set"


def _store(store: str, entry: dict) -> None:
    entries = _state[store]
    key = (entry["category"], entry["name"])
    index = next((i for i, e in enumerate(entries) if (e["category"], e["name"]) == key), None)
    if index is None:
        entries.append(entry)
    else:
        entries[index] = entry
    _write()


def _entry(store: str, category: str, name: str) -> dict:
    entry = next((e for e in _state[store] if (e["category"], e["name"]) == (category, name)), None)
    if entry is None:
        raise ValueError(f"No {category} {_label(store)} named {name}")
    return entry


def _set_folder(category: str, name: str) -> Path:
    return (
        looks_path().parent
        / TEXTURES_DIR
        / bpy.path.clean_name(category)
        / bpy.path.clean_name(name)
    )


def _copy_image(image: bpy.types.Image, folder: Path, taken: set[str]) -> str:
    """Copy the image's file as is, or write it as PNG when it has none; returns the file name."""
    source = Path(bpy.path.abspath(image.filepath)) if image.filepath else None
    on_disk = image.source == "FILE" and not image.packed_file and source and source.is_file()
    file_name = source.name if on_disk else f"{bpy.path.clean_name(image.name)}.png"
    if file_name in taken:
        raise ValueError(f"{image.name}: another texture in the set is also named {file_name}")
    if on_disk:
        shutil.copy2(source, folder / file_name)
    else:
        io.save_png(image, folder / file_name, None)
    return file_name


def _load_set(material: bpy.types.Material, category: str, name: str) -> list[str]:
    """Point the material's texture nodes at the set's files. A role the material has no
    texture for is reported, not added."""
    textures = _entry(TEXTURE_SETS, category, name)["textures"]
    unused = []
    for role, relative in textures.items():
        if category == FOLIAGE and role not in FOLIAGE_ROLES:
            continue
        path = looks_path().parent / relative
        if not path.is_file():
            raise ValueError(f"{category} texture set {name}: file missing: {path}")
        node = _texture_node(material, role)
        if node is None:
            unused.append(role)
            continue
        node.image = bpy.data.images.load(str(path), check_existing=True)
        node.image.colorspace_settings.name = NON_COLOR if role in io.NON_COLOR_ROLES else SRGB
    return [f"{name}: {material.name} has no {role} texture; not used" for role in unused]


def _require_writable() -> None:
    if _state["error"]:
        raise ValueError(f"{LOOKS_FILE} could not be read; it was left unchanged")


def _parse(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    palettes = [_parse_palette(entry) for entry in _list(data, PALETTES)]
    for kind in LEGACY_KINDS:
        palettes += [_parse_palette({**entry, "category": kind}) for entry in _list(data, kind)]
    sets = [_parse_texture_set(entry) for entry in _list(data, TEXTURE_SETS)]
    return {PALETTES: palettes, TEXTURE_SETS: sets}


def _list(data: dict, key: str) -> list:
    entries = data.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")
    return entries


def _parse_named(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("each entry must be an object")
    for key in ("name", "category"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"an entry needs a {key}")
    return {"name": entry["name"], "category": entry["category"].strip().lower()}


def _parse_palette(entry: object) -> dict:
    parsed = _parse_named(entry)
    colors = entry.get("colors")
    if not isinstance(colors, list) or not colors or len(colors) > PALETTE_SIZE:
        raise ValueError(f"{parsed['name']}: needs 1 to {PALETTE_SIZE} colors")
    cleaned = []
    for color in colors:
        if (
            not isinstance(color, list)
            or len(color) != 3
            or any(not isinstance(c, (int, float)) or not 0 <= c <= 1 for c in color)
        ):
            raise ValueError(f"{parsed['name']}: colors must be r, g, b from 0 to 1")
        cleaned.append([float(channel) for channel in color])
    return {**parsed, "colors": cleaned}


def _parse_texture_set(entry: object) -> dict:
    parsed = _parse_named(entry)
    textures = entry.get("textures")
    if not isinstance(textures, dict) or not textures:
        raise ValueError(f"{parsed['name']}: needs textures")
    for role, relative in textures.items():
        if role not in io.ROLE_SUFFIXES or not isinstance(relative, str) or not relative:
            raise ValueError(f"{parsed['name']}: bad texture entry {role}")
    return {**parsed, "textures": dict(textures)}


def _write() -> None:
    file = looks_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {PALETTES: _state[PALETTES], TEXTURE_SETS: _state[TEXTURE_SETS]}
    file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _slot_material(obj: bpy.types.Object, slot: int) -> bpy.types.Material:
    if obj.type != "MESH":
        raise ValueError(f"{obj.name}: not a mesh")
    if slot < 0 or slot >= len(obj.material_slots) or obj.material_slots[slot].material is None:
        raise ValueError(f"{obj.name}: choose a material slot")
    return obj.material_slots[slot].material


def _texture_node(material: bpy.types.Material, role: str) -> bpy.types.Node | None:
    """The image node for `role`: the labelled one, else an image wired straight into the
    matching Principled input (imports leave such a node unlabelled now and then)."""
    node = io.role_node(material, role)
    socket = io.ROLE_INPUTS.get(role)
    if node is not None or socket is None or material.node_tree is None:
        return node
    for link in material.node_tree.links:
        if (
            link.to_node.type == "BSDF_PRINCIPLED"
            and link.to_socket.name == socket
            and link.from_node.type == "TEX_IMAGE"
            and link.from_node.image
        ):
            return link.from_node
    return None


def _base_node(material: bpy.types.Material) -> bpy.types.Node:
    node = _texture_node(material, "BaseColor")
    if node is None:
        raise ValueError(f"{material.name}: no base-color image")
    return node


def _base_image(material: bpy.types.Material) -> bpy.types.Image:
    image = _base_node(material).image
    width, height = image.size
    if width == 0 or height == 0:
        raise ValueError(f"{image.name}: image has no pixels")
    return image


def _srgb_pixels(image: bpy.types.Image) -> array:
    """RGBA in sRGB. An 8-bit buffer already holds its color space's values; a float buffer
    holds linear values."""
    if image.channels != 4:
        raise ValueError(f"{image.name}: expected RGBA, got {image.channels} channels")
    pixels = io.read_pixels(image)
    if not image.is_float:
        if image.colorspace_settings.name != SRGB:
            raise ValueError(
                f"{image.name}: base color is {image.colorspace_settings.name}, expected {SRGB}"
            )
        return pixels
    for channel in range(3):
        pixels[channel::4] = array("f", map(_to_srgb, pixels[channel::4]))
    return pixels


def _samples(
    obj: bpy.types.Object, slot: int, image: bpy.types.Image
) -> list[tuple[float, float, float]]:
    """sRGB texels under the slot's faces, so an atlas's empty margin stays out."""
    uv_layer = obj.data.uv_layers.active
    if uv_layer is None:
        raise ValueError(f"{obj.name}: no UV map")
    width, height = image.size
    pixels = _srgb_pixels(image)
    samples = []
    faces = 0
    for poly in obj.data.polygons:
        if poly.material_index != slot:
            continue
        faces += 1
        uvs = [uv_layer.data[index].uv for index in poly.loop_indices]
        for index in range(1, len(uvs) - 1):
            triangle = (uvs[0], uvs[index], uvs[index + 1])
            samples.extend(_triangle_samples(triangle, pixels, width, height))
    if faces == 0:
        raise ValueError(f"{obj.name}: the slot has no faces")
    return samples


def _triangle_samples(
    triangle: tuple, pixels: array, width: int, height: int
) -> list[tuple[float, float, float]]:
    samples = []
    for i in range(SAMPLE_STEPS + 1):
        for j in range(SAMPLE_STEPS + 1 - i):
            a, b = i / SAMPLE_STEPS, j / SAMPLE_STEPS
            c = 1 - a - b
            u = (c * triangle[0].x + a * triangle[1].x + b * triangle[2].x) % 1.0
            v = (c * triangle[0].y + a * triangle[1].y + b * triangle[2].y) % 1.0
            x = min(width - 1, int(u * width))
            y = min(height - 1, int(v * height))
            index = (y * width + x) * 4
            samples.append((pixels[index], pixels[index + 1], pixels[index + 2]))
    return samples


def _palette(samples: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Median cut to at most PALETTE_SIZE colors, ordered dark to light."""
    buckets = [samples]
    while len(buckets) < PALETTE_SIZE:
        ranges = [(_span(bucket), i) for i, bucket in enumerate(buckets) if len(bucket) > 1]
        if not ranges or max(ranges)[0] < 1e-4:
            break
        bucket = buckets.pop(max(ranges)[1])
        channel = max(range(3), key=lambda c: _channel_span(bucket, c))
        ordered = sorted(bucket, key=lambda sample: sample[channel])
        buckets += [ordered[: len(ordered) // 2], ordered[len(ordered) // 2 :]]
    colors = [
        tuple(min(1.0, max(0.0, sum(s[c] for s in bucket) / len(bucket))) for c in range(3))
        for bucket in buckets
    ]
    return sorted(set(colors), key=lambda color: (_luminance(color), color))


def _span(bucket: list[tuple[float, float, float]]) -> float:
    return max(_channel_span(bucket, channel) for channel in range(3))


def _channel_span(bucket: list[tuple[float, float, float]], channel: int) -> float:
    values = [sample[channel] for sample in bucket]
    return max(values) - min(values)


class _Recolored(dict):
    """sRGB color -> recolored sRGB, computed once per distinct color."""

    def __init__(self, palette: list[tuple[float, float, float]]) -> None:
        super().__init__()
        self.palette = palette
        self.hues = [_hue(color) for color in palette]
        self.luminances = [_luminance(color) for color in palette]

    def __missing__(self, color: tuple[float, float, float]) -> tuple[float, float, float]:
        value = self[color] = self._recolor(color)
        return value

    def _recolor(self, color: tuple[float, float, float]) -> tuple[float, float, float]:
        """Gray pixels take their luminance through the palette; colored pixels take the
        palette color nearest in hue, scaled by their luminance, so grooves stay dark."""
        pixel_l = _luminance(color)
        if _saturation(color) < MIN_SATURATION:
            return _along_palette(self.palette, pixel_l)
        hue = _hue(color)
        index = min(range(len(self.hues)), key=lambda i: _hue_distance(hue, self.hues[i]))
        chosen_l = self.luminances[index]
        scale = pixel_l / chosen_l if chosen_l > 1e-6 else pixel_l
        return tuple(min(1.0, max(0.0, channel * scale)) for channel in self.palette[index])


def _foliage_color(material: bpy.types.Material, colors: list[tuple]) -> str:
    """Leaves take the palette's middle color as a material color; the base-color image stays
    only as the alpha mask, so no image is rewritten."""
    color = colors[len(colors) // 2]
    mask = _base_image(material)
    if mask.channels != 4:
        raise ValueError(f"{mask.name}: foliage mask needs an alpha channel")
    material[io.LOOK_COLOR_PROPERTY] = list(color)
    tree = material.node_tree
    bsdf = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        raise ValueError(f"{material.name}: no Principled BSDF")
    base, alpha = bsdf.inputs["Base Color"], bsdf.inputs["Alpha"]
    for link in list(base.links):
        tree.links.remove(link)
    base.default_value = (*(io.srgb_to_linear(channel) for channel in color), 1.0)
    if not alpha.is_linked:
        tree.links.new(_base_node(material).outputs["Alpha"], alpha)
    hex_color = "".join(f"{round(channel * 255):02x}" for channel in color)
    return f"leaf color #{hex_color} over the mask"


def _surface_recolor(material: bpy.types.Material, colors: list[tuple]) -> str:
    """A new base-color image recolored from the palette that keeps the texture's pattern."""
    source = _base_image(material)
    pixels = _srgb_pixels(source)
    lookup = _Recolored(colors)
    rgb = zip(pixels[0::4], pixels[1::4], pixels[2::4], strict=True)
    recolored = list(map(lookup.__getitem__, rgb))
    for channel in range(3):
        pixels[channel::4] = array("f", map(itemgetter(channel), recolored))
    width, height = source.size
    image = bpy.data.images.new(f"{material.name}_BaseColor", width, height, alpha=True)
    image.colorspace_settings.name = SRGB
    image.pixels.foreach_set(pixels)
    image.pack()
    _base_node(material).image = image
    return f"recolored {len(lookup)} distinct colors"


def _along_palette(
    palette: list[tuple[float, float, float]], luminance: float
) -> tuple[float, float, float]:
    if len(palette) == 1:
        scale = luminance / max(_luminance(palette[0]), 1e-6)
        return tuple(min(1.0, channel * scale) for channel in palette[0])
    position = min(1.0, max(0.0, luminance)) * (len(palette) - 1)
    index = min(int(position), len(palette) - 2)
    mix = position - index
    return tuple(
        (1 - mix) * palette[index][channel] + mix * palette[index + 1][channel]
        for channel in range(3)
    )


def _luminance(color: tuple[float, float, float]) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def _saturation(color: tuple[float, float, float]) -> float:
    high = max(color)
    return 0.0 if high <= 1e-8 else (high - min(color)) / high


def _hue(color: tuple[float, float, float]) -> float:
    red, green, blue = color
    high, low = max(color), min(color)
    span = high - low
    if span < 1e-8:
        return 0.0
    if high == red:
        hue = ((green - blue) / span) % 6
    elif high == green:
        hue = (blue - red) / span + 2
    else:
        hue = (red - green) / span + 4
    return hue / 6


def _hue_distance(left: float, right: float) -> float:
    distance = abs(left - right)
    return min(distance, 1 - distance)


def _to_srgb(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * max(channel, 0.0)
    return 1.055 * (channel ** (1 / 2.4)) - 0.055
