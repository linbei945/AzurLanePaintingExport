"""离线自检脚本（不需要游戏素材）。

用桩对象构造分析结果，覆盖布局合成、表情合成、越界裁剪与导出调度等
关键逻辑。运行：``python selftest.py``
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

from PIL import Image

import core
import worker

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        FAILED.append(f"{name}: {exc}")
        print(f"[FAIL] {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    else:
        PASSED.append(name)
        print(f"[ ok ] {name}")


def patch(**attrs):
    """上下文管理器：临时替换模块属性。"""
    class _Ctx:
        def __init__(self) -> None:
            self.saved = {}

        def __enter__(self):
            for owner, values in attrs.items():
                mod = sys.modules[owner]
                for key, value in values.items():
                    self.saved[(mod, key)] = getattr(mod, key)
                    setattr(mod, key, value)
            return self

        def __exit__(self, *exc):
            for (mod, key), value in self.saved.items():
                setattr(mod, key, value)
            return False

    return _Ctx()


# ---------------------------------------------------------------- fake unity
class Vec:
    def __init__(self, x: float = 0.0, y: float = 0.0) -> None:
        self.x, self.y = float(x), float(y)


class GameObject:
    def __init__(self, name: str) -> None:
        self.m_Name = name


class RectTransform:
    """Mimics UnityPy's RectTransform (the class name matters to ExtTransform)."""

    def __init__(self, name="go", pos=(0, 0), size=(100, 100), pivot=(0, 0),
                 anchor_min=(0, 0), anchor_max=(0, 0), scale=(1, 1)):
        self.m_GameObject = GameObject(name)
        self.m_LocalPosition = Vec(*pos)
        self.m_AnchoredPosition = Vec(*pos)
        self.m_SizeDelta = Vec(*size)
        self.m_Pivot = Vec(*pivot)
        self.m_AnchorMin = Vec(*anchor_min)
        self.m_AnchorMax = Vec(*anchor_max)
        self.m_LocalScale = Vec(*scale)


class PlainTransform:
    """A Transform without RectTransform data."""

    def __init__(self, name="go", pos=(0, 0)):
        self.m_GameObject = GameObject(name)
        self.m_LocalPosition = Vec(*pos)
        self.m_LocalScale = Vec(1, 1)


ROOT = core.ExtTransform(RectTransform("root", size=(1920, 1080)), None)


def make_part(name="layer", pos=(0, 0), size=(100, 100), parent=ROOT,
              raw=None) -> core.Part:
    tr = RectTransform(name, pos=pos, size=size)
    ext = core.ExtTransform(tr, parent)
    raw = raw or core.V2(*size)
    return core.Part(name, name, None, None, raw, ext)


def solid(width: int, height: int, color=(255, 0, 0, 255)) -> Image.Image:
    return Image.new("RGBA", (max(1, int(width)), max(1, int(height))), color)


def fake_rebuild(part: core.Part):
    """Stand-in for the mesh rebuild: a solid block of the part's size."""
    return solid(part.size.x, part.size.y)


def make_analysis(parts, face=None, canvas=(1920, 1080), root_name="root"):
    return core.Analysis("painting/test", root_name, parts, face, canvas)


# ---------------------------------------------------------------- tests
def test_face_bundle_candidates():
    cases = {
        # 关键回归：三段式皮肤名不能塌成前两段（tiancheng_cv_2 不是 tiancheng_cv）
        "tiancheng_cv_2": ["tiancheng_cv_2", "tiancheng_cv", "tiancheng"],
        "tiancheng_cv_h_n": ["tiancheng_cv_h_n", "tiancheng_cv_h", "tiancheng_cv", "tiancheng"],
        "2b": ["2b"],
        "2b_n": ["2b_n", "2b"],                 # _n = 无装备，直接共用本体表情
        "2b_hx": ["2b_hx", "2b"],
        "2b_n_hx": ["2b_n_hx", "2b_hx", "2b"],  # _hx 自带表情组时优先用它
        "xin_n_1": ["xin_n_1", "xin_n", "xin"],  # 尾部不是标记，只能逐段回退
        "n_n_x": ["n_n_x", "n_n", "n"],
    }
    for src, want in cases.items():
        got = core.face_bundle_candidates(src)
        assert got == want, f"{src} -> {got}，期望 {want}"


