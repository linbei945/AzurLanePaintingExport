"""Core of the merged Azur Lane painting exporter.

Reads AssetBundles directly (UnityPy), rebuilds MeshImage painting parts at
native resolution and merges face expressions, reproducing the
AssetStudioMod -> AzurLanePaintingExtract -> ALPA pipeline in one step.
"""
from __future__ import annotations

import os
import re
import sys
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# UnityPy pulls in audio support (fmod) when its export module is imported.
# We never touch AudioClip, and the fmod DLL is not shipped with this tool, so
# provide a tiny stub when the real package cannot be imported.
try:  # pragma: no cover - depends on packaging
    import fmod_toolkit  # noqa: F401
except Exception:  # noqa: BLE001
    _stub = types.ModuleType("fmod_toolkit")
    _stub.raw_to_wav = lambda *a, **k: b""  # type: ignore[attr-defined]
    sys.modules["fmod_toolkit"] = _stub

import UnityPy
from PIL import Image
from UnityPy.classes import PPtr
from UnityPy.helpers.MeshHelper import MeshHandler

UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.51f1"

BANNED_NAMES = {"shop_hx", "shadow"}
BUILTIN_UI_SPRITE = "UISprite"
FACE_GO_NAME = "face"


# ---------------------------------------------------------------- vectors
class V2:
    __slots__ = ("x", "y")

    def __init__(self, x=0.0, y=0.0):
        self.x, self.y = float(x), float(y)

    def __add__(self, o: "V2") -> "V2":
        return V2(self.x + o.x, self.y + o.y)

    def __sub__(self, o: "V2") -> "V2":
        return V2(self.x - o.x, self.y - o.y)

    def __mul__(self, o) -> "V2":
        if isinstance(o, V2):
            return V2(self.x * o.x, self.y * o.y)
        return V2(self.x * o, self.y * o)

    def __truediv__(self, o: float) -> "V2":
        return V2(self.x / o, self.y / o)

    def rounded(self) -> "V2":
        return V2(round(self.x), round(self.y))

    def __repr__(self):
        return f"({self.x:.1f},{self.y:.1f})"


def vec2(v) -> V2:
    if v is None:
        return V2()
    return V2(getattr(v, "x", 0.0), getattr(v, "y", 0.0))


# ---------------------------------------------------------------- transforms
class ExtTransform:
    """Port of ALPA's ExtendedTransform / ExtendedTransformChild."""

    def __init__(self, tr, parent: Optional["ExtTransform"] = None):
        self.tr = tr
        self.parent = parent

    @property
    def is_rect(self) -> bool:
        return type(self.tr).__name__ == "RectTransform"

    def local_scale(self) -> V2:
        return vec2(self.tr.m_LocalScale) if self.parent else V2(1, 1)

    def cumulative_scale(self) -> V2:
        return self.parent.cumulative_scale() if self.parent else V2(1, 1)

    def overall_scale(self) -> V2:
        return self.local_scale() * self.cumulative_scale()

    def unscaled_size(self) -> V2:
        if not self.is_rect:
            return V2()
        if not self.parent:
            return vec2(self.tr.m_SizeDelta)
        anchors = vec2(self.tr.m_AnchorMax) - vec2(self.tr.m_AnchorMin)
        return anchors * self.parent.size() + vec2(self.tr.m_SizeDelta)

    def size(self) -> V2:
        return self.unscaled_size() * self.overall_scale()

    def pivot_position(self) -> V2:
        if not self.parent:
            return self.size() * vec2(self.tr.m_Pivot) if self.is_rect else V2()
        p = self.parent
        if self.is_rect:
            if p.is_rect:
                anchors = vec2(self.tr.m_AnchorMax) - vec2(self.tr.m_AnchorMin)
                return (
                    anchors * p.size() * vec2(self.tr.m_Pivot)
                    + vec2(self.tr.m_AnchorMin) * p.size()
                    + p.origin()
                    + vec2(self.tr.m_AnchoredPosition) * self.cumulative_scale()
                )
            return p.pivot_position() + vec2(self.tr.m_AnchoredPosition) * self.cumulative_scale()
        return p.pivot_position() + vec2(self.tr.m_LocalPosition) * self.cumulative_scale()

    def origin(self) -> V2:
        if not self.parent:
            return V2()
        if not self.is_rect:
            raise ValueError("origin on non rect transform")
        return self.pivot_position() - vec2(self.tr.m_Pivot) * self.size()


