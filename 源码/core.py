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
            # a plain Transform has no pivot / size, its position is the origin
            return self.pivot_position()
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
    _image: Optional[Image.Image] = field(default=None, repr=False, compare=False)
    _texture: Optional[Image.Image] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self.paste = self.transform.origin().rounded()
        self.size = self.transform.size()


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


# ---------------------------------------------------------------- bundle IO
def pptr(base, d) -> PPtr:
    return PPtr(m_FileID=d["m_FileID"], m_PathID=d["m_PathID"], assetsfile=base.assetsfile)


def path_id(d) -> int:
    return int(d.get("m_PathID", 0)) if isinstance(d, dict) else 0


# ---------------------------------------------------------------- list order
# 「版本后缀」：同一个皮肤的多个版本，可组合出现（如 2b_n_hx）
SKIN_VARIANT_TOKENS = ("_shophx", "_buildhx", "_n_hx", "_hx", "_n")
# 组内显示顺序：原版 → _n → _hx → _n_hx → 遮挡层版本
SKIN_VARIANT_RANK = {
    "": 0,
    "_n": 1,
    "_hx": 2,
    "_n_hx": 3,
    "_buildhx": 4,
    "_n_buildhx": 5,
    "_shophx": 6,
    "_n_shophx": 7,
}


def split_skin_name(name: str) -> Tuple[str, str]:
    """把 ``2b_n_hx`` 拆成 ``("2b", "_n_hx")``。

    只有 n / hx / buildhx / shophx 才算同一皮肤的「版本」，
    其它后缀（``_2`` ``_3`` ``_alter`` ``_doa`` …）都是不同的皮肤。
    """
    low = name.lower()
    end = len(low)
    while True:
        for token in SKIN_VARIANT_TOKENS:
            if end > len(token) and low[:end].endswith(token):
                end -= len(token)
                break
        else:
            break
    return name[:end], low[end:]


def skin_base_key(base: str) -> tuple:
    """按 ``_`` 分段比较：``abeikelongbi_2`` 排在 ``_3`` 前，``_2`` 排在 ``_10`` 前。"""
    return tuple(
        (0, int(seg), "") if seg.isdigit() else (1, 0, seg)
        for seg in base.casefold().split("_")
    )


def skin_sort_key(name: str) -> Tuple[tuple, int, str, str]:
    """列表排序键：同一皮肤的各版本相邻，数字字段按数值比较。"""
    base, variant = split_skin_name(name)
    return (
        skin_base_key(base),
        SKIN_VARIANT_RANK.get(variant, 100 + len(variant)),
        base.casefold(),
        name.casefold(),
    )


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

    @staticmethod
    def list_paintings_static(root: str) -> List[str]:
        folder = os.path.join(root, "painting")
        if not os.path.isdir(folder):
            return []
        out = []
        for f in sorted(os.listdir(folder), key=skin_sort_key):
            if f.endswith("_tex") or f.endswith(".manifest") or f.endswith(".resS"):
                continue
            if os.path.isfile(os.path.join(folder, f)):
                out.append(f)
        return out


# ---------------------------------------------------------------- analysis
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


def analyze(source: AssetSource, name: str) -> Analysis:
    env = source.load_env(name)
    root = _bundle_root(env)
    if root is None:
        raise RuntimeError("该文件不是立绘 prefab（没有 GameObject），可跳过")

    parts: List[Part] = []
    face: Optional[FaceSlot] = None

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
                        parts.append(Part(go_name, sprite.m_Name, sprite, mesh, raw, ext))
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

    return Analysis(name, root.m_Name, parts, face, canvas)


def compute_raw_size(mono_ptr, tree, sprite) -> V2:
    raw = tree.get("mRawSpriteSize")
    if isinstance(raw, dict) and "x" in raw:
        return V2(raw["x"], raw["y"])
    try:
        tex = sprite.m_RD.texture.read()
        return V2(tex.m_Width, tex.m_Height)
    except Exception:
        return V2(1, 1)