def _face_dir(bundles):
    root = tempfile.mkdtemp(prefix="alpe_face_")
    os.makedirs(os.path.join(root, "paintingface"), exist_ok=True)
    for name in bundles:
        open(os.path.join(root, "paintingface", name), "w").close()
    return root


def test_face_bundle_path_prefers_exact():
    # 同名 bundle 存在时必须用它，不能回退到本体（这就是 tiancheng_cv_2 花脸的原因）
    root = _face_dir(["tiancheng_cv", "tiancheng_cv_2", "tiancheng"])
    try:
        got = core.face_bundle_path(root, "tiancheng_cv_2", (184, 184))
        assert os.path.basename(got) == "tiancheng_cv_2", got
        got = core.face_bundle_path(root, "tiancheng_cv_2_n", (184, 184))
        assert os.path.basename(got) == "tiancheng_cv_2", got
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_face_bundle_path_n_marker_is_direct():
    # _n 不需要核对槽尺寸：同一张画，只是没装备
    root = _face_dir(["2b"])
    try:
        with patch(core={"face_slot_size": lambda r, n: (_ for _ in ()).throw(AssertionError("不该解析槽"))}):
            got = core.face_bundle_path(root, "2b_n")
        assert os.path.basename(got) == "2b", got
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_face_bundle_path_rejects_other_skin():
    # 只剩别人的 bundle，且脸槽对不上 -> 宁可没有表情，也不能套别人的脸
    root = _face_dir(["tiancheng_cv"])
    slots = {"painting/tiancheng_cv_2": (184, 184), "painting/tiancheng_cv": (90, 96)}
    try:
        with patch(core={"face_slot_size": lambda r, n: slots.get(n)}):
            assert core.face_bundle_path(root, "tiancheng_cv_2", (184, 184)) is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_face_bundle_path_accepts_same_slot():
    # _asmr / _ex 这类同图变体：脸槽一致就沿用本体表情
    root = _face_dir(["hu_2"])
    try:
        with patch(core={"face_slot_size": lambda r, n: (331, 321)}):
            got = core.face_bundle_path(root, "hu_2_asmr", (331, 321))
        assert os.path.basename(got) == "hu_2", got
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_face_sort_key():
    names = ["10", "2", "1", "ex", "3"]
    assert sorted(names, key=core.face_sort_key) == ["1", "2", "3", "10", "ex"]


def test_safe_name():
    # 连续的非法字符会合并成一个下划线
    assert core.safe_name('a/b:c*?"<>|d') == "a_b_c_d", core.safe_name('a/b:c*?"<>|d')
    assert core.safe_name("2B") == "2B"


def test_transform_geometry():
    part = make_part(pos=(100, 50), size=(200, 100))
    assert (part.paste.x, part.paste.y) == (100, 50), part.paste
    assert (part.size.x, part.size.y) == (200, 100), part.size


def test_transform_with_anchors():
    tr = RectTransform("child", size=(0, 0), pivot=(0.5, 0.5),
                       anchor_min=(0, 0), anchor_max=(1, 1))
    ext = core.ExtTransform(tr, ROOT)
    # anchors stretch to the parent (1920x1080) plus sizeDelta
    assert (ext.size().x, ext.size().y) == (1920, 1080), ext.size()


def test_plain_transform_does_not_crash():
    ext = core.ExtTransform(PlainTransform("plain", pos=(30, 40)), ROOT)
    assert (ext.origin().x, ext.origin().y) == (30, 40), ext.origin()


def test_fit_keeps_normal_canvas():
    part = make_part("root", size=(1920, 1080), raw=core.V2(1920, 1080))
    analysis = make_analysis([part])
    scale, w, h, origin = core.fit(analysis)
    assert (scale, w, h) == (1.0, 1920, 1080), (scale, w, h)
    assert (origin.x, origin.y) == (0, 0), origin