# ---------------------------------------------------------------- data model
@dataclass
class Part:
    go_name: str
    name: str
    sprite: object
    mesh: object
    raw_size: V2
    transform: ExtTransform
    paste: V2 = field(init=False)
    size: V2 = field(init=False)
    unscaled: V2 = field(init=False)
    scale: V2 = field(init=False)
    mesh_bbox: Optional[Tuple[int, int, int, int]] = None
    _image: Optional[Image.Image] = field(default=None, repr=False, compare=False)
    _texture: Optional[Image.Image] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self.paste = self.transform.origin().rounded()
        self.size = self.transform.size()
        self.unscaled = self.transform.unscaled_size()
        self.scale = self.transform.overall_scale()


@dataclass
class FaceSlot:
    go_name: str
    name: str
    transform: ExtTransform
    paste: V2 = field(init=False)
    size: V2 = field(init=False)

    def __post_init__(self):
        self.paste = self.transform.origin().rounded()
        self.size = self.transform.size()


@dataclass
class Analysis:
    name: str                    # bundle path like painting/2b
    root_name: str
    parts: List[Part]
    face: Optional[FaceSlot]
    canvas: Tuple[int, int]      # native painting canvas (raw sprite size)
    native_scale: V2             # native px / UI unit
    env: object = None           # keep bundles alive
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- bundle IO
def pptr(base, d) -> PPtr:
    return PPtr(m_FileID=d["m_FileID"], m_PathID=d["m_PathID"], assetsfile=base.assetsfile)


def path_id(d) -> int:
    return int(d.get("m_PathID", 0)) if isinstance(d, dict) else 0


class AssetSource:
    """Loads bundles of one AssetBundles folder and caches the dependency table."""

    def __init__(self, root: str):
        self.root = root
        self.table: Dict[str, List[str]] = {}
        self._load_dependencies()

    def _load_dependencies(self):
        path = os.path.join(self.root, "dependencies")
        if not os.path.isfile(path):
            return
        env = UnityPy.load(path)
        for o in env.objects:
            if o.type.name == "MonoBehaviour":
                try:
                    tree = o.read_typetree()
                except Exception:
                    continue
                if "m_Keys" in tree:
                    for k, v in zip(tree["m_Keys"], tree["m_Values"]):
                        self.table[k] = list(v.get("m_Dependencies", []))
                    return

    def bundle_path(self, name: str) -> str:
        return os.path.join(self.root, *name.split("/"))

    def load_env(self, name: str, with_deps: bool = True):
        env = UnityPy.load(self.bundle_path(name))
        if with_deps:
            for dep in self.table.get(name, []):
                p = self.bundle_path(dep)
                if os.path.isfile(p):
                    try:
                        env.load_file(p)
                    except Exception:
                        pass
        return env

    def list_paintings(self) -> List[str]:
        return AssetSource.list_paintings_static(self.root)

    @staticmethod
    def list_paintings_static(root: str) -> List[str]:
        folder = os.path.join(root, "painting")
        if not os.path.isdir(folder):
            return []
        out = []
        for f in sorted(os.listdir(folder)):
            if f.endswith("_tex") or f.endswith(".manifest") or f.endswith(".resS"):
                continue
            if os.path.isfile(os.path.join(folder, f)):
                out.append(f)
        return out


# ---------------------------------------------------------------- analysis
def _game_objects(env):
    for o in env.objects:
        if o.type.name == "GameObject":
            yield o


def _bundle_root(env):
    """First GameObject exposed by the bundle (a prefab), if any."""
    for o in env.objects:
        if o.type.name == "AssetBundle":
            ab = o.read()
            for entry in list(ab.m_Container):
                try:
                    asset = entry[1].asset.read()
                except Exception:
                    continue
                if type(asset).__name__ == "GameObject":
                    return asset
    # a plain serialized file (no asset bundle wrapper)
    for o in env.objects:
        if o.type.name == "GameObject":
            try:
                return o.read()
            except Exception:
                continue
    return None


def _components(go):
    out = []
    for pair in go.m_Component:
        try:
            ptr = pair.component
            out.append((ptr, ptr.read()))
        except Exception:
            continue
    return out