# ---------------------------------------------------------------- overlays
# 「和谐遮挡层」：prefab 里额外挂的一层贴纸（建造/商店预览用），不是皮肤本体。
# 判定看 GameObject 名或它用的贴图名是否以这两个后缀结尾。
OVERLAY_SUFFIXES = ("_buildhx", "_shophx")


def is_overlay_part(part: "Part") -> bool:
    for name in (getattr(part, "go_name", ""), getattr(part, "name", "")):
        if name and name.lower().endswith(OVERLAY_SUFFIXES):
            return True
    return False


def drop_overlays(analysis: "Analysis") -> "Analysis":
    """把遮挡层从图层列表里摘掉（画布尺寸也会跟着收紧）。"""
    analysis.parts = [p for p in analysis.parts if not is_overlay_part(p)]
    return analysis


# ---------------------------------------------------------------- rebuilding
def blend(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    """Composite ``src`` onto ``dst`` at (x, y), dropping what falls outside.

    Layout rounding and the face-offset spinner can push a layer past the
    left/top edge. Pillow tolerates that by compositing over a padded crop,
    but the behaviour is subtle; clipping here is explicit and costs one
    crop only in the rare out-of-bounds case.
    """
    cx, cy = max(0, -x), max(0, -y)
    if cx >= src.width or cy >= src.height:
        return
    if cx or cy:
        src = src.crop((cx, cy, src.width, src.height))
    dst.alpha_composite(src, (x + cx, y + cy))


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

    def fall_back(img):
        """Keep the plain texture when the mesh cannot be used."""
        part._image = img
        return img

    try:
        handler = MeshHandler(part.mesh)
        handler.process()
    except Exception:
        return fall_back(tex)

    verts = handler.m_Vertices or []
    uvs = handler.m_UV0 or []
    if not verts or not uvs or len(verts) != len(uvs):
        return fall_back(tex)

    tris = []
    try:
        for group in handler.get_triangles():
            tris.extend(group)
    except Exception:
        tris = []
    if not tris:
        return fall_back(tex)

    max_x = int(max(v[0] for v in verts)) + 1
    max_y = int(max(v[1] for v in verts)) + 1
    width, height = max_x, max_y
    if width <= 0 or height <= 0:
        return fall_back(tex)

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
        blend(out, crop, x0, height - 1 - y1)
    part._image = out
    return out


def _bounds(analysis: Analysis) -> Tuple[float, float, float, float]:
    """Bounding box of every layer in UI space: (left, bottom, right, top)."""
    items: List = list(analysis.parts)
    if analysis.face is not None:
        items.append(analysis.face)
    left = min(i.paste.x for i in items)
    bottom = min(i.paste.y for i in items)
    right = max(i.paste.x + i.size.x for i in items)
    top = max(i.paste.y + i.size.y for i in items)
    return left, bottom, right, top


def _root_part(analysis: Analysis) -> Optional[Part]:
    return next((p for p in analysis.parts if p.go_name == analysis.root_name), None)


# 输出尺寸与原生纹理相差不超过这个像素数时，直接取纹理尺寸（避免莫名多出 1px）
CANVAS_SNAP = 2
# 声明尺寸与图层矩形的长宽比偏差超过这个比例，就认为 mRawSpriteSize 不可信
ASPECT_TOLERANCE = 0.10
# 极端 prefab 的兜底上限：正常立绘远小于此值，触发时说明布局异常
MAX_RENDER_PIXELS = 36_000_000


def fit(analysis: Analysis, native: bool = True) -> Tuple[float, int, int, V2]:
    """输出帧：(scale, w, h, origin)。

    ``native=True``（默认）原生纹理 1:1：主图层的「UI 单位 → 像素」比例由它自己的
    原始纹理宽度决定，画布吸附到纹理尺寸，整体不放大不缩小。
    ``native=False`` 游戏布局：按 prefab 的 UI 坐标 1:1 输出，画面更大，但背景是
    小纹理上采样得到（详见 README 的「两种输出尺寸」）。
    """
    left, bottom, right, top = _bounds(analysis)
    raw_w = max(1.0, right - left)
    raw_h = max(1.0, top - bottom)

    if not native:
        scale = 1.0
        pixels = raw_w * raw_h
        if pixels > MAX_RENDER_PIXELS:  # pragma: no cover - 布局异常时的兜底
            scale = (MAX_RENDER_PIXELS / pixels) ** 0.5
        w = max(1, int(round(raw_w * scale)))
        h = max(1, int(round(raw_h * scale)))
        return scale, w, h, V2(left, bottom)

    root = _root_part(analysis)
    if root is not None and root.size.x > 0:
        scale = analysis.canvas[0] / root.size.x
    else:
        scale = 1.0
    w = max(1, int(round(raw_w * scale)))
    h = max(1, int(round(raw_h * scale)))
    cw, ch = analysis.canvas
    if abs(w - cw) <= CANVAS_SNAP:
        w = cw
    if abs(h - ch) <= CANVAS_SNAP:
        h = ch
    pixels = w * h
    if pixels > MAX_RENDER_PIXELS:  # pragma: no cover - 布局异常时的兜底
        k = (MAX_RENDER_PIXELS / pixels) ** 0.5
        scale *= k
        w = max(1, int(w * k))
        h = max(1, int(h * k))
    return scale, w, h, V2(left, bottom)


def pad_to_raw(part: Part, img: Image.Image) -> Image.Image:
    """把网格图垫到 sprite 声明尺寸，使它在图层矩形里对齐。

    只有「声明尺寸比网格图大、且长宽比与图层矩形一致」时才垫：正常立绘的贴图边缘
    会多出几十像素，这一垫能让图层在矩形里对齐。个别 prefab（如 i404 的背景层）
    把几个不同网格的 ``mRawSpriteSize`` 写成同一个值，长宽比与矩形明显不符；这时
    按网格图原样使用，否则会被垫到错误画布再缩放，导致整层错位或出现空洞。
    """
    tw = max(1, int(round(part.raw_size.x)))
    th = max(1, int(round(part.raw_size.y)))
    if img.width >= tw and img.height >= th:
        return img
    rect_aspect = part.size.x / part.size.y if part.size.y else 0.0
    if rect_aspect > 0 and abs((tw / th) / rect_aspect - 1.0) > ASPECT_TOLERANCE:
        return img
    canvas = Image.new("RGBA", (max(tw, img.width), max(th, img.height)), (0, 0, 0, 0))
    # ALPA pads in y-up space, which is the bottom of the final image
    blend(canvas, img, 0, canvas.height - img.height)
    return canvas


def layer_size(part: Part, scale: float) -> Tuple[int, int]:
    """像素尺寸：布局比例换算后，与纹理尺寸相差 2px 内时直接用纹理尺寸。

    立绘 prefab 的 RectTransform 与贴图尺寸常常差零点几像素，直接按布局换算
    会让整层被 LANCZOS 重采样一遍（2048→2047 这种），这里吸附回纹理尺寸，
    让图层保持 1:1；只有真正的缩放层（如 _rw 人物层）才会重采样。
    """
    dw = max(1, int(round(part.size.x * scale)))
    dh = max(1, int(round(part.size.y * scale)))
    tw = max(1, int(round(part.raw_size.x)))
    th = max(1, int(round(part.raw_size.y)))
    if abs(dw - tw) <= CANVAS_SNAP:
        dw = tw
    if abs(dh - th) <= CANVAS_SNAP:
        dh = th
    return dw, dh


def compose_base(analysis: Analysis, native: bool = True) -> Optional[Image.Image]:
    """Compose every painting layer, at native texture size or game layout size."""
    scale, w, h, origin = fit(analysis, native)
    canvas = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    used = 0
    for part in analysis.parts:
        img = rebuild_part(part)
        if img is None:
            continue
        img = pad_to_raw(part, img)
        dw, dh = layer_size(part, scale)
        if img.size != (dw, dh):
            img = img.resize((dw, dh), Image.LANCZOS)
        x = int(round((part.paste.x - origin.x) * scale))
        y = int(round((part.paste.y - origin.y) * scale))
        blend(canvas, img, x, h - y - dh)
        used += 1
    if used == 0:
        return None
    return canvas


def face_layout(analysis: Analysis, offset: Tuple[int, int] = (0, 0),
                native: bool = True):
    """Where the face patch goes: (x, y, w, h, canvas_w, canvas_h) in image pixels.

    ``offset`` is (right, up) in final-image pixels, matching ALPA's spinner
    semantics (纵向 +1 表示往上一个像素)。坐标可能为负，绘制时由 ``blend`` 裁剪。
    """
    if analysis.face is None:
        return None
    scale, w, h, origin = fit(analysis, native)
    slot = analysis.face
    dw_f = slot.size.x * scale
    dh_f = slot.size.y * scale
    dw = max(1, int(round(dw_f)))
    dh = max(1, int(round(dh_f)))
    # 直接对「矩形上边缘」取整，比先取整位置再减高度更接近真实位置
    x_f = (slot.paste.x - origin.x) * scale
    top_f = h - (slot.paste.y - origin.y + FACE_SLOT_Y_BIAS) * scale - dh_f
    # +offset[1] 表示往上
    return (int(round(x_f)) + int(offset[0]),
            int(round(top_f)) - int(offset[1]), dw, dh, w, h)


def compose_face(analysis: Analysis, face_img: Image.Image, crop: bool = False,
                 base: Optional[Image.Image] = None,
                 offset: Tuple[int, int] = (0, 0),
                 native: bool = True) -> Optional[Image.Image]:
    if base is None:
        base = compose_base(analysis, native)
    if base is None or analysis.face is None or face_img is None:
        return base
    rect = face_layout(analysis, offset, native)
    if rect is None:
        return base
    x, y, dw, dh, _, _ = rect
    img = face_img if face_img.size == (dw, dh) else face_img.resize((dw, dh), Image.LANCZOS)
    out = base.copy()
    blend(out, img, x, y)
    if crop:
        out = crop_alpha(out)
    return out


def crop_alpha(img: Image.Image) -> Image.Image:
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


# ---------------------------------------------------------------- faces
# 表情槽的 y 比实际内容低 1 个 UI 单位（RectTransform 的半开区间），不补偿的话
# 贴图会整体下沉：把表情 sprite 与 rw 纹理按 1:1 像素比对，30 个样本里 28 个
# 都差 1 像素，补偿后全部归零。
FACE_SLOT_Y_BIAS = 1.0

# 立绘名尾部的标记：_n = 无装备差分（与本体检共用表情），
# _hx = 特触 / 换装差分（可能自带一套表情，见 paintingface 里同名的 bundle）
FACE_MARKERS = ("_n", "_hx")


def _split_face_markers(painting: str) -> Tuple[str, List[str]]:
    """拆出尾部的标记：``a_n_hx`` -> ``("a", ["_n", "_hx"])``。"""
    base, tokens = painting, []
    while True:
        for marker in FACE_MARKERS:
            if base.endswith(marker):
                base = base[: -len(marker)]
                tokens.insert(0, marker)
                break
        else:
            return base, tokens


def face_bundle_candidates(painting: str) -> List[str]:
    """paintingface 里可能的 bundle 名，从最具体到最宽泛。

    立绘名与表情 bundle 名基本一一对应（``tiancheng_cv_2`` 用
    ``paintingface/tiancheng_cv_2``），只有两种差分要换算：

    * ``_n`` 只是「无装备」，表情与本体完全相同 —— ``2b_n`` 用 ``2b``；
    * ``_hx`` 若在 paintingface 里有自己的 bundle 就用它（``abuluqi_2_hx``），
      没有才回落到本体（``2b_hx`` -> ``2b``）。

    所以顺序是：原名 → 去掉 ``_n`` → 去掉全部标记 → 再从右往左逐段回退
    （``_ex`` / ``_asmr`` / ``_hei`` / ``_dark_shadow`` 这类修饰词没有独立表情组时，
    沿用本体那一套）。
    """
    base, tokens = _split_face_markers(painting)
    names = [painting]
    if "_n" in tokens:
        names.append(base + "".join(t for t in tokens if t != "_n"))
    names.append(base)
    # 兜底：逐段去掉尾部修饰词
    tail = base
    while "_" in tail:
        tail = tail.rsplit("_", 1)[0]
        names.append(tail)

    out: List[str] = []
    for name in names:
        if name and name not in out:
            out.append(name)
    return out


def face_bundle_name(painting: str) -> str:
    """最宽泛的候选名（本体名），用于兜底判断。"""
    return face_bundle_candidates(painting)[-1]


# 表情槽尺寸 / AssetSource 缓存：解析依赖表很贵，不能每次重建
_FACE_SLOT_CACHE: dict = {}
_SOURCE_CACHE: dict = {}


def _cached_source(root: str) -> "AssetSource":
    source = _SOURCE_CACHE.get(root)
    if source is None:
        source = AssetSource(root)
        _SOURCE_CACHE[root] = source
    return source


def slot_of(analysis: "Analysis") -> Optional[Tuple[int, int]]:
    """已解析立绘的表情槽尺寸（没有脸槽则 ``None``）。"""
    face = analysis.face
    if face is None:
        return None
    return int(round(face.size.x)), int(round(face.size.y))


def face_slot_size(root: str, painting: str) -> Optional[Tuple[int, int]]:
    """立绘的差分表情槽尺寸（UI 单位），用于确认某个 bundle 到底对不对得上。"""
    key = (root, painting)
    if key in _FACE_SLOT_CACHE:
        return _FACE_SLOT_CACHE[key]
    size = None
    try:
        size = slot_of(analyze(_cached_source(root), "painting/" + painting))
    except Exception:
        size = None
    _FACE_SLOT_CACHE[key] = size
    return size


def _same_slot(a, b) -> bool:
    if not a or not b:
        return False
    return all(abs(x - y) <= max(1, round(y * 0.03)) for x, y in zip(a, b))


def face_bundle_exists(root: str, painting: str) -> bool:
    """只判断 paintingface 里有没有候选文件。

    列表刷 4000 多行时用这个：``face_bundle_path`` 为了确认是同一张脸要去解析
    prefab 的脸槽，单个几十到上百毫秒，全量跑一遍要一分半以上，会把窗口卡死。
    """
    folder = os.path.join(root, "paintingface")
    return any(os.path.isfile(os.path.join(folder, c))
               for c in face_bundle_candidates(painting))


def face_bundle_path(root: str, painting: str,
                     slot: Optional[Tuple[int, int]] = None) -> Optional[str]:
    """paintingface 里与该立绘匹配的 bundle 路径，没有则 ``None``。

    同名 / ``_n`` / ``_hx`` 是同一人物的差分，可以直接换算；再往宽泛回退时
    必须核对表情槽尺寸——不同皮肤（``tiancheng_cv_2`` 与 ``tiancheng_cv``）
    脸槽大小不同，套用别人的表情就是花脸。

    只有「两边脸槽都拿得到且明显不一致」才判定为不匹配；拿不到（对方 prefab
    不在这个版本里、或本身不是 prefab）时按原来的行为沿用，避免平白丢掉表情。
    """
    base, tokens = _split_face_markers(painting)
    direct = {painting}
    if "_n" in tokens:
        direct.add(base + "".join(t for t in tokens if t != "_n"))

    mine = slot
    for candidate in face_bundle_candidates(painting):
        path = os.path.join(root, "paintingface", candidate)
        if not os.path.isfile(path):
            continue
        if candidate in direct:
            return path
        if mine is None:
            mine = face_slot_size(root, painting)
        theirs = face_slot_size(root, candidate)
        if mine is None or theirs is None or _same_slot(mine, theirs):
            return path
    return None


def _face_bundle(root: str, painting: str,
                 slot: Optional[Tuple[int, int]] = None) -> Optional[str]:
    """兼容旧调用名。"""
    return face_bundle_path(root, painting, slot)


def load_faces(source: AssetSource, painting: str,
               wanted: Optional[set] = None,
               slot: Optional[Tuple[int, int]] = None) -> List[Tuple[str, Image.Image]]:
    """Expression sprites of a painting, sorted by expression number.

    ``wanted`` skips decoding sprites we are not going to write out — the
    texture decode is the expensive part of an export.
    """
    path = face_bundle_path(source.root, painting, slot)
    if path is None:
        return []
    try:
        env = UnityPy.load(path)
    except Exception:
        return []
    out: List[Tuple[str, Image.Image]] = []
    for o in env.objects:
        if o.type.name != "Sprite":
            continue
        try:
            spr = o.read()
            if wanted is not None and spr.m_Name not in wanted:
                continue
            img = sprite_image(spr)
        except Exception:
            continue
        if img is not None:
            out.append((spr.m_Name, img))
    out.sort(key=lambda item: face_sort_key(item[0]))
    return out


def face_names_in_bundle(root: str, painting: str) -> List[str]:
    """Expression sprite names of the matching paintingface bundle (sorted)."""
    path = _face_bundle(root, painting)
    if path is None:
        return []
    try:
        env = UnityPy.load(path)
        names = []
        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            try:
                names.append(obj.read().m_Name)
            except Exception:
                continue
    except Exception:
        return []
    names.sort(key=face_sort_key)
    return names


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
                    native: bool = True,
                    face_names: Optional[List[str]] = None,
                    face_offset: Tuple[int, int] = (0, 0),
                    export_base: bool = False,
                    overwrite: bool = True,
                    keep_overlay: bool = False) -> Tuple[List[str], int]:
    """Export one painting at native texture resolution.

    ``face_names`` selects what to write: ``"0"`` means the expression-free
    base image, ``"1"``/``"2"``… are the expression files, an empty list
    falls back to the base image.  ``None`` keeps the batch behaviour (all
    expressions); ``export_base`` forces the base on top of that.

    ``keep_overlay`` 为 False 时跳过 ``_buildhx`` / ``_shophx`` 这类和谐遮挡层。

    Returns ``(written files, number of files skipped because they existed)``.
    """
    analysis = analyze(source, "painting/" + painting)
    if not keep_overlay:
        drop_overlays(analysis)
    wanted = None if face_names is None else {str(n) for n in face_names}
    faces = (load_faces(source, painting, wanted, slot_of(analysis))
             if export_faces else [])

    if export_base or not export_faces:
        want_base = True
    elif wanted is None:
        want_base = not faces
    else:
        want_base = "0" in wanted or not wanted
    if not want_base and not faces:
        return [], 0

    os.makedirs(out_dir, exist_ok=True)
    painting_dir = os.path.join(out_dir, painting)
    os.makedirs(painting_dir, exist_ok=True)

    saved: List[str] = []
    skipped = 0

    def write(img: Image.Image, path: str) -> None:
        nonlocal skipped
        if not overwrite and os.path.isfile(path):
            skipped += 1
            return
        img.save(path, compress_level=png_level)
        saved.append(path)

    base = compose_base(analysis, native)
    if base is None:
        raise RuntimeError("立绘为空（贴图或网格缺失）")
    if want_base:
        write(crop_alpha(base) if crop else base, os.path.join(painting_dir, painting + ".png"))

    for name, img in faces:
        out = compose_face(analysis, img, crop, base=base, offset=face_offset, native=native)
        if out is not None:
            write(out, os.path.join(painting_dir, f"{painting}_{safe_name(name)}.png"))

    if export_parts:
        parts_dir = os.path.join(painting_dir, "parts")
        os.makedirs(parts_dir, exist_ok=True)
        seen: Dict[str, int] = {}
        for p in analysis.parts:
            img = rebuild_part(p)
            if img is None:
                continue
            # several layers can share a sprite name; keep them apart
            stem = safe_name(p.name) or "part"
            seen[stem] = seen.get(stem, 0) + 1
            if seen[stem] > 1:
                stem = f"{stem}_{seen[stem]}"
            write(img, os.path.join(parts_dir, stem + ".png"))

    return saved, skipped


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name)