def test_fit_is_one_to_one_with_texture():
    # UI 布局 1000×882，纹理 500×440：主图层必须正好落在 500 像素上
    part = make_part("root", size=(1000, 882), raw=core.V2(500, 440))
    analysis = make_analysis([part], canvas=(500, 440))
    scale, w, h, _ = core.fit(analysis)
    assert scale == 0.5, scale
    assert part.size.x * scale == 500.0, part.size.x * scale
    assert (w, h) == (500, 440), (w, h)      # 882*0.5=441 → 吸附回纹理高度


def test_fit_keeps_overhang():
    # 图层比纹理高出 42px（如 ninghai）：不许裁掉内容
    root = make_part("root", size=(1015, 1024), raw=core.V2(1015, 982))
    other = make_part("bg", pos=(0, 0), size=(1015, 1024), raw=core.V2(1015, 982))
    analysis = make_analysis([root, other], canvas=(1015, 982))
    _, w, h, _ = core.fit(analysis)
    assert (w, h) == (1015, 1024), (w, h)


def test_fit_clamps_huge_canvas():
    part = make_part("root", size=(8000, 8000), raw=core.V2(8000, 8000))
    analysis = make_analysis([part], canvas=(8000, 8000))
    scale, w, h, _ = core.fit(analysis)
    assert w * h <= core.MAX_RENDER_PIXELS, (w, h)
    assert scale < 1.0, scale


def test_boundings_include_face():
    part = make_part(size=(100, 100))
    face = core.FaceSlot("face", "$face$", core.ExtTransform(
        RectTransform("face", pos=(-50, 200), size=(40, 40)), ROOT))
    analysis = make_analysis([part], face)
    _, w, h, origin = core.fit(analysis)
    assert (origin.x, origin.y) == (-50, 0), origin
    assert (w, h) == (150, 240), (w, h)


def test_blend_negative_is_clipped():
    dst = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
    src = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
    core.blend(dst, src, -5, -5)
    assert dst.getpixel((0, 0))[:3] == (255, 0, 0), dst.getpixel((0, 0))
    assert dst.getpixel((14, 14))[:3] == (255, 0, 0), dst.getpixel((14, 14))
    assert dst.getpixel((15, 15))[:3] == (0, 0, 0), dst.getpixel((15, 15))


def test_blend_fully_outside():
    dst = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
    core.blend(dst, solid(5, 5), -50, 0)
    core.blend(dst, solid(5, 5), 0, -50)
    assert dst.getpixel((0, 0))[:3] == (0, 0, 0)


def test_fit_layout_mode():
    # 游戏布局模式：按 UI 坐标 1:1，画布跟着布局走
    part = make_part("root", size=(1000, 882), raw=core.V2(500, 440))
    analysis = make_analysis([part], canvas=(500, 440))
    scale, w, h, _ = core.fit(analysis, native=False)
    assert (scale, w, h) == (1.0, 1000, 882), (scale, w, h)
    with patch(core={"rebuild_part": fake_rebuild}):
        img = core.compose_base(analysis, native=False)
    assert img is not None and img.size == (1000, 882), img.size


def test_pad_keeps_sub_region():
    # 正常情况：网格图比声明尺寸窄几十像素，垫边后才能在矩形里对齐
    part = make_part("root", size=(4298, 3778), raw=core.V2(2048, 1800))
    out = core.pad_to_raw(part, solid(2017, 1800))
    assert out.size == (2048, 1800), out.size


def test_pad_skips_bogus_raw():
    # i404 这类：mRawSpriteSize 与网格、矩形对不上，不能垫边（否则整层错位出空洞）
    part = make_part("root", size=(5252, 9214), raw=core.V2(2048, 1770))
    out = core.pad_to_raw(part, solid(1167, 2048))
    assert out.size == (1167, 2048), out.size