def _mono_of(go):
    for ptr, obj in _components(go):
        if type(obj).__name__ == "MonoBehaviour":
            try:
                return ptr, ptr.read_typetree()
            except Exception:
                return ptr, None
    return None, None


def analyze(source: AssetSource, name: str, keep_env: bool = False) -> Analysis:
    env = source.load_env(name)
    root = _bundle_root(env)
    if root is None:
        raise RuntimeError("该文件不是立绘 prefab（没有 GameObject），可跳过")

    parts: List[Part] = []
    face: Optional[FaceSlot] = None
    warnings: List[str] = []

    def transform_of(go):
        for ptr, obj in _components(go):
            if type(obj).__name__ in ("Transform", "RectTransform"):
                return obj
        return None

    def walk(tr, parent_ext):
        nonlocal face
        try:
            go = tr.m_GameObject.read()
        except Exception:
            return
        go_name = go.m_Name
        ext = ExtTransform(tr, parent_ext)

        if go_name == FACE_GO_NAME and face is None:
            face = FaceSlot(go_name, "$face$", ext)
        else:
            banned = False
            node = ext
            while node is not None:
                try:
                    if node.tr.m_GameObject.read().m_Name in BANNED_NAMES:
                        banned = True
                        break
                except Exception:
                    pass
                node = node.parent
            if not banned:
                ptr, tree = _mono_of(go)
                if tree and "m_Sprite" in tree and path_id(tree["m_Sprite"]):
                    try:
                        sprite = pptr(ptr, tree["m_Sprite"]).read()
                    except Exception:
                        sprite = None
                    if sprite is not None and (path_id(tree.get("mMesh", {})) or sprite.m_Name != BUILTIN_UI_SPRITE):
                        mesh = None
                        if path_id(tree.get("mMesh", {})):
                            try:
                                mesh = pptr(ptr, tree["mMesh"]).read()
                            except Exception:
                                mesh = None
                        raw = compute_raw_size(ptr, tree, sprite)
                        part = Part(go_name, sprite.m_Name, sprite, mesh, raw, ext)
                        if mesh is not None:
                            part.mesh_bbox = mesh_bbox(mesh)
                        parts.append(part)
        for child in tr.m_Children:
            try:
                walk(child.read(), ext)
            except Exception:
                pass

    root_tr = transform_of(root)
    if root_tr is None:
        raise RuntimeError("根节点没有 Transform")
    walk(root_tr, None)

    if not parts:
        raise RuntimeError("没有找到可还原的立绘部件")

    # native canvas: the coordinate space shared by the painting meshes
    root_part = next((p for p in parts if p.go_name == root.m_Name), None)
    primary = root_part or max(parts, key=lambda p: p.raw_size.x * p.raw_size.y)
    canvas = (int(round(primary.raw_size.x)), int(round(primary.raw_size.y)))
    if canvas[0] <= 0 or canvas[1] <= 0:
        raise RuntimeError("画布尺寸无效")

    rect = primary.size
    scale = V2(canvas[0] / rect.x if rect.x else 1.0, canvas[1] / rect.y if rect.y else 1.0)

    return Analysis(name, root.m_Name, parts, face, canvas, scale, env if keep_env else None, warnings)


def compute_raw_size(mono_ptr, tree, sprite) -> V2:
    raw = tree.get("mRawSpriteSize")
    if isinstance(raw, dict) and "x" in raw:
        return V2(raw["x"], raw["y"])
    try:
        tex = sprite.m_RD.texture.read()
        return V2(tex.m_Width, tex.m_Height)
    except Exception:
        return V2(1, 1)


def mesh_bbox(mesh) -> Optional[Tuple[int, int, int, int]]:
    try:
        handler = MeshHandler(mesh)
        handler.process()
        verts = handler.m_Vertices or []
        if not verts:
            return None
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        return int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1
    except Exception:
        return None


# ---------------------------------------------------------------- rebuilding
def texture_image(sprite) -> Optional[Image.Image]:
    try:
        tex = sprite.m_RD.texture.read()
        if tex is None:
            return None
        img = tex.image
        if img is None:
            return None
        return img.convert("RGBA")
    except Exception:
        return None


