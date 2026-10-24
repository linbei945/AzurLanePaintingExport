"""One-click Azur Lane painting exporter.

Merges the three tools of the classic workflow into a single application:
  * AssetStudioMod            (bundle parsing / mesh + texture extraction)
  * AzurLanePaintingExtract   (mesh reconstruction)
  * AzurLanePaintingAnalysis  (layout + face expression merging)
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from PIL import Image
from PySide6.QtCore import QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import core
import worker
from core import AssetSource, face_bundle_exists

APP_NAME = "碧蓝航线立绘导出器"
APP_VERSION = "1.12.2"

# 表情 bundle 解析规则版本：改动候选命名规则时 +1，让旧的表情计数缓存失效
FACE_RULE_VERSION = 2

STATUS_QUEUED = "排队中"
STATUS_DONE = "已完成"
STATUS_FAIL = "失败"
STATUS_SKIP = "已存在"

STYLE = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 12px; }
QMainWindow, QWidget { background: #f5f6f8; color: #1f2430; }
QFrame#Card { background: #ffffff; border: 1px solid #e3e6ec; border-radius: 8px; }
QLabel#Title { font-size: 18px; font-weight: 600; }
QLabel#Subtitle { color: #7a8296; }
QLabel#Section { font-weight: 600; color: #3a4152; }
QLineEdit, QComboBox {
    background: #ffffff; border: 1px solid #d8dce5; border-radius: 6px;
    padding: 5px 8px; min-height: 20px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #2f6fed; }
QPushButton {
    background: #ffffff; border: 1px solid #d8dce5; border-radius: 6px;
    padding: 6px 14px; min-height: 20px;
}
QPushButton:hover { background: #f0f3fa; border-color: #b9c2d6; }
QPushButton:pressed { background: #e5eaf5; }
QPushButton#Primary { background: #2f6fed; border: 1px solid #2f6fed; color: #ffffff; font-weight: 600; }
QPushButton#Primary:hover { background: #2a63d4; }
QPushButton#Primary:disabled { background: #a9bdf0; border-color: #a9bdf0; }
QPushButton:checked { background: #e8f0ff; border-color: #2f6fed; color: #2f6fed; font-weight: 600; }
QPushButton#Danger { color: #c0392b; }
QTableWidget {
    background: #ffffff; border: 1px solid #e3e6ec; border-radius: 6px;
    gridline-color: #eef1f6; selection-background-color: #e8f0ff; selection-color: #1f2430;
}
QHeaderView::section {
    background: #f7f8fb; border: none; border-bottom: 1px solid #e3e6ec;
    padding: 6px; font-weight: 600; color: #58607a;
}
QProgressBar { border: 1px solid #d8dce5; border-radius: 6px; background: #ffffff; height: 16px; text-align: center; }
QProgressBar::chunk { background: #2f6fed; border-radius: 5px; }
QPlainTextEdit { background: #0f1420; color: #c9d4e5; border-radius: 6px; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #cfd5e2; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


class FaceCountWorker(QThread):
    """Reads expression names per painting in the background (bundle headers only)."""

    batch_ready = Signal(dict)
    finished_all = Signal()

    def __init__(self, root: str, names: list[str], parent=None) -> None:
        super().__init__(parent)
        self.root = root
        self.names = names
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        buffer: dict[str, list[str]] = {}
        for name in self.names:
            if self._stop:
                break
            try:
                names = core.face_names_in_bundle(self.root, name)
            except Exception:
                names = []
            buffer[name] = names
            if len(buffer) >= 80:
                self.batch_ready.emit(buffer)
                buffer = {}
        if buffer:
            self.batch_ready.emit(buffer)
        self.finished_all.emit()


class PreviewView(QGraphicsView):
    """Zoomable / pannable preview canvas with a checkerboard backdrop."""

    zoom_changed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        # Keep the viewport size constant: an appearing scrollbar changes the
        # viewport, which re-triggers the auto-fit and makes the image/zoom
        # flicker between two values at certain splitter positions.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignCenter)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumHeight(240)
        self.setBackgroundBrush(self._checker_brush())
        self.setStyleSheet("QGraphicsView { border: 1px dashed #d8dce5; border-radius: 6px; }")
        self._placeholder = "选择左侧立绘查看合成预览"
        self.setToolTip("滚轮缩放 · 按住左键拖动平移 · 双击切换 1:1 / 适应窗口")
        self._auto_fit = True
        self._fitting = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(60)
        self._fit_timer.timeout.connect(self.fit)

    @staticmethod
    def _checker_brush() -> QBrush:
        pm = QPixmap(18, 18)
        pm.fill(QColor("#ffffff"))
        p = QPainter(pm)
        p.fillRect(0, 0, 9, 9, QColor("#f1f3f7"))
        p.fillRect(9, 9, 9, 9, QColor("#f1f3f7"))
        p.end()
        return QBrush(pm)

    def has_image(self) -> bool:
        return not self._item.pixmap().isNull()

    def clear(self) -> None:
        self._item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())
        self._emit_zoom()

    def set_image(self, pix: QPixmap, keep_view: bool = False) -> None:
        same_size = not self._item.pixmap().isNull() and self._item.pixmap().size() == pix.size()
        self._item.setPixmap(pix)
        self._scene.setSceneRect(QRectF(pix.rect()))
        if not (keep_view and same_size):
            self.fit()
            self._fit_timer.start()

    def fit(self) -> None:
        if not self.has_image():
            return
        self._fitting = True
        try:
            self._auto_fit = True
            self.fitInView(self._item, Qt.KeepAspectRatio)
        finally:
            self._fitting = False
        self._emit_zoom()

    def zoom_reset(self) -> None:
        if not self.has_image():
            return
        self._auto_fit = False
        self.resetTransform()
        self._emit_zoom()

    def zoom_by(self, factor: float) -> None:
        if not self.has_image():
            return
        target = self.transform().m11() * factor
        if target < 0.02 or target > 16:
            return
        self._auto_fit = False
        self.scale(factor, factor)
        self._emit_zoom()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fitting or not self.has_image() or not self._auto_fit:
            return
        self._fit_timer.start()

    def _emit_zoom(self) -> None:
        self.zoom_changed.emit(self.transform().m11())

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.has_image():
            return
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.0018 ** delta)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if not self.has_image():
            return
        if self.transform().m11() >= 0.99:
            self.fit()
        else:
            self.zoom_reset()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.has_image():
            return
        painter = QPainter(self.viewport())
        painter.setPen(QColor("#9aa2b5"))
        painter.drawText(self.viewport().rect(), Qt.AlignCenter, self._placeholder)
        painter.end()


class MainWindow(QMainWindow):
    preview_ready = Signal(object, int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.resize(1320, 860)
        icon_path = resource_path(os.path.join("assets", "app.ico"))
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setAcceptDrops(True)

        self.settings = QSettings("AzurLaneTools", "PaintingExporter")
        self.root = ""
        self.out_dir = ""
        self.rows: dict[str, int] = {}
        self.checked: set[str] = set()
        self.faces_available: dict[str, int] = {}
        self.status: dict[str, str] = {}
        self._preview_token = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._start_preview)
        self.preview_ready.connect(self._on_preview_done)

        self.export_pool: ProcessPoolExecutor | None = None
        self.preview_pool: ProcessPoolExecutor | None = None
        self.futures: dict = {}
        self.pending: list[dict] = []
        self.total_jobs = 0
        self.done_jobs = 0
        self.failed_jobs = 0
        self.ok_jobs = 0
        self.skipped_jobs = 0
        self._base_pixmap: QPixmap | None = None
        self._face_data: dict[str, tuple[QPixmap, list[int]]] = {}
        self._face_names: list[str] = []
        self.face_choice: dict[str, set[str]] = {}
        self.face_default: set[str] | None = None
        self._current_painting: str | None = None
        self.only_checked = False
        self._log_history: list[str] = []
        self.face_offsets: dict[str, tuple[int, int]] = {}
        self._current_face: str | None = None
        self.face_counts: dict[str, int] = {}
        self.face_names_cache: dict[str, list[str]] = {}
        self.count_worker: FaceCountWorker | None = None
        self._count_total = 0
        self._count_done = 0
        self._counting_visible = False
        self.running = False
        self.started_at = 0.0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(120)
        self._poll_timer.timeout.connect(self._poll_export)

        self._build_ui()
        self._restore_settings()

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_paths())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(6)
        splitter.addWidget(self._build_list_card())
        splitter.addWidget(self._build_preview_card())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([462, 838])
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_footer())
        self._build_menu()

    def _build_header(self) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(2, 0, 2, 0)
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        lay.addWidget(title)
        lay.addStretch(1)
        self.lbl_source_info = QLabel("未选择素材目录")
        self.lbl_source_info.setObjectName("Subtitle")
        lay.addWidget(self.lbl_source_info)
        return box

    def _build_paths(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("素材根目录:"))
        self.ed_root = QLineEdit()
        self.ed_root.setPlaceholderText(r"选择包含 painting、paintingface、dependencies 的 AssetBundles 文件夹")
        self.ed_root.editingFinished.connect(self._on_root_edited)
        row1.addWidget(self.ed_root, 1)
        btn_root = QPushButton("浏览…")
        btn_root.clicked.connect(self._choose_root)
        row1.addWidget(btn_root)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("导出目录:"))
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("选择或输入导出位置")
        self.ed_out.editingFinished.connect(self._on_out_edited)
        row2.addWidget(self.ed_out, 1)
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(self._choose_out)
        row2.addWidget(btn_out)
        btn_open = QPushButton("打开目录")
        btn_open.clicked.connect(self._open_out)
        row2.addWidget(btn_open)

        lay.addLayout(row1)
        lay.addLayout(row2)
        return card

    def _build_list_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        card.setMinimumWidth(428)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索立绘名称，例如 hailunna / 2b / _rw")
        self.ed_search.textChanged.connect(self._apply_filter)
        top.addWidget(self.ed_search, 1)

        self.cb_filter = QComboBox()
        self.cb_filter.addItems(["全部", "仅含差分表情", "仅 _rw", "仅 _n", "排除背景部件"])
        self.cb_filter.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.cb_filter)
        lay.addLayout(top)

        top2 = QHBoxLayout()
        top2.setSpacing(4)
        compact = "QPushButton { padding: 2px 8px; min-height: 18px; }"
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(lambda: self._set_all_checked(True))
        btn_none = QPushButton("全不选")
        btn_none.clicked.connect(lambda: self._set_all_checked(False))
        btn_inv = QPushButton("反选")
        btn_inv.clicked.connect(self._invert_checked)
        self.btn_only_checked = QPushButton("已勾选")
        self.btn_only_checked.setCheckable(True)
        self.btn_only_checked.setToolTip("只看已经勾选的立绘，再点一次恢复全部")
        self.btn_only_checked.toggled.connect(self._on_only_checked)
        for b in (btn_all, btn_none, btn_inv, self.btn_only_checked):
            b.setFixedHeight(24)
            b.setStyleSheet(compact)
            top2.addWidget(b)
        top2.addStretch(1)
        self.lbl_count = QLabel("已选 0/0 项")
        self.lbl_count.setObjectName("Subtitle")
        top2.addWidget(self.lbl_count)
        lay.addLayout(top2)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["立绘名称", "差分"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        lay.addWidget(self._build_options())
        return card

    def _build_options(self) -> QWidget:
        opts = QFrame()
        opts.setObjectName("Options")
        opts.setStyleSheet("QFrame#Options { border: none; border-top: 1px solid #eef1f6; }")
        olay = QVBoxLayout(opts)
        olay.setContentsMargins(0, 8, 0, 0)
        olay.setSpacing(6)
        cap = QLabel("导出选项")
        cap.setObjectName("Section")
        olay.addWidget(cap)

        grid1 = QHBoxLayout()
        grid1.addWidget(QLabel("分辨率"))
        self.cb_scale = QComboBox()
        self.cb_scale.addItems(["原始纹理", "游戏布局"])
        self.cb_scale.setMinimumWidth(130)
        grid1.addWidget(self.cb_scale)
        grid1.addSpacing(10)
        grid1.addWidget(QLabel("PNG 压缩"))
        self.sp_level = QSpinBox()
        self.sp_level.setRange(0, 9)
        self.sp_level.setValue(0)
        self.sp_level.setFixedWidth(56)
        grid1.addWidget(self.sp_level)
        grid1.addSpacing(10)
        grid1.addWidget(QLabel("并行"))
        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 32)
        self.sp_workers.setValue(max(2, min(4, (os.cpu_count() or 4) // 2)))
        self.sp_workers.setFixedWidth(56)
        grid1.addWidget(self.sp_workers)
        grid1.addStretch(1)
        olay.addLayout(grid1)

        grid2 = QHBoxLayout()
        self.chk_parts = QCheckBox("同时导出各部件")
        self.chk_crop = QCheckBox("裁剪透明边")
        self.chk_skip = QCheckBox("跳过已存在的文件")
        self.chk_skip.setChecked(True)
        grid2.addWidget(self.chk_parts)
        grid2.addSpacing(6)
        grid2.addWidget(self.chk_crop)
        grid2.addSpacing(6)
        grid2.addWidget(self.chk_skip)
        grid2.addStretch(1)
        olay.addLayout(grid2)
        return opts

    def _build_face_picker(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet("QFrame { border: none; }")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        head = QHBoxLayout()
        cap = QLabel("差分表情")
        cap.setObjectName("Section")
        head.addWidget(cap)
        self.lbl_face_hint = QLabel("勾选要导出的表情，单击可预览")
        self.lbl_face_hint.setObjectName("Subtitle")
        head.addWidget(self.lbl_face_hint)
        head.addStretch(1)
        for text, slot, tip in (
            ("全选", self._faces_select_all, "勾选该立绘的全部表情"),
            ("全不选", self._faces_select_none, "一个表情都不导出"),
            ("应用到全部", self._faces_apply_all, "把当前勾选的表情套用到所有立绘"),
        ):
            btn = QPushButton(text)
            btn.setFixedHeight(24)
            btn.setStyleSheet("QPushButton { padding: 2px 8px; min-height: 18px; }")
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            head.addWidget(btn)
        v.addLayout(head)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.IconMode)
        self.face_list.setIconSize(QSize(60, 60))
        self.face_list.setGridSize(QSize(80, 86))
        self.face_list.setMovement(QListWidget.Static)
        self.face_list.setResizeMode(QListWidget.Adjust)
        self.face_list.setFlow(QListWidget.LeftToRight)
        self.face_list.setWrapping(False)
        self.face_list.setFixedHeight(92)
        self.face_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.face_list.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.face_list.setStyleSheet(
            "QListWidget { background: #fbfcfe; border: 1px solid #eef1f6; border-radius: 6px; }"
            "QListWidget::item { border-radius: 6px; padding: 2px; }"
            "QListWidget::item:selected { background: #e8f0ff; color: #1f2430; }"
        )
        self.face_list.itemChanged.connect(self._on_face_check_changed)
        self.face_list.itemClicked.connect(self._on_face_clicked)
        v.addWidget(self.face_list)

        off_row = QHBoxLayout()
        off_row.setContentsMargins(0, 0, 0, 0)
        off_row.setSpacing(4)
        off_row.addWidget(QLabel("横向偏移"))
        self.sp_off_x = QSpinBox()
        self.sp_off_x.setRange(-999, 999)
        self.sp_off_x.setFixedSize(64, 24)
        self.sp_off_x.setAccelerated(True)
        self.sp_off_x.setToolTip("正数向右移动，可直接输入数值")
        self.sp_off_x.valueChanged.connect(self._on_face_offset_changed)
        off_row.addWidget(self.sp_off_x)
        off_row.addSpacing(10)
        off_row.addWidget(QLabel("纵向偏移"))
        self.sp_off_y = QSpinBox()
        self.sp_off_y.setRange(-999, 999)
        self.sp_off_y.setFixedSize(64, 24)
        self.sp_off_y.setAccelerated(True)
        self.sp_off_y.setToolTip("正数向上移动，可直接输入数值")
        self.sp_off_y.valueChanged.connect(self._on_face_offset_changed)
        off_row.addWidget(self.sp_off_y)
        off_row.addSpacing(8)
        self.btn_off_reset = QPushButton("恢复默认")
        self.btn_off_reset.setFixedHeight(24)
        self.btn_off_reset.setStyleSheet("QPushButton { padding: 2px 8px; min-height: 18px; }")
        self.btn_off_reset.setToolTip("把当前立绘的偏移恢复为 0,0")
        self.btn_off_reset.clicked.connect(self._reset_face_offset)
        off_row.addWidget(self.btn_off_reset)
        off_row.addStretch(1)
        v.addLayout(off_row)

        self.face_box = box
        box.setVisible(False)
        return box

    def _build_preview_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        card.setMinimumWidth(420)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self.preview = PreviewView()
        self.preview.zoom_changed.connect(self._on_zoom_changed)
        self.preview.setMinimumHeight(280)

        head = QHBoxLayout()
        head.addWidget(QLabel("预览"))
        for text, slot, tip in (
            ("−", lambda: self.preview.zoom_by(1 / 1.25), "缩小"),
            ("+", lambda: self.preview.zoom_by(1.25), "放大"),
            ("1:1", self.preview.zoom_reset, "按像素 1:1 显示"),
            ("适应", self.preview.fit, "缩放以适应窗口"),
        ):
            btn = QPushButton(text)
            btn.setFixedSize(46 if text != "适应" else 54, 24)
            btn.setStyleSheet("QPushButton { padding: 0px 2px; }")
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            head.addWidget(btn)
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setObjectName("Subtitle")
        self.lbl_zoom.setFixedWidth(46)
        head.addWidget(self.lbl_zoom)
        head.addStretch(1)
        self.lbl_preview_info = QLabel("")
        self.lbl_preview_info.setObjectName("Subtitle")
        head.addWidget(self.lbl_preview_info)
        lay.addLayout(head)

        lay.addWidget(self.preview, 1)

        lay.addWidget(self._build_face_picker())

        self.lbl_detail = QLabel("—")
        self.lbl_detail.setObjectName("Subtitle")
        self.lbl_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_detail)
        return card

    def _build_footer(self) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(8)
        self.lbl_log = QLineEdit()
        self.lbl_log.setReadOnly(True)
        self.lbl_log.setFrame(False)
        self.lbl_log.setFixedHeight(22)
        self.lbl_log.setPlaceholderText("就绪")
        self.lbl_log.setStyleSheet(
            "QLineEdit { background: transparent; border: none; color: #58607a; padding: 0 2px; }"
            "QLineEdit:focus { border: none; }"
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(190)
        self.btn_start = QPushButton("开始导出")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setMinimumWidth(120)
        self.btn_start.clicked.connect(self._start_export)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_export)
        lay.addWidget(self.lbl_log, 1)
        lay.addWidget(self.progress)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.btn_start)
        return box

    def _build_menu(self) -> None:
        bar = self.menuBar()
        m_file = bar.addMenu("文件")
        act_open = QAction("打开导出目录", self)
        act_open.triggered.connect(self._open_out)
        m_file.addAction(act_open)
        m_file.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_help = bar.addMenu("帮助")
        act_log = QAction("查看日志", self)
        act_log.triggered.connect(self._show_log)
        m_help.addAction(act_log)
        m_help.addSeparator()
        act_help = QAction("使用说明", self)
        act_help.triggered.connect(self._show_help)
        m_help.addAction(act_help)
        act_about = QAction("关于", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    # ------------------------------------------------------------------ settings
    def _restore_settings(self) -> None:
        # First run on a new machine: leave both paths empty, the user picks
        # them once and they are remembered from then on.
        root = self.settings.value("root", "", str)
        out = self.settings.value("out", "", str)
        if root and not os.path.isdir(os.path.join(root, "painting")):
            root = ""
        self.ed_root.setText(root)
        self.ed_out.setText(out)
        if not self.settings.value("level_zero_default", False, bool):
            # v1.5: PNG compression now defaults to 0 (faster saves)
            self.settings.setValue("level", 0)
            self.settings.setValue("level_zero_default", True)
        self.sp_level.setValue(int(self.settings.value("level", 0)))
        self.sp_workers.setValue(int(self.settings.value("workers", self.sp_workers.value())))
        self.chk_parts.setChecked(self.settings.value("parts", False, bool))
        self.chk_crop.setChecked(self.settings.value("crop", False, bool))
        self.chk_skip.setChecked(self.settings.value("skip", True, bool))
        # 默认「游戏布局」；换用新键名，避免旧版本存下的 scale=0 覆盖新默认值
        idx = int(self.settings.value("scale_mode", 1))
        self.cb_scale.setCurrentIndex(idx if 0 <= idx < self.cb_scale.count() else 1)
        if root:
            self._set_root(root, reload_list=True)
        if out:
            self._set_out(out, save=False)
        if not root:
            self._log("欢迎使用：请先选择素材根目录（AssetBundles）和导出目录")

    def _save_settings(self) -> None:
        self.settings.setValue("root", self.root)
        self.settings.setValue("out", self.out_dir)
        self.settings.setValue("level", self.sp_level.value())
        self.settings.setValue("workers", self.sp_workers.value())
        self.settings.setValue("parts", self.chk_parts.isChecked())
        self.settings.setValue("crop", self.chk_crop.isChecked())
        self.settings.setValue("skip", self.chk_skip.isChecked())
        self.settings.setValue("scale_mode", self.cb_scale.currentIndex())

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        self._save_face_cache()
        self._stop_counting()
        self._stop_export()
        if self.preview_pool:
            self.preview_pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)

    # ------------------------------------------------------------------ paths
    def _choose_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 AssetBundles 文件夹", self.root or os.path.expanduser("~"))
        if path:
            self._set_root(path, reload_list=True)

    def _choose_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择导出目录", self.out_dir or os.path.expanduser("~"))
        if path:
            self._set_out(path)

    def _on_root_edited(self) -> None:
        self._set_root(self.ed_root.text().strip(), reload_list=True)

    def _on_out_edited(self) -> None:
        self._set_out(self.ed_out.text().strip(), save=False)

    def _set_root(self, path: str, reload_list: bool = False) -> None:
        changed = path != self.root
        if path and not os.path.isdir(os.path.join(path, "painting")):
            self._log(f"[警告] {path} 下没有 painting 文件夹，请确认选择的是 AssetBundles 根目录")
            self.lbl_source_info.setText("目录无效")
            # never keep a broken root: the list would still show the previous
            # folder's paintings and every export would fail
            self.root = ""
            self.ed_root.setText(path)
            self._reload_list()
            return
        self.root = path
        self.ed_root.setText(path)
        if not path:
            self._reload_list()
            return
        n = len(AssetSource.list_paintings_static(path))
        self.lbl_source_info.setText(f"{n} 个立绘资源")
        if changed:
            self.face_counts.clear()
            self._stop_counting()
            self._load_face_cache()
        if reload_list and (changed or not self.rows):
            self._reload_list()
        if self.preview_pool:
            self.preview_pool.shutdown(wait=False, cancel_futures=True)
            self.preview_pool = None

    def _set_out(self, path: str, save: bool = True) -> None:
        self.out_dir = path
        self.ed_out.setText(path)
        if save:
            self._save_settings()

    def _open_out(self) -> None:
        if self.out_dir and os.path.isdir(self.out_dir):
            os.startfile(self.out_dir)  # noqa: S606
        else:
            QMessageBox.information(self, "提示", "导出目录尚不存在")

    # ------------------------------------------------------------------ list
    def _reload_list(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.rows.clear()
        self.checked.clear()
        self.status.clear()
        self.faces_available.clear()
        if not self.root:
            self.table.blockSignals(False)
            return
        try:
            names = AssetSource.list_paintings_static(self.root)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 读取列表失败: {exc}")
            self.table.blockSignals(False)
            return

        painting_dir = os.path.join(self.root, "painting")
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(names))
        pending: list[str] = []
        for row, name in enumerate(names):
            has_bundle = face_bundle_exists(self.root, name)
            self.faces_available[name] = 1 if has_bundle else 0
            if not has_bundle:
                self.face_names_cache[name] = []
                self.face_counts[name] = 0
            count = self.face_counts.get(name)
            if count is None and has_bundle:
                pending.append(name)
            item = QTableWidgetItem(name)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, name)
            self.table.setItem(row, 0, item)
            face_item = QTableWidgetItem(self._face_text(count, has_bundle))
            if count:
                face_item.setForeground(QColor("#2f6fed"))
            face_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, face_item)
            self.rows[name] = row
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)
        self.lbl_count.setText(f"{len(names)} 项")
        self._apply_filter()
        self._log(f"已载入 {len(names)} 个立绘素材（{painting_dir}）")
        self._start_face_counting(pending)

    @staticmethod
    def _face_text(count: int | None, has_bundle: bool) -> str:
        if count is None:
            return "…" if has_bundle else "—"
        return str(count) if count > 0 else "—"

    @staticmethod
    def _face_cache_path() -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AzurLanePaintingExport", "face_counts.json")

    def _face_signature(self) -> str:
        folder = os.path.join(self.root, "paintingface")
        try:
            st = os.stat(folder)
            return f"{FACE_RULE_VERSION}:{len(os.listdir(folder))}:{int(st.st_mtime)}"
        except Exception:
            return ""

    def _load_face_cache(self) -> None:
        try:
            with open(self._face_cache_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        entry = data.get(self.root) if isinstance(data, dict) else None
        if not entry or entry.get("sig") != self._face_signature():
            return
        faces = entry.get("faces") or {}
        if not isinstance(faces, dict):
            return
        for name, names in faces.items():
            if not isinstance(names, list):
                continue
            self.face_names_cache[name] = [str(n) for n in names]
            self.face_counts[name] = len(names)

    def _save_face_cache(self) -> None:
        if not self.root or not self.face_names_cache:
            return
        path = self._face_cache_path()
        data = {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        data[self.root] = {"sig": self._face_signature(), "faces": self.face_names_cache}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:
            pass

    def _start_face_counting(self, names: list[str]) -> None:
        if self.count_worker is not None:
            self.count_worker.stop()
            self.count_worker.wait(1500)
            self.count_worker = None
        if not names:
            return
        self._count_total = len(names)
        self._count_done = 0
        worker = FaceCountWorker(self.root, names, self)
        worker.batch_ready.connect(self._on_face_counts)
        worker.finished_all.connect(self._on_face_count_done)
        self.count_worker = worker
        self._counting_visible = True
        worker.start(QThread.LowPriority)
        self._status_line(f"正在统计差分表情数量（0/{self._count_total}）…", count_msg=True)

    def _on_face_counts(self, batch: dict) -> None:
        self._count_done += len(batch)
        self.table.setUpdatesEnabled(False)
        for name, names in batch.items():
            names = [str(n) for n in (names or [])]
            self.face_names_cache[name] = names
            count = len(names)
            self.face_counts[name] = count
            self.faces_available[name] = 1 if count else 0
            row = self.rows.get(name)
            if row is None:
                continue
            cell = self.table.item(row, 1)
            if cell is None:
                continue
            has_bundle = face_bundle_exists(self.root, name)
            cell.setText(self._face_text(count, has_bundle))
            cell.setForeground(QColor("#2f6fed") if count else QColor("#8a93a8"))
        self.table.setUpdatesEnabled(True)
        if self._count_total and not self.running and self._counting_visible:
            self._status_line(
                f"正在统计差分表情数量（{self._count_done}/{self._count_total}）…", count_msg=True
            )

    def _on_face_count_done(self) -> None:
        total_with_faces = sum(1 for names in self.face_names_cache.values() if names)
        self._save_face_cache()
        self._log(f"差分表情数量统计完成：{total_with_faces} 个立绘带表情")

    def _stop_counting(self) -> None:
        if self.count_worker is not None:
            self.count_worker.stop()
            self.count_worker.wait(1500)
            self.count_worker = None

    # ------------------------------------------------------------------ filter / check
    def _visible_names(self, ignore_only_checked: bool = False) -> list[str]:
        text = self.ed_search.text().strip().lower()
        mode = self.cb_filter.currentIndex()
        out = []
        for name in self.rows:
            if self.only_checked and not ignore_only_checked and name not in self.checked:
                continue
            if text and text not in name.lower():
                continue
            if mode == 1 and not self.faces_available.get(name):
                continue
            if mode == 2 and not name.endswith("_rw"):
                continue
            if mode == 3 and not name.endswith("_n"):
                continue
            if mode == 4 and re.search(r"_(bj|jz|tx)\d*$", name):
                continue
            out.append(name)
        return out

    def _apply_filter(self) -> None:
        visible = set(self._visible_names())
        self.table.setUpdatesEnabled(False)
        for name, row in self.rows.items():
            self.table.setRowHidden(row, name not in visible)
        self.table.setUpdatesEnabled(True)
        self._update_count()

    def _set_all_checked(self, value: bool) -> None:
        self.table.blockSignals(True)
        for name in self._visible_names(ignore_only_checked=True):
            row = self.rows[name]
            item = self.table.item(row, 0)
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)
            if value:
                self.checked.add(name)
            else:
                self.checked.discard(name)
        self.table.blockSignals(False)
        self._update_count()
        if self.only_checked:
            self._apply_filter()

    def _invert_checked(self) -> None:
        self.table.blockSignals(True)
        for name in self._visible_names(ignore_only_checked=True):
            row = self.rows[name]
            item = self.table.item(row, 0)
            if item.checkState() == Qt.Checked:
                item.setCheckState(Qt.Unchecked)
                self.checked.discard(name)
            else:
                item.setCheckState(Qt.Checked)
                self.checked.add(name)
        self.table.blockSignals(False)
        self._update_count()
        if self.only_checked:
            self._apply_filter()

    def _update_count(self) -> None:
        total = len(self.checked)
        self.btn_only_checked.setToolTip(f"只看已经勾选的立绘（{total} 个），再点一次恢复全部")
        self.lbl_count.setText(f"已选 {total}/{len(self.rows)} 项")

    def _on_only_checked(self, enabled: bool) -> None:
        self.only_checked = enabled
        self._apply_filter()
        self._update_count()
        if enabled:
            self._log(f"只显示已勾选的立绘（{len(self.checked)} 个）")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        name = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.checked.add(name)
        else:
            self.checked.discard(name)
        self._update_count()
        if self.only_checked:
            QTimer.singleShot(0, self._apply_filter)

    # ------------------------------------------------------------------ preview
    def _on_selection_changed(self) -> None:
        self._preview_timer.start()

    def _current_name(self) -> str | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return self.table.item(items[0].row(), 0).data(Qt.UserRole)

    def _ensure_preview_pool(self) -> bool:
        if not self.root:
            return False
        if self.preview_pool is None:
            self.preview_pool = ProcessPoolExecutor(
                max_workers=1, initializer=worker.init, initargs=(self.root,)
            )
        return True

    def _start_preview(self) -> None:
        name = self._current_name()
        if not name or not self._ensure_preview_pool():
            return
        self._preview_token += 1
        token = self._preview_token
        self.lbl_preview_info.setText("正在解析…")
        self._base_pixmap = None
        self._face_data = {}
        self._face_names = []
        self.face_list.blockSignals(True)
        self.face_list.clear()
        self.face_list.blockSignals(False)
        self.face_box.setVisible(False)
        self.preview.clear()
        job = {
            "root": self.root,
            "name": name,
            "native": self.cb_scale.currentIndex() == 0,
        }
        try:
            fut = self.preview_pool.submit(worker.preview_job, job)
        except Exception as exc:  # noqa: BLE001
            self.lbl_preview_info.setText(f"预览失败: {exc}")
            return
        fut.add_done_callback(lambda f: self.preview_ready.emit(f, token))

    def _on_preview_done(self, fut, token: int) -> None:
        try:
            result = fut.result()
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        if token != self._preview_token:
            return
        if not result.get("ok"):
            self.lbl_preview_info.setText("预览失败")
            self.lbl_detail.setText(result.get("error", ""))
            self._log(f"[预览失败] {result.get('name')}: {result.get('error')}")
            return
        pix = QPixmap()
        pix.loadFromData(result["png"], "PNG")
        self._base_pixmap = pix
        self._current_painting = result.get("name")
        self.lbl_preview_info.setText(f"{result['width']} × {result['height']}")
        faces = result.get("faces") or []
        self._populate_faces(
            result.get("name"), faces, result.get("base_thumb") or b"", result.get("face_rect")
        )
        if self.face_list.count():
            self.face_list.setCurrentRow(0)
        self._render_face_preview("0" if self.face_list.count() else None)
        parts = result.get("parts", [])
        shown = "、".join(parts[:6]) + ("…" if len(parts) > 6 else "")
        self.lbl_detail.setText(f"合成层 {len(parts)} 个：{shown} · 差分表情 {len(faces)} 个")
        self.lbl_detail.setToolTip("、".join(parts))

    # ------------------------------------------------------------------ faces
    def _populate_faces(self, painting: str, faces: list[dict],
                        base_thumb: bytes = b"", face_rect=None) -> None:
        self.face_list.blockSignals(True)
        self.face_list.clear()
        self._face_data = {}
        self._face_names = []
        if face_rect and base_thumb:
            thumb = QPixmap()
            thumb.loadFromData(base_thumb, "PNG")
            self._face_names.append("0")
            self._face_data["0"] = (None, list(face_rect))
            item = QListWidgetItem(QIcon(thumb), "0")
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, "0")
            item.setCheckState(Qt.Checked)
            item.setToolTip("无表情底图（单击预览，勾选则导出底图）")
            self.face_list.addItem(item)
        for face in faces:
            name = face["name"]
            pix = QPixmap()
            pix.loadFromData(face["png"], "PNG")
            thumb = QPixmap()
            thumb.loadFromData(face["thumb"], "PNG")
            self._face_data[name] = (pix, face["rect"])
            self._face_names.append(name)
            item = QListWidgetItem(QIcon(thumb), name)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, name)
            item.setCheckState(Qt.Checked)
            item.setToolTip(f"表情 {name}（单击预览，勾选决定是否导出）")
            self.face_list.addItem(item)

        chosen = self._selection_for(painting, self._face_names)
        for i in range(self.face_list.count()):
            item = self.face_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in chosen else Qt.Unchecked)
        self.face_list.blockSignals(False)

        dx, dy = self.face_offsets.get(painting, (0, 0))
        for spin, value in ((self.sp_off_x, dx), (self.sp_off_y, dy)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

        self.face_box.setVisible(bool(faces))
        self._update_face_hint()

    def _selection_for(self, painting: str | None, names: list[str]) -> set[str]:
        if painting and painting in self.face_choice:
            return set(self.face_choice[painting]) & set(names)
        if self.face_default is not None:
            return set(self.face_default) & set(names)
        # default: only the first expression, so a fresh painting does not
        # silently emit five near-identical files
        return set(names[:1])

    def _checked_faces(self) -> set[str]:
        out = set()
        for i in range(self.face_list.count()):
            item = self.face_list.item(i)
            if item.checkState() == Qt.Checked:
                out.add(item.data(Qt.UserRole))
        return out

    def _update_face_hint(self) -> None:
        total = len(self._face_names)
        if not total:
            return
        checked = len(self._checked_faces())
        cur = self._current_painting
        is_default = cur not in self.face_choice and self.face_default is None
        scope = "（已应用到全部立绘）" if self.face_default is not None and not self.face_choice else ""
        note = " · 默认勾 0（底图）" if is_default else ""
        self.lbl_face_hint.setText(f"已选 {checked}/{total} 项{note} · 单击预览{scope}")

    def _on_face_check_changed(self, item: QListWidgetItem) -> None:
        if not self._current_painting or not self._face_names:
            return
        self.face_choice[self._current_painting] = self._checked_faces()
        self._update_face_hint()

    def _on_face_clicked(self, item: QListWidgetItem) -> None:
        self._render_face_preview(item.data(Qt.UserRole))

    def _on_face_offset_changed(self, _value: int = 0) -> None:
        name = self._current_painting
        if not name:
            return
        self.face_offsets[name] = (self.sp_off_x.value(), self.sp_off_y.value())
        face = self._current_face
        if face is None:
            checked = sorted(self._checked_faces())
            face = checked[0] if checked else (self._face_names[0] if self._face_names else None)
        if face:
            self._render_face_preview(face)

    def _reset_face_offset(self) -> None:
        self.sp_off_x.setValue(0)
        self.sp_off_y.setValue(0)

    def _faces_select_all(self) -> None:
        self._set_all_faces(True)

    def _faces_select_none(self) -> None:
        self._set_all_faces(False)

    def _set_all_faces(self, value: bool) -> None:
        self.face_list.blockSignals(True)
        for i in range(self.face_list.count()):
            item = self.face_list.item(i)
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)
        self.face_list.blockSignals(False)
        if self._current_painting:
            self.face_choice[self._current_painting] = self._checked_faces()
        self._update_face_hint()

    def _faces_apply_all(self) -> None:
        if not self._face_names:
            return
        self.face_default = self._checked_faces()
        self.face_choice.clear()
        self._update_face_hint()
        names = sorted(self.face_default, key=lambda s: (len(s), s))
        self._log(f"已把表情选择应用到全部立绘：{', '.join(names) if names else '（不导出表情）'}")

    def _render_face_preview(self, face_name: str | None) -> None:
        if self._base_pixmap is None or self._base_pixmap.isNull():
            return
        image = self._base_pixmap.toImage().copy()
        self._current_face = face_name
        data = self._face_data.get(face_name) if face_name else None
        if data is not None and data[0] is not None:
            face_pix, (x, y, w, h) = data
            dx, dy = self.face_offsets.get(self._current_painting or "", (0, 0))
            painter = QPainter(image)
            painter.drawPixmap(x + dx, y - dy, face_pix)
            painter.end()
        pix = QPixmap.fromImage(image)
        self.preview.set_image(pix, keep_view=True)

    def _on_zoom_changed(self, factor: float) -> None:
        self.lbl_zoom.setText(f"{factor * 100:.0f}%")

    # ------------------------------------------------------------------ export
    def _start_export(self) -> None:
        if self.running:
            return
        if not self.root:
            QMessageBox.warning(self, "提示", "请先选择 AssetBundles 根目录")
            return
        if not self.out_dir:
            QMessageBox.warning(self, "提示", "请先选择导出目录")
            return
        names = [n for n in self.rows if n in self.checked]
        if not names:
            current = self._current_name()
            if not current:
                QMessageBox.information(
                    self, "提示",
                    "请先勾选要导出的立绘，或在左侧列表里选中一个立绘（选中即可直接导出它）。",
                )
                return
            names = [current]
            self._log(f"未勾选任何立绘，直接导出当前选中的：{current}")
        os.makedirs(self.out_dir, exist_ok=True)

        self.running = True
        self.failed_jobs = 0
        self.ok_jobs = 0
        self.skipped_jobs = 0
        self.done_jobs = 0
        self.total_jobs = len(names)
        self.started_at = time.time()
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log(f"开始导出 {len(names)} 项到 {self.out_dir}")
        if self.face_default is not None:
            picked = "、".join(sorted(self.face_default)) or "（无）"
            self._log(f"差分表情筛选已应用到全部立绘：{picked}")
        custom = sum(1 for n in names if n in self.face_choice)
        if custom:
            self._log(f"{custom} 个立绘使用单独的表情勾选")

        if self.export_pool is None:
            self.export_pool = ProcessPoolExecutor(
                max_workers=self.sp_workers.value(),
                initializer=worker.init,
                initargs=(self.root,),
            )
        opts = {
            "root": self.root,
            "out_dir": self.out_dir,
            "faces": True,
            "parts": self.chk_parts.isChecked(),
            "crop": self.chk_crop.isChecked(),
            "level": self.sp_level.value(),
            "native": self.cb_scale.currentIndex() == 0,
        }
        for name in names:
            row = self.rows.get(name)
            if row is None:
                continue
            chosen = self.face_choice.get(name, self.face_default)
            requested = sorted(chosen) if chosen is not None else ["0"]
            if not requested:
                requested = ["0"]
            available = self.face_names_cache.get(name)
            expected = self._expected_files(name, requested, available)
            skip_dir = os.path.join(self.out_dir, name)
            if self.chk_skip.isChecked() and self._already_done(skip_dir, expected, available):
                self._set_status(name, STATUS_SKIP, "无匹配表情" if not expected else "—")
                self.done_jobs += 1
                self.skipped_jobs += 1
                continue
            job = dict(
                opts,
                name=name,
                face_names=requested,
                face_offset=self.face_offsets.get(name, (0, 0)),
                overwrite=not self.chk_skip.isChecked(),
            )
            try:
                fut = self.export_pool.submit(worker.export_job, job)
            except Exception as exc:  # noqa: BLE001
                self._set_status(name, STATUS_FAIL, str(exc)[:40])
                self.failed_jobs += 1
                self.done_jobs += 1
                continue
            self.futures[fut] = name
            self._set_status(name, STATUS_QUEUED, "")

        self._update_progress()
        if not self.futures:
            self._finish_export()
            return
        self._poll_timer.start()

    def _expected_files(self, name: str, requested: list[str], available: list[str] | None) -> list[str]:
        """Files this painting should produce for the current selection."""
        expected: list[str] = []
        for item in requested:
            if item == "0":
                expected.append(f"{name}.png")
            elif available is None or item in available:
                expected.append(f"{name}_{core.safe_name(item)}.png")
        return expected

    def _already_done(self, folder: str, expected: list[str], available: list[str] | None) -> bool:
        if not os.path.isdir(folder):
            return False
        if not expected:
            # every requested expression is known to be missing on this painting
            return available is not None
        for filename in expected:
            if not os.path.isfile(os.path.join(folder, filename)):
                return False
        if self.chk_parts.isChecked():
            parts = os.path.join(folder, "parts")
            if not (
                os.path.isdir(parts)
                and any(f.lower().endswith(".png") for f in os.listdir(parts))
            ):
                return False
        return True

    def _poll_export(self) -> None:
        finished = [f for f in list(self.futures) if f.done()]
        for fut in finished:
            name = self.futures.pop(fut)
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"name": name, "ok": False, "error": str(exc), "seconds": 0}
            if result.get("ok") and not result.get("count"):
                skipped = result.get("skipped", 0)
                self._set_status(name, STATUS_SKIP, f"已存在 {skipped}" if skipped else "无匹配表情")
                self.skipped_jobs += 1
            elif result.get("ok"):
                self._set_status(name, STATUS_DONE, f"{result.get('seconds', 0)}s")
                self.ok_jobs += 1
            else:
                self._set_status(name, STATUS_FAIL, "")
                self.failed_jobs += 1
                self._log(f"[失败] {name}: {result.get('error')}")
            self.done_jobs += 1
        if finished:
            self._update_progress()
        if not self.futures:
            self._finish_export()

    def _update_progress(self) -> None:
        pct = 0 if not self.total_jobs else int(self.done_jobs * 100 / self.total_jobs)
        self.progress.setValue(pct)
        elapsed = time.time() - self.started_at
        speed = self.done_jobs / elapsed if elapsed > 0 else 0
        eta = (self.total_jobs - self.done_jobs) / speed if speed > 0 else 0
        self._status_line(
            f"{self.done_jobs}/{self.total_jobs} · 成功 {self.ok_jobs} · 失败 {self.failed_jobs} · "
            f"{speed:.2f} 项/秒 · 剩余 {format_duration(eta)}"
        )

    def _finish_export(self) -> None:
        self._poll_timer.stop()
        self.running = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        elapsed = time.time() - self.started_at
        self._status_line(
            f"完成 {self.done_jobs}/{self.total_jobs} 项 · 成功 {self.ok_jobs} · 失败 {self.failed_jobs} · "
            f"用时 {format_duration(elapsed)}"
        )
        self._log(
            f"导出结束：成功 {self.ok_jobs}，失败 {self.failed_jobs}，跳过 {self.skipped_jobs}，"
            f"用时 {format_duration(elapsed)}"
        )

    def _stop_export(self) -> None:
        if self.export_pool:
            self.export_pool.shutdown(wait=False, cancel_futures=True)
            self.export_pool = None
        for fut in self.futures:
            fut.cancel()
        self.futures.clear()
        if self.running:
            self._poll_timer.stop()
            self.running = False
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._log("已停止导出")

    def _set_status(self, name: str, text: str, extra: str = "") -> None:
        """Row state is shown by colouring the name (status column was removed)."""
        row = self.rows.get(name)
        if row is None:
            return
        cell = self.table.item(row, 0)
        if cell is None:
            return
        color = {
            STATUS_DONE: "#1f9d55",
            STATUS_FAIL: "#c0392b",
            STATUS_SKIP: "#8a93a8",
            STATUS_QUEUED: "#8a93a8",
        }.get(text, "#1f2430")
        cell.setForeground(QColor(color))
        tip = f"{name}\n{text}" + (f" · {extra}" if extra else "")
        if text == STATUS_FAIL:
            tip += "\n（详细原因见底部日志）"
        cell.setToolTip(tip)

    # ------------------------------------------------------------------ misc
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not os.path.isdir(path):
                continue
            inner = os.path.join(path, "AssetBundles")
            if os.path.isdir(os.path.join(inner, "painting")):
                path = inner
            if os.path.isdir(os.path.join(path, "painting")):
                self._set_root(path, reload_list=True)
                self._log(f"从拖放载入素材目录：{path}")
                break

    def _log(self, text: str) -> None:
        self._counting_visible = False
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self._log_history.append(line)
        if len(self._log_history) > 3000:
            del self._log_history[:1000]
        self._status_line(line)

    def _status_line(self, text: str, count_msg: bool = False) -> None:
        """Show text in the single-line footer log (kept out of the history)."""
        if not hasattr(self, "lbl_log"):
            return
        if not count_msg:
            self._counting_visible = False
        self.lbl_log.setText(text)
        self.lbl_log.setCursorPosition(len(text))
        self.lbl_log.setToolTip("\n".join(self._log_history[-12:]) or text)

    def _show_log(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(760, 480)
        lay = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(self._log_history))
        lay.addWidget(view)
        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton("复制全部")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(self._log_history)))
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        dialog.exec()

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "使用说明",
            "1. 素材根目录选择碧蓝航线的 AssetBundles 文件夹（包含 painting、paintingface、dependencies，"
            "以及 ui\\commonui_atlas）。\n"
            "2. 导出目录选择保存位置。\n"
            "3. 在左侧列表中勾选要导出的立绘（可用搜索/筛选快速定位，例如只看 _rw）。\n"
            "4. 右侧“差分表情”里会列出该立绘的全部表情：\n"
            "   · 0 号是无表情底图，默认勾选；勾 1/2/3 才会导出对应表情；\n"
            "   · 单击某个表情，上方预览立即合成；预览支持滚轮缩放、拖动平移；\n"
            "   · 勾选哪些，导出时就只导出哪些（不勾选则只出无表情的立绘）；\n"
            "   · “应用到全部”可把当前勾选套用到所有立绘（例如只导出 3 号表情）；\n"
            "   · 表情位置不对时，用下方的横向/纵向偏移微调（正数向右/向上），可点箭头或直接输入。\n"
            "5. 点击“开始导出”：勾选几项就输出几张完整立绘，统一放在“导出目录\\立绘名\\”下。\n"
            "   没有勾选任何立绘时，会直接导出你当前预览的那一个。\n"
            "6. 工具条“已勾选”可以只看已勾选的立绘，方便导出前核对清单。\n\n"
            "提示：\n"
            "· “游戏布局”按游戏内 UI 坐标输出（默认）；“原始纹理”按素材原生尺寸输出。\n"
            "· PNG 压缩默认 0（不压缩、保存最快），需要小体积可调到 6～9。\n"
            "· 首次导出请先小批量验证；失败项会在日志中给出原因。\n"
            "· 勾选“跳过已存在的文件”可断点续跑：只跳过已存在的单个文件，缺哪个补哪个。",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "把原本需要三个软件的操作合并为一步：<br>"
            "· AssetStudioMod（解析 AssetBundle / Mesh / Texture2D）<br>"
            "· AzurLanePaintingExtract（网格还原为图片）<br>"
            "· AzurLanePaintingAnalysis-Kt（布局合成与差分表情）<br><br>"
            "三者均为 MIT 许可的开源项目，本项目基于其算法重新实现。",
        )


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分"


def main() -> int:
    multiprocessing.freeze_support()
    Image.MAX_IMAGE_PIXELS = None
    if "--diag" in sys.argv:
        return run_diag(sys.argv)
    if "--cli" in sys.argv:
        return run_cli(sys.argv)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    return app.exec()


def run_diag(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="diag")
    parser.add_argument("--diag", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--name", default="2b")
    args = parser.parse_args(argv[1:])
    try:
        import UnityPy

        print("UnityPy", UnityPy.__version__)
        src = AssetSource(args.root)
        print("deps", len(src.table))
        analysis = core.analyze(src, "painting/" + args.name)
        print("parts", [(p.name, p.size) for p in analysis.parts])

        for p in analysis.parts:
            tex = p.sprite.m_RD.texture.read()
            print("tex", tex.m_Name, tex.m_Width, tex.m_Height, tex.m_TextureFormat)
            img = tex.image
            print("img", img)
            part = core.rebuild_part(p)
            print("part", None if part is None else part.size)

        # frozen multiprocessing / preview path
        with ProcessPoolExecutor(max_workers=1, initializer=worker.init, initargs=(args.root,)) as pool:
            res = pool.submit(worker.preview_job, {"root": args.root, "name": args.name}).result()
        print("preview", res.get("ok"), res.get("width"), res.get("height"), len(res.get("png") or b""), res.get("error"))
        for f in (res.get("faces") or [])[:3]:
            print("  face", f["name"], len(f["png"]), f["rect"])
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="AzurLanePaintingExport", description="批处理导出（无界面）")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--root", required=True, help="AssetBundles 根目录")
    parser.add_argument("--out", required=True, help="导出目录")
    parser.add_argument("--name", action="append", default=[], help="只导出指定立绘（可重复）")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少个")
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)))
    parser.add_argument("--no-faces", action="store_true", help="不导出差分表情")
    parser.add_argument("--face-names", default="", help="只导出指定差分表情，逗号分隔，例如 3 或 1,3,5")
    parser.add_argument("--parts", action="store_true", help="额外导出各部件")
    parser.add_argument("--base", action="store_true", help="同时导出无表情底图")
    parser.add_argument("--ui-scale", action="store_true",
                        help="按游戏布局尺寸输出（放大版，背景为上采样）")
    parser.add_argument("--crop", action="store_true", help="裁剪透明边")
    parser.add_argument("--level", type=int, default=0, help="PNG 压缩等级 0-9（默认 0 不压缩）")
    args = parser.parse_args(argv[1:])

    names = args.name or AssetSource.list_paintings_static(args.root)
    if args.limit:
        names = names[: args.limit]
    face_names = [s.strip() for s in args.face_names.replace("，", ",").split(",") if s.strip()]
    opts = {
        "root": args.root,
        "out_dir": args.out,
        "faces": not args.no_faces,
        "parts": args.parts,
        "crop": args.crop,
        "level": args.level,
        "native": not args.ui_scale,
        "face_names": face_names or None,
        "export_base": args.base,
    }
    ok = 0
    failed = 0
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=worker.init, initargs=(args.root,)) as pool:
        futures = {pool.submit(worker.export_job, dict(opts, name=n)): n for n in names}

        for i, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            if result["ok"]:
                ok += 1
            else:
                failed += 1
                print(f"[失败] {result['name']}: {result['error']}")
            if i % 20 == 0 or i == len(futures):
                speed = i / max(1e-6, time.time() - started)
                print(f"{i}/{len(futures)} 成功 {ok} 失败 {failed} ({speed:.2f} 项/秒)")
    print(f"完成：成功 {ok}，失败 {failed}，用时 {format_duration(time.time() - started)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