def test_compose_base_matches_texture_size():
    root = make_part("root", size=(1000, 882), raw=core.V2(500, 440))
    low = make_part("low", pos=(0, -2), size=(1000, 882), raw=core.V2(500, 440))
    analysis = make_analysis([root, low], canvas=(500, 440))
    with patch(core={"rebuild_part": fake_rebuild}):
        img = core.compose_base(analysis)
    assert img is not None
    # 下层比画布低 2px，吸附到纹理尺寸后应被裁掉而不是抛错
    assert img.size == (500, 440), img.size


def test_compose_face_negative_offset():
    part = make_part("body", size=(200, 200), raw=core.V2(200, 200))
    face = core.FaceSlot("face", "$face$", core.ExtTransform(
        RectTransform("face", pos=(50, 50), size=(40, 40)), ROOT))
    analysis = make_analysis([part], face, canvas=(200, 200))
    patch_img = solid(40, 40, (0, 255, 0, 255))
    with patch(core={"rebuild_part": fake_rebuild}):
        base = core.compose_base(analysis)
        out = core.compose_face(analysis, patch_img, base=base, offset=(-999, -999))
        assert out is not None and out.size == base.size, out.size
        # 右 10 / 上 5：贴图从 (50,50) 挪到 (60,55)，图片行坐标 200-55-40=105
        moved = core.compose_face(analysis, patch_img, base=base, offset=(10, 5))
        assert moved.getpixel((80, 125))[:3] == (0, 255, 0), moved.getpixel((80, 125))
        # 向上偏移必须让脸在图片里更靠上（行号更小）
        plain = core.compose_face(analysis, patch_img, base=base)
        up = core.compose_face(analysis, patch_img, base=base, offset=(0, 20))
        assert up.getpixel((70, 95))[:3] == (0, 255, 0), "纵向正数应向上"
        assert plain.getpixel((70, 95))[:3] == (255, 0, 0), "未偏移时该像素应是立绘本体"


def test_export_flow():
    tmp = tempfile.mkdtemp(prefix="alpe_")
    try:
        body = make_part("body", size=(200, 200), raw=core.V2(200, 200))
        bg = make_part("body", size=(200, 200), raw=core.V2(200, 200))  # 同名部件
        face = core.FaceSlot("face", "$face$", core.ExtTransform(
            RectTransform("face", pos=(50, 50), size=(40, 40)), ROOT))
        analysis = make_analysis([body, bg], face)
        faces = [("1", solid(40, 40, (0, 255, 0, 255))),
                 ("2", solid(40, 40, (0, 0, 255, 255)))]

        def fake_load_faces(source, painting, wanted=None, slot=None):
            return [(n, im) for n, im in faces if wanted is None or n in wanted]

        def fake_analyze(source, name):
            return analysis

        with patch(core={"analyze": fake_analyze, "load_faces": fake_load_faces,
                         "rebuild_part": fake_rebuild}):
            # 只导底图
            saved, skipped = core.export_painting(None, "test", tmp, face_names=["0"])
            assert saved == [os.path.join(tmp, "test", "test.png")], saved
            assert skipped == 0
            assert Image.open(saved[0]).size == (200, 200)

            # 只导 1 号表情（旧实现会解码全部表情）
            saved, _ = core.export_painting(None, "test", tmp, face_names=["1"])
            assert saved == [os.path.join(tmp, "test", "test_1.png")], saved

            # 同样的文件再来一次 -> 全部跳过
            saved, skipped = core.export_painting(None, "test", tmp, face_names=["1"],
                                                  overwrite=False)
            assert saved == [] and skipped == 1, (saved, skipped)

            # 全量（CLI 默认）：两个表情都不带底图
            saved, _ = core.export_painting(None, "test", tmp, face_names=None)
            names = sorted(os.path.basename(p) for p in saved)
            assert names == ["test_1.png", "test_2.png"], names

            # 负偏移不再炸
            saved, _ = core.export_painting(None, "test", tmp, face_names=["2"],
                                            face_offset=(-5000, -5000))
            assert saved, saved

            # 各部件：同名 sprite 不能互相覆盖
            saved, _ = core.export_painting(None, "test", tmp, face_names=["0"],
                                            export_parts=True, overwrite=False)
            parts_dir = os.path.join(tmp, "test", "parts")
            names = sorted(os.listdir(parts_dir))
            assert names == ["body.png", "body_2.png"], names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preview_is_native_pixels():
    analysis = make_analysis([])
    img = solid(2048, 1800)
    with patch(core={"analyze": lambda src, name: analysis,
                     "compose_base": lambda a, native=True: img,
                     "face_layout": lambda a, native=True: None},
               worker={"get_source": lambda root: None}):
        res = worker.preview_job({"root": "x", "name": "test"})
    assert res.get("ok"), res
    assert (res["width"], res["height"]) == (2048, 1800), (res["width"], res["height"])
    assert "ratio" not in res, "预览不再做降采样"
    assert Image.open(io.BytesIO(res["png"])).size == (2048, 1800)