def rebuild_part(part: Part) -> Optional[Image.Image]:
    """Rebuild one MeshImage part as a y-down image (cached per part)."""
    if part._image is not None:
        return part._image
    tex = part._texture if part._texture is not None else texture_image(part.sprite)
    part._texture = tex
    if part.mesh is None or tex is None:
        part._image = tex
        return tex

    try:
        handler = MeshHandler(part.mesh)
        handler.process()
    except Exception:
        return tex

    verts = handler.m_Vertices or []
    uvs = handler.m_UV0 or []
    if not verts or not uvs or len(verts) != len(uvs):
        return tex

    tris = []
    try:
        for group in handler.get_triangles():
            tris.extend(group)
    except Exception:
        tris = []
    if not tris:
        return tex

    max_x = int(max(v[0] for v in verts)) + 1
    max_y = int(max(v[1] for v in verts)) + 1
    width, height = max_x, max_y
    if width <= 0 or height <= 0:
        return tex

    tw, th = tex.size
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for tri in tris:
        try:
            vx = [verts[i] for i in tri]
            uv = [uvs[i] for i in tri]
        except IndexError:
            continue
        x0 = int(min(v[0] for v in vx))
        x1 = int(max(v[0] for v in vx))
        y0 = int(min(v[1] for v in vx))
        y1 = int(max(v[1] for v in vx))
        if x1 <= x0 or y1 <= y0:
            continue
        dw, dh = x1 - x0, y1 - y0
        u0 = min(u[0] for u in uv)
        u1 = max(u[0] for u in uv)
        v0 = min(u[1] for u in uv)
        v1 = max(u[1] for u in uv)
        sx0 = max(0, min(tw, int(round(u0 * tw))))
        sx1 = max(0, min(tw, int(round(u1 * tw))))
        sy0 = max(0, min(th, int(round((1.0 - v1) * th))))
        sy1 = max(0, min(th, int(round((1.0 - v0) * th))))
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        crop = tex.crop((sx0, sy0, sx1, sy1))
        if crop.size != (dw, dh):
            crop = crop.resize((dw, dh), Image.LANCZOS)
        # mesh coordinates are y-up, images are y-down
        dest_y = height - 1 - y1
        if dest_y < 0 or dest_y + dh > height:
            continue
        out.alpha_composite(crop, (x0, dest_y))
    part._image = out
    return out


def select_parts(analysis: Analysis) -> List[Part]:
    """All painting layers of the prefab (background, character, overlays)."""
    return list(analysis.parts)


def layout(analysis: Analysis, scale: float) -> Tuple[int, int, V2]:
    """Canvas size and origin for the given render scale."""
    items = [p for p in analysis.parts]
    if analysis.face is not None:
        items = items + [analysis.face]
    left = min(i.paste.x for i in items)
    bottom = min(i.paste.y for i in items)
    right = max(i.paste.x + i.size.x for i in items)
    top = max(i.paste.y + i.size.y for i in items)
    w = max(1, int(round((right - left) * scale)))
    h = max(1, int(round((top - bottom) * scale)))
    return w, h, V2(left, bottom)


def auto_scale(analysis: Analysis) -> float:
    """Scale that renders the painting at its native texture resolution."""
    root = next((p for p in analysis.parts if p.go_name == analysis.root_name), None)
    if root is None or root.size.x <= 0:
        return 1.0
    return min(1.0, analysis.canvas[0] / root.size.x)


def pad_to_raw(part: Part, img: Image.Image) -> Image.Image:
    tw = max(1, int(round(part.raw_size.x)))
    th = max(1, int(round(part.raw_size.y)))
    if img.width >= tw and img.height >= th:
        return img
    canvas = Image.new("RGBA", (max(tw, img.width), max(th, img.height)), (0, 0, 0, 0))
    # ALPA pads in y-up space, which is the bottom of the final image
    canvas.alpha_composite(img, (0, canvas.height - img.height))
    return canvas


MAX_RENDER_PIXELS = 36_000_000


def effective_scale(analysis: Analysis, scale: float) -> float:
    """Clamp absurd canvases (some prefabs use a 8k+ layout canvas)."""
    w, h, _ = layout(analysis, scale)
    pixels = w * h
    if pixels <= MAX_RENDER_PIXELS:
        return scale
    return scale * (MAX_RENDER_PIXELS / pixels) ** 0.5


def compose_base(analysis: Analysis, scale: float = 1.0) -> Optional[Image.Image]:
    """Compose every painting layer in UI layout space (ALPA compatible)."""
    scale = effective_scale(analysis, scale)
    w, h, origin = layout(analysis, scale)
    canvas = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    used = 0
    for part in analysis.parts:
        img = rebuild_part(part)
        if img is None:
            continue
        img = pad_to_raw(part, img)
        dw = max(1, int(round(part.size.x * scale)))
        dh = max(1, int(round(part.size.y * scale)))
        if img.size != (dw, dh):
            img = img.resize((dw, dh), Image.LANCZOS)
        x = int(round((part.paste.x - origin.x) * scale))
        y = int(round((part.paste.y - origin.y) * scale))
        canvas.alpha_composite(img, (x, h - y - dh))
        used += 1
    if used == 0:
        return None
    return canvas


def face_layout(analysis: Analysis, scale: float, offset: Tuple[int, int] = (0, 0)):
    """Where the face patch goes: (x, y, w, h, canvas_w, canvas_h) in image pixels.

    ``offset`` is (right, up) in final-image pixels, matching ALPA's spinner
    semantics (纵向 +1 表示往上一个像素)。
    """
    if analysis.face is None:
        return None
    scale = effective_scale(analysis, scale)
    w, h, origin = layout(analysis, scale)
    slot = analysis.face
    dw = max(1, int(round(slot.size.x * scale)))
    dh = max(1, int(round(slot.size.y * scale)))
    x = int(round((slot.paste.x - origin.x) * scale)) + int(offset[0])
    y = int(round((slot.paste.y - origin.y) * scale)) - int(offset[1])
    return x, h - y - dh, dw, dh, w, h


def compose_face(analysis: Analysis, face_img: Image.Image, scale: float = 1.0,
                 crop: bool = False, base: Optional[Image.Image] = None,
                 offset: Tuple[int, int] = (0, 0)) -> Optional[Image.Image]:
    if base is None:
        base = compose_base(analysis, scale)
    if base is None or analysis.face is None or face_img is None:
        return base
    rect = face_layout(analysis, scale, offset)
    if rect is None:
        return base
    x, y, dw, dh, _, _ = rect
    img = face_img if face_img.size == (dw, dh) else face_img.resize((dw, dh), Image.LANCZOS)
    out = base.copy()
    out.alpha_composite(img, (x, y))
    if crop:
        out = crop_alpha(out)
    return out


def crop_alpha(img: Image.Image) -> Image.Image:
    bbox = img.getbbox()
    if bbox and bbox[0] >= 0:
        return img.crop(bbox)
    return img


# ---------------------------------------------------------------- faces
FACE_SPLIT_RE = re.compile(r"^(.+?)(_[hg]\d*|_alter|_younv|_n|_rw\d*|_bj\d*|_hei|_wjz|_doa|_idol|_front|_mid|_tolove|_shophx|_buildhx|_\d+|_ex|_tx\d+|_jz\d*|_renwu|_memory|_wjz)+$")


def face_bundle_name(painting: str) -> str:
    """Port of ALPA's rule: take first two '_' chunks, drop a trailing _n."""
    chunks = painting.split("_", 2)
    base = "_".join(chunks[:2]) if len(chunks) > 1 else chunks[0]
    base = base.replace("_n", "")
    return base


def load_faces(source: AssetSource, painting: str) -> List[Tuple[str, Image.Image]]:
    names = [face_bundle_name(painting)]
    if names[0] != painting:
        names.append(painting)
    for candidate in names:
        path = source.bundle_path("paintingface/" + candidate)
        if not os.path.isfile(path):
            continue
        try:
            env = UnityPy.load(path)
        except Exception:
            continue
        out: List[Tuple[str, Image.Image]] = []
        for o in env.objects:
            if o.type.name != "Sprite":
                continue
            try:
                spr = o.read()
                img = sprite_image(spr)
            except Exception:
                continue
            if img is not None:
                out.append((spr.m_Name, img))
        if out:
            out.sort(key=lambda item: face_sort_key(item[0]))
            return out
    return []


def count_faces(root: str, painting: str) -> int:
    """Number of expression sprites in the matching paintingface bundle."""
    for candidate in (face_bundle_name(painting), painting):
        path = os.path.join(root, "paintingface", candidate)
        if not os.path.isfile(path):
            continue
        try:
            env = UnityPy.load(path)
            return sum(1 for o in env.objects if o.type.name == "Sprite")
        except Exception:
            return 0
    return 0