def test_ui_layer():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import main as ui
    except Exception as exc:  # noqa: BLE001
        print(f"[skip] 界面测试（未安装 PySide6）: {exc}")
        return

    from PySide6.QtWidgets import QApplication

    class FakeSettings:
        def __init__(self, *a, **k) -> None:
            self.data = {}

        def value(self, key, default=None, *a):
            return self.data.get(key, default)

        def setValue(self, key, value):
            self.data[key] = value

    app = QApplication.instance() or QApplication([])
    with patch(main={"QSettings": FakeSettings}):
        win = ui.MainWindow()
        try:
            assert ui.format_duration(65) == "1 分 5 秒"
            assert ui.format_duration(3700) == "1 时 1 分"

            win.rows = {"2b": 0, "2b_n": 1, "xin_n_1": 2, "x_bj1": 3, "y_rw": 4}
            win.checked = set()
            win.faces_available = {k: 0 for k in win.rows}
            win.cb_filter.setCurrentIndex(3)          # 仅 _n
            assert win._visible_names() == ["2b_n"], win._visible_names()
            win.cb_filter.setCurrentIndex(4)          # 排除背景部件
            assert "x_bj1" not in win._visible_names(), win._visible_names()
            win.cb_filter.setCurrentIndex(2)          # 仅 _rw
            assert win._visible_names() == ["y_rw"]
            win.cb_filter.setCurrentIndex(0)
            win.ed_search.setText("2b")
            assert win._visible_names() == ["2b", "2b_n"], win._visible_names()

            # 输出模式：默认游戏布局，两项文案不带括号后缀
            assert [win.cb_scale.itemText(i) for i in range(win.cb_scale.count())] == \
                ["原始纹理", "游戏布局"], [win.cb_scale.itemText(i) for i in range(2)]
            assert win.cb_scale.currentIndex() == 1, win.cb_scale.currentIndex()

            # 表情期望文件
            assert win._expected_files("2b", ["0", "1"], None) == ["2b.png", "2b_1.png"]
            assert win._expected_files("2b", ["5"], ["1", "3"]) == []

            # 有效目录：列表能载入
            tmp = tempfile.mkdtemp(prefix="alpe_ui_")
            try:
                root = os.path.join(tmp, "AssetBundles")
                os.makedirs(os.path.join(root, "painting"))
                os.makedirs(os.path.join(root, "paintingface"))
                open(os.path.join(root, "painting", "2b"), "w").close()
                win._set_root(root, reload_list=True)
                assert win.root == root, win.root
                assert win.table.rowCount() == 1, win.table.rowCount()
                assert win.lbl_source_info.text() == "1 个立绘资源", win.lbl_source_info.text()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            # 无效目录不能留下坏 root
            win._set_root(os.path.join(tempfile.gettempdir(), "no_such_folder"))
            assert win.root == "", win.root
            assert win.table.rowCount() == 0
        finally:
            win.close()
            app.processEvents()


def main() -> int:
    tests = [(name[len("test_"):], fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        check(name, fn)
    print()
    print(f"共 {len(tests)} 项：通过 {len(PASSED)}，失败 {len(FAILED)}")
    for item in FAILED:
        print("  -", item)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