def face_names_in_bundle(root: str, painting: str) -> List[str]:
    """Expression sprite names of the matching paintingface bundle (sorted)."""
    for candidate in (face_bundle_name(painting), painting):
        path = os.path.join(root, "paintingface", candidate)
        if not os.path.isfile(path):
            continue
        try:
            env = UnityPy.load(path)
            names: List[str] = []
            for obj in env.objects:
                if obj.type.name != "Sprite":
                    continue
                try:
                    names.append(obj.read().m_Name)
                except Exception:
                    continue
            names.sort(key=face_sort_key)
            return names
        except Exception:
            return []
    return []


def face_sort_key(name: str):
    """Natural order so expressions read 1, 2, 3 … 10 instead of 1, 10, 2."""
    m = re.match(r"^(\d+)$", name)
    if m:
        return (0, int(m.group(1)), name)
    m = re.match(r"^(\d+)", name)
    if m:
        return (1, int(m.group(1)), name)
    return (2, 0, name)


def sprite_image(sprite) -> Optional[Image.Image]:
    try:
        rd = sprite.m_RD
        offset = rd.atlasRectOffset
        if offset is not None and offset.x == -1 and offset.y == -1:
            return texture_image(sprite)
        tex = rd.texture.read()
        img = tex.image.convert("RGBA")
        rect = rd.textureRect
        x, y, w, h = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
        th = img.height
        box = (x, th - (y + h), x + w, th - y)
        return img.crop(box)
    except Exception:
        try:
            return texture_image(sprite)
        except Exception:
            return None


# ---------------------------------------------------------------- export
def export_painting(source: AssetSource, painting: str, out_dir: str,
                    export_faces: bool = True, export_parts: bool = False,
                    crop: bool = False, png_level: int = 6,
                    native_scale: bool = True,
                    face_names: Optional[List[str]] = None,
                    face_offset: Tuple[int, int] = (0, 0),
                    first_face_only: bool = False,
                    export_base: bool = False,
                    overwrite: bool = True) -> List[str]:
    """Export one painting.

    ``face_names`` selects what to write: ``"0"`` means the expression-free
    base image, ``"1"``/``"2"``… are the expression files, an empty list
    falls back to the base image.  ``None`` keeps the batch behaviour (all
    expressions); ``export_base`` forces the base on top of that.
    """
    analysis = analyze(source, "painting/" + painting)
    scale = auto_scale(analysis) if native_scale else 1.0
    saved: List[str] = []
    faces = load_faces(source, painting) if export_faces else []
    wanted = None if face_names is None else {str(n) for n in face_names}
    if wanted is not None:
        faces = [(n, img) for n, img in faces if n in wanted]
    elif first_face_only:
        faces = faces[:1]

    if export_base or not export_faces:
        want_base = True
    elif wanted is None:
        want_base = not faces
    else:
        want_base = "0" in wanted or not wanted
    if not want_base and not faces:
        return []

    os.makedirs(out_dir, exist_ok=True)
    painting_dir = os.path.join(out_dir, painting)
    os.makedirs(painting_dir, exist_ok=True)
    base = compose_base(analysis, scale)
    if base is None:
        raise RuntimeError("立绘为空（贴图或网格缺失）")
    if want_base:
        path = os.path.join(painting_dir, painting + ".png")
        if overwrite or not os.path.isfile(path):
            (crop_alpha(base) if crop else base).save(path, compress_level=png_level)
            saved.append(path)

    if faces:
        face_dir = painting_dir
        for name, img in faces:
            out = compose_face(analysis, img, scale, crop, base=base, offset=face_offset)
            if out is None:
                continue
            path = os.path.join(face_dir, f"{painting}_{safe_name(name)}.png")
            if not overwrite and os.path.isfile(path):
                continue
            out.save(path, compress_level=png_level)
            saved.append(path)

    if export_parts:
        parts_dir = os.path.join(painting_dir, "parts")
        os.makedirs(parts_dir, exist_ok=True)
        for p in analysis.parts:
            img = rebuild_part(p)
            if img is None:
                continue
            path = os.path.join(parts_dir, f"{safe_name(p.name)}.png")
            img.save(path, compress_level=png_level)
            saved.append(path)

    analysis.env = None
    return saved


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name)
