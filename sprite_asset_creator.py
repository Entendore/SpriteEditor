#!/usr/bin/env python3
"""
Sprite Asset Creator v2 — Professional PySide6 Desktop Tool
Creates sprite sheets and export metadata for Unity, Godot, Unreal Engine, and generic JSON+PNG.

Requirements:  pip install PySide6 Pillow
"""

import sys, os, json, math, uuid, copy, time, traceback
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QSplitter, QTabWidget, QGroupBox, QLabel, QPushButton, QComboBox,
    QSpinBox, QDoubleSpinBox, QListWidget, QListWidgetItem, QSlider,
    QFileDialog, QMessageBox, QDialog, QFormLayout, QCheckBox, QLineEdit,
    QScrollArea, QStatusBar, QToolBar, QMenu, QMenuBar, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem, QAbstractItemView,
    QSizePolicy, QDockWidget, QProgressBar, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QToolButton, QTextEdit, QInputDialog
)
from PySide6.QtCore import (
    Qt, QTimer, QSize, QRect, QRectF, QPoint, QPointF, Signal, Slot, QMimeData,
    QPropertyAnimation, QEasingCurve, QBuffer, QIODevice, QObject, QSettings
)
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QBrush, QIcon, QAction,
    QDragEnterEvent, QDropEvent, QFont, QKeySequence, QTransform,
    QPixmapCache, QShortcut, QTextCursor
)

from PIL import Image

# Disable PIL's decompression bomb warning for large sprite sheets
Image.MAX_IMAGE_PIXELS = None

# ═══════════════════════════════════════════════════════════════════════════════
#  DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SpriteFrame:
    name: str = "frame"
    image: object = None          # PIL Image (RGBA)
    rect: tuple = (0, 0, 0, 0)   # source rect
    filepath: str = ""
    pivot_x: float = 0.5
    pivot_y: float = 0.5
    border: tuple = (0, 0, 0, 0) # left, bottom, right, top  (9-slice)
    tag: str = ""                 # user tag for filtering
    trimmed: bool = False
    trim_offset: tuple = (0, 0)   # offset before trim

    def copy(self):
        return SpriteFrame(
            name=self.name, image=self.image.copy() if self.image else None,
            rect=self.rect, filepath=self.filepath,
            pivot_x=self.pivot_x, pivot_y=self.pivot_y,
            border=self.border, tag=self.tag,
            trimmed=self.trimmed, trim_offset=self.trim_offset,
        )


@dataclass
class Animation:
    name: str = "idle"
    fps: float = 12.0
    loop: bool = True
    frames: list = field(default_factory=list)  # indices into global frame list
    per_frame_duration: list = field(default_factory=list)  # ms overrides per frame

    def effective_durations(self):
        base = int(1000 / self.fps) if self.fps > 0 else 83
        result = []
        for i, idx in enumerate(self.frames):
            if i < len(self.per_frame_duration) and self.per_frame_duration[i] > 0:
                result.append(self.per_frame_duration[i])
            else:
                result.append(base)
        return result

    def to_dict(self):
        return {"name": self.name, "fps": self.fps, "loop": self.loop,
                "frames": self.frames,
                "per_frame_duration": self.per_frame_duration}


@dataclass
class Project:
    frames: list = field(default_factory=list)
    animations: list = field(default_factory=list)
    pack_padding: int = 1
    pack_pow2: bool = False
    pack_max_width: int = 2048
    pixels_per_unit: int = 100

    def to_json(self):
        d = {"frames": [], "animations": [], "settings": {
            "pack_padding": self.pack_padding, "pack_pow2": self.pack_pow2,
            "pack_max_width": self.pack_max_width, "pixels_per_unit": self.pixels_per_unit}}
        for f in self.frames:
            d["frames"].append({"name": f.name, "rect": f.rect, "filepath": f.filepath,
                                 "pivot_x": f.pivot_x, "pivot_y": f.pivot_y,
                                 "border": f.border, "tag": f.tag})
        for a in self.animations:
            d["animations"].append(a.to_dict())
        return d

    @staticmethod
    def save(path, project):
        with open(path, "w") as fp:
            json.dump(project.to_json(), fp, indent=2)

    @staticmethod
    def load(path):
        with open(path) as fp:
            d = json.load(fp)
        p = Project()
        s = d.get("settings", {})
        p.pack_padding = s.get("pack_padding", 1)
        p.pack_pow2 = s.get("pack_pow2", False)
        p.pack_max_width = s.get("pack_max_width", 2048)
        p.pixels_per_unit = s.get("pixels_per_unit", 100)
        return p, d.get("frames", []), d.get("animations", [])


# ═══════════════════════════════════════════════════════════════════════════════
#  BIN-PACKING (Shelf algorithm)
# ═══════════════════════════════════════════════════════════════════════════════

def _next_pow2(n):
    if n <= 0: return 1
    p = 1
    while p < n: p <<= 1
    return p


def pack_sprites(frames, padding=1, power_of_two=False, max_width=2048):
    if not frames:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), []

    items = []
    for i, frame in enumerate(frames):
        img = frame.image
        w, h = img.size
        items.append({"index": i, "name": frame.name, "w": w, "h": h, "image": img})
    # Sort by area descending for better packing
    items.sort(key=lambda it: it["w"] * it["h"], reverse=True)

    shelf_y = shelf_x = shelf_height = max_y = 0
    canvas_width = max_width
    placements = []

    for item in items:
        iw, ih = item["w"] + padding, item["h"] + padding
        if shelf_x + iw > canvas_width:
            shelf_y += shelf_height
            shelf_x = 0
            shelf_height = 0
        if shelf_y + ih > canvas_width * 4:
            canvas_width = min(canvas_width * 2, 8192)

        placements.append({"index": item["index"], "name": item["name"], "x": shelf_x, "y": shelf_y,
                           "w": item["w"], "h": item["h"], "rotated": False,
                           "image": item["image"]})
        shelf_x += iw
        shelf_height = max(shelf_height, ih)
        max_y = max(max_y, shelf_y + ih)

    # Calculate actual used bounds instead of using raw max_width
    used_w = max((p["x"] + p["w"] for p in placements), default=1)
    used_h = max_y
    
    if power_of_two:
        used_w, used_h = _next_pow2(used_w), _next_pow2(used_h)
        
    canvas_w = min(used_w, max_width)
    canvas_h = used_h

    atlas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    
    # Re-sort placements by original index to ensure metadata matches frames list
    placements.sort(key=lambda p: p["index"])
    
    results = []
    for p in placements:
        atlas.paste(p["image"], (p["x"], p["y"]))
        results.append({"name": p["name"], "x": p["x"], "y": p["y"],
                        "w": p["w"], "h": p["h"], "rotated": p["rotated"]})
    return atlas, results


# ═══════════════════════════════════════════════════════════════════════════════
#  UNDO COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

class AddFramesCommand(QUndoCommand):
    def __init__(self, model, frames, description="Add Frames"):
        super().__init__(description)
        self._model = model
        self._frames = frames
        self._start_idx = 0

    def redo(self):
        self._start_idx = len(self._model._frames)
        self._model._frames.extend(self._frames)
        self._model.frames_changed.emit()

    def undo(self):
        del self._model._frames[self._start_idx:self._start_idx + len(self._frames)]
        self._model.frames_changed.emit()


class RemoveFramesCommand(QUndoCommand):
    def __init__(self, model, indices, description="Remove Frames"):
        super().__init__(description)
        self._model = model
        self._removed = [(i, model._frames[i].copy()) for i in sorted(indices, reverse=True)]
        self._indices = sorted(indices)

    def redo(self):
        for i in sorted(self._indices, reverse=True):
            del self._model._frames[i]
        self._model.frames_changed.emit()

    def undo(self):
        for i, frame in sorted(self._removed, key=lambda x: x[0]):
            self._model._frames.insert(i, frame)
        self._model.frames_changed.emit()


class ReorderFramesCommand(QUndoCommand):
    def __init__(self, model, old_order, new_order, description="Reorder Frames"):
        super().__init__(description)
        self._model = model
        self._old = old_order
        self._new = new_order

    def redo(self):
        self._model._frames = [self._model._frames[i] for i in self._new]
        self._model.frames_changed.emit()

    def undo(self):
        # Inverse permutation
        inv = [0] * len(self._new)
        for new_pos, old_pos in enumerate(self._new):
            inv[old_pos] = new_pos
        self._model._frames = [self._model._frames[i] for i in inv]
        self._model.frames_changed.emit()


class ModifyFrameCommand(QUndoCommand):
    def __init__(self, model, index, new_frame, description="Modify Frame"):
        super().__init__(description)
        self._model = model
        self._index = index
        self._new_frame = new_frame
        self._old_frame = model._frames[index].copy()

    def redo(self):
        if 0 <= self._index < len(self._model._frames):
            self._model._frames[self._index] = self._new_frame
            self._model.frames_changed.emit()

    def undo(self):
        if 0 <= self._index < len(self._model._frames):
            self._model._frames[self._index] = self._old_frame
            self._model.frames_changed.emit()


class BatchModifyFramesCommand(QUndoCommand):
    def __init__(self, model, updates, description="Batch Modify Frames"):
        super().__init__(description)
        self._model = model
        self._updates = updates  # List of (index, new_frame)
        self._old = [(i, model._frames[i].copy()) for i, _ in updates]

    def redo(self):
        for i, new_f in self._updates:
            if 0 <= i < len(self._model._frames):
                self._model._frames[i] = new_f
        self._model.frames_changed.emit()

    def undo(self):
        for i, old_f in self._old:
            if 0 <= i < len(self._model._frames):
                self._model._frames[i] = old_f
        self._model.frames_changed.emit()


class ClearAllCommand(QUndoCommand):
    def __init__(self, model, description="Clear All"):
        super().__init__(description)
        self._model = model
        self._old_frames = [f.copy() for f in model._frames]
        self._old_anims = [copy.deepcopy(a) for a in model._animations]

    def redo(self):
        self._model._frames.clear()
        self._model._animations.clear()
        self._model._atlas_image = None
        self._model._atlas_regions = []
        self._model.frames_changed.emit()
        self._model.animations_changed.emit()

    def undo(self):
        self._model._frames = self._old_frames
        self._model._animations = self._old_anims
        self._model.frames_changed.emit()
        self._model.animations_changed.emit()


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCUMENT MODEL (central data + undo)
# ═══════════════════════════════════════════════════════════════════════════════

class DocumentModel(QObject):
    frames_changed = Signal()
    animations_changed = Signal()
    atlas_changed = Signal()

    def __init__(self):
        super().__init__()
        self._frames: List[SpriteFrame] = []
        self._animations: List[Animation] = []
        self._atlas_image = None
        self._atlas_regions = []
        self.undo_stack = QUndoStack(self)
        self._project_path = ""

    @property
    def frames(self): return self._frames
    @property
    def animations(self): return self._animations
    @property
    def atlas_image(self): return self._atlas_image
    @property
    def atlas_regions(self): return self._atlas_regions

    def push(self, cmd):
        self.undo_stack.push(cmd)

    # ── trim ──
    def auto_trim_frame(self, index, threshold=0):
        f = self._frames[index]
        img = f.image.convert("RGBA")
        bbox = img.getbbox()
        if bbox is None:
            return None  # Fully transparent
        ox, oy, ox2, oy2 = bbox
        cropped = img.crop(bbox)
        new_f = f.copy()
        new_f.image = cropped
        new_f.rect = (0, 0, cropped.width, cropped.height)
        new_f.trimmed = True
        new_f.trim_offset = (f.rect[0] + ox, f.rect[1] + oy)
        return new_f


# ═══════════════════════════════════════════════════════════════════════════════
#  STYLESHEET
# ═══════════════════════════════════════════════════════════════════════════════
# (Same as original, omitted here for brevity, but present in the final script)
STYLE = """
/* ── Global ── */
QWidget {
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
    color: #e0e0e0;
}
QWidget:disabled { color: #666; }

QMainWindow { background: #1e1e2e; }
QDialog { background: #2a2a3c; }

/* ── Scrollbars ── */
QScrollBar:vertical {
    background: #1e1e2e; width: 10px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #45475a; min-height: 30px; border-radius: 5px;
}
QScrollBar::handle:vertical:hover { background: #585b70; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

QScrollBar:horizontal {
    background: #1e1e2e; height: 10px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #45475a; min-width: 30px; border-radius: 5px;
}
QScrollBar::handle:horizontal:hover { background: #585b70; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }

/* ── GroupBox ── */
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 8px;
    margin-top: 12px;
    padding: 16px 10px 10px 10px;
    font-weight: bold;
    background: #2a2a3c;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #89b4fa;
}

/* ── Buttons ── */
QPushButton {
    background: #45475a;
    border: 1px solid #585b70;
    border-radius: 6px;
    padding: 6px 16px;
    color: #cdd6f4;
    font-weight: bold;
    min-height: 22px;
}
QPushButton:hover { background: #585b70; border-color: #89b4fa; }
QPushButton:pressed { background: #313244; }
QPushButton:disabled { background: #313244; color: #585b70; border-color: #45475a; }
QPushButton:focus { border-color: #89b4fa; outline: none; }

QPushButton[primary="true"] {
    background: #89b4fa;
    color: #1e1e2e;
    border-color: #89b4fa;
}
QPushButton[primary="true"]:hover { background: #b4d0fb; }
QPushButton[primary="true"]:pressed { background: #74a4f7; }

QPushButton[danger="true"] {
    background: #f38ba8;
    color: #1e1e2e;
    border-color: #f38ba8;
}
QPushButton[danger="true"]:hover { background: #f5a3ba; }

/* ── ToolButton ── */
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
    color: #cdd6f4;
    font-weight: bold;
}
QToolButton:hover { background: #45475a; border-color: #585b70; }
QToolButton:pressed { background: #313244; }

/* ── Toolbar ── */
QToolBar {
    background: #1e1e2e;
    border-bottom: 1px solid #313244;
    spacing: 4px;
    padding: 4px;
}
QToolBar QToolButton {
    padding: 6px 12px;
    font-size: 12px;
}

/* ── Menu ── */
QMenuBar {
    background: #1e1e2e;
    border-bottom: 1px solid #313244;
    color: #cdd6f4;
    padding: 2px;
}
QMenuBar::item { padding: 6px 12px; border-radius: 4px; }
QMenuBar::item:selected { background: #45475a; }
QMenu {
    background: #2a2a3c;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 6px 32px 6px 16px; border-radius: 4px; }
QMenu::item:selected { background: #45475a; }
QMenu::separator { height: 1px; background: #45475a; margin: 4px 8px; }

/* ── List / Tree ── */
QListWidget, QTreeWidget {
    background: #181825;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 2px;
    outline: none;
}
QListWidget::item, QTreeWidget::item {
    padding: 4px 6px;
    border-radius: 4px;
    margin: 1px 2px;
}
QListWidget::item:selected, QTreeWidget::item:selected {
    background: #45475a;
    color: #89b4fa;
}
QListWidget::item:hover, QTreeWidget::item:hover {
    background: #313244;
}

/* ── Input ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #181825;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 5px 8px;
    color: #cdd6f4;
    min-height: 22px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #89b4fa;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background: #2a2a3c;
    border: 1px solid #45475a;
    selection-background-color: #45475a;
    color: #cdd6f4;
}

/* ── TabWidget ── */
QTabWidget::pane {
    border: 1px solid #45475a;
    border-radius: 6px;
    background: #1e1e2e;
}
QTabBar::tab {
    background: #2a2a3c;
    border: 1px solid #45475a;
    border-bottom: none;
    padding: 8px 18px;
    margin-right: 2px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    color: #a6adc8;
    font-weight: bold;
}
QTabBar::tab:selected {
    background: #1e1e2e;
    color: #89b4fa;
    border-color: #89b4fa;
}
QTabBar::tab:hover:!selected { background: #313244; color: #cdd6f4; }

/* ── Slider ── */
QSlider::groove:horizontal {
    background: #313244;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #89b4fa;
    width: 16px; height: 16px;
    margin: -5px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover { background: #b4d0fb; }

/* ── CheckBox ── */
QCheckBox { spacing: 8px; color: #cdd6f4; }
QCheckBox::indicator {
    width: 18px; height: 18px;
    border: 2px solid #585b70;
    border-radius: 4px;
    background: #181825;
}
QCheckBox::indicator:checked {
    background: #89b4fa;
    border-color: #89b4fa;
}
QCheckBox::indicator:hover { border-color: #89b4fa; }

/* ── StatusBar ── */
QStatusBar {
    background: #181825;
    border-top: 1px solid #313244;
    color: #a6adc8;
    padding: 2px 8px;
}
QStatusBar QLabel { color: #a6adc8; }

/* ── ProgressBar ── */
QProgressBar {
    background: #313244;
    border: none;
    border-radius: 4px;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #89b4fa, stop:1 #a6e3a1);
    border-radius: 4px;
}

/* ── DockWidget ── */
QDockWidget {
    color: #cdd6f4;
    font-weight: bold;
    titlebar-close-icon: none;
}
QDockWidget::title {
    background: #2a2a3c;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    margin: 2px;
}
QDockWidget::close-button, QDockWidget::float-button {
    background: #45475a;
    border: 1px solid #585b70;
    border-radius: 3px;
    padding: 0;
    width: 16px; height: 16px;
    margin: 2px;
}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {
    background: #585b70;
}

/* ── Splitter ── */
QSplitter::handle { background: #313244; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical { height: 3px; }

/* ── Tooltip ── */
QToolTip {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #585b70;
    border-radius: 6px;
    padding: 6px 10px;
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  SPRITE CANVAS
# ═══════════════════════════════════════════════════════════════════════════════

class SpriteCanvas(QGraphicsView):
    region_hovered = Signal(str)
    region_clicked = Signal(str)
    zoom_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 46)))

        self._pixmap_item = None
        self._grid_rects = []
        self._grid_items = []
        self._highlight_item = None
        self._show_grid = False
        self._zoom = 1.0
        self._minimap_enabled = True
        self._region_names = []

    def set_pixmap(self, pixmap):
        self._scene.clear()
        self._grid_items.clear()
        self._highlight_item = None
        if pixmap and not pixmap.isNull():
            self._pixmap_item = self._scene.addPixmap(pixmap)
            self._scene.setSceneRect(QRectF(pixmap.rect()))
        else:
            self._pixmap_item = None

    def set_grid(self, rects):
        self._grid_rects = [QRectF(r) for r in rects]
        self._draw_grid()

    def set_show_grid(self, show):
        self._show_grid = show
        self._draw_grid()

    def _draw_grid(self):
        for item in self._grid_items:
            self._scene.removeItem(item)
        self._grid_items.clear()
        self._highlight_item = None

        if not self._grid_rects:
            return

        for idx, r in enumerate(self._grid_rects):
            if self._show_grid:
                pen = QPen(QColor(137, 180, 250, 120), 1.5, Qt.PenStyle.DashLine)
                item = self._scene.addRect(r, pen, QBrush(QColor(137, 180, 250, 20)))
                item.setZValue(1)
                self._grid_items.append(item)

            rect = self._scene.addRect(r, QPen(Qt.PenStyle.NoPen), QBrush(QColor(0, 0, 0, 0)))
            rect.setZValue(2)
            rect.setAcceptHoverEvents(True)
            rect.setData(0, idx)  # Store index directly for fast lookup
            self._grid_items.append(rect)

    def _find_region_at(self, pos: QPointF):
        for item in self._scene.items(pos):
            idx = item.data(0)
            if idx is not None and isinstance(idx, int) and 0 <= idx < len(self._grid_rects):
                if self._grid_rects[idx].contains(pos):
                    return idx
        return -1

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.position())
        idx = self._find_region_at(scene_pos)
        if idx >= 0:
            region = self._grid_rects[idx]
            if self._highlight_item:
                self._highlight_item.setRect(region)
            else:
                pen = QPen(QColor(166, 227, 161, 200), 2, Qt.PenStyle.SolidLine)
                self._highlight_item = self._scene.addRect(region, pen,
                                                            QBrush(QColor(166, 227, 161, 30)))
                self._highlight_item.setZValue(3)
        elif self._highlight_item:
            self._scene.removeItem(self._highlight_item)
            self._highlight_item = None
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._scene.mouseGrabberItem():
            scene_pos = self.mapToScene(event.position())
            idx = self._find_region_at(scene_pos)
            if idx >= 0 and idx < len(self._region_names):
                self.region_clicked.emit(self._region_names[idx])
        super().mousePressEvent(event)

    def set_region_names(self, names):
        self._region_names = names

    def wheelEvent(self, event):
        factor = 1.2
        if event.angleDelta().y() > 0:
            self._zoom = min(self._zoom * factor, 30.0)
        else:
            self._zoom = max(self._zoom / factor, 0.02)
        self.setTransform(QTransform().scale(self._zoom, self._zoom))
        self.zoom_changed.emit(self._zoom)

    def fit_to_view(self):
        if self._pixmap_item:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            # Recalculate zoom level after fit
            transform = self.transform()
            self._zoom = transform.m11()
            self.zoom_changed.emit(self._zoom)

    def reset_zoom(self):
        self._zoom = 1.0
        self.setTransform(QTransform())
        self.zoom_changed.emit(1.0)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._minimap_enabled and self._pixmap_item and self._zoom > 1.05:
            self._draw_minimap()

    def _draw_minimap(self):
        if not self._pixmap_item:
            return
        pm = self._pixmap_item.pixmap()
        if pm.isNull():
            return

        mm_w, mm_h = 140, 100
        mm_x = self.viewport().width() - mm_w - 12
        mm_y = self.viewport().height() - mm_h - 12

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        painter.setPen(QPen(QColor(69, 71, 90), 1))
        painter.setBrush(QBrush(QColor(24, 24, 37, 200)))
        painter.drawRoundedRect(mm_x - 2, mm_y - 2, mm_w + 4, mm_h + 4, 6, 6)

        scaled = pm.scaled(mm_w, mm_h, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.FastTransformation)
        ox = mm_x + (mm_w - scaled.width()) // 2
        oy = mm_y + (mm_h - scaled.height()) // 2
        painter.drawImage(QPoint(ox, oy), scaled.toImage())

        vp = self.mapToScene(self.viewport().rect()).boundingRect()
        scene_rect = self._scene.sceneRect()
        if scene_rect.width() > 0 and scene_rect.height() > 0:
            scale_x = scaled.width() / scene_rect.width()
            scale_y = scaled.height() / scene_rect.height()
            vx = ox + int(vp.x() * scale_x)
            vy = oy + int(vp.y() * scale_y)
            vw = max(4, int(vp.width() * scale_x))
            vh = max(4, int(vp.height() * scale_y))
            painter.setPen(QPen(QColor(137, 180, 250), 1.5))
            painter.setBrush(QBrush(QColor(137, 180, 250, 40)))
            painter.drawRect(vx, vy, vw, vh)

        painter.end()


# ═══════════════════════════════════════════════════════════════════════════════
#  ANIMATION PREVIEW
# ═══════════════════════════════════════════════════════════════════════════════

class AnimationPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(256, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmaps = []
        self._durations = []
        self._current_frame = 0
        self._fps = 12
        self._loop = True
        self._playing = False
        self._onion_skin = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance_frame)

    def set_animation(self, pixmaps, durations=None, fps=12, loop=True):
        self._pixmaps = pixmaps or []
        self._durations = durations or []
        self._fps = fps
        self._loop = loop
        self._current_frame = 0
        self.update()

    def play(self):
        if self._pixmaps:
            self._playing = True
            if self._durations and 0 <= self._current_frame < len(self._durations):
                interval = self._durations[self._current_frame]
            else:
                interval = int(1000 / self._fps) if self._fps > 0 else 83
            self._timer.start(max(16, interval))

    def pause(self):
        self._playing = False
        self._timer.stop()

    def stop(self):
        self._playing = False
        self._timer.stop()
        self._current_frame = 0
        self.update()

    def scrub_to(self, frame_idx):
        if 0 <= frame_idx < len(self._pixmaps):
            self._current_frame = frame_idx
            self.update()

    def set_onion_skin(self, enabled):
        self._onion_skin = enabled
        self.update()

    def _advance_frame(self):
        if not self._pixmaps:
            return
        if self._durations and 0 <= self._current_frame < len(self._durations):
            self._timer.setInterval(max(16, self._durations[self._current_frame]))
        self._current_frame += 1
        if self._current_frame >= len(self._pixmaps):
            if self._loop:
                self._current_frame = 0
            else:
                self._current_frame = len(self._pixmaps) - 1
                self.pause()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._draw_checkerboard(painter)

        if not self._pixmaps:
            painter.setPen(QColor(100, 100, 100))
            painter.setFont(QFont("Sans", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Select an animation to preview")
            painter.end()
            return

        if self._onion_skin and self._current_frame > 0:
            prev = self._pixmaps[self._current_frame - 1]
            scaled = prev.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.setOpacity(0.25)
            painter.drawPixmap(x, y, scaled)
            painter.setOpacity(1.0)

        pm = self._pixmaps[self._current_frame]
        scaled = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

        painter.setPen(QColor(200, 200, 200))
        painter.setFont(QFont("Monospace", 10))
        text = f"Frame {self._current_frame + 1}/{len(self._pixmaps)}"
        painter.drawText(10, self.height() - 10, text)
        if self._playing:
            painter.setPen(QColor(166, 227, 161))
            painter.drawText(10, self.height() - 28, "PLAYING")
        painter.end()

    def _draw_checkerboard(self, painter):
        sz = 12
        w, h = self.width(), self.height()
        for cy in range(0, h, sz):
            for cx in range(0, w, sz):
                c = QColor(42, 42, 58) if (cx // sz + cy // sz) % 2 == 0 else QColor(50, 50, 66)
                painter.fillRect(cx, cy, sz, sz, c)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMELINE WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class TimelineWidget(QWidget):
    frame_selected = Signal(int)
    order_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setMaximumHeight(130)
        self._pixmaps = []
        self._durations = []
        self._current = -1
        self._frame_size = 64
        self._drag_idx = -1
        self.setMouseTracking(True)

    def set_frames(self, pixmaps, durations=None, current=0):
        self._pixmaps = list(pixmaps or [])
        self._durations = list(durations or [])
        self._current = min(current, max(0, len(self._pixmaps) - 1))
        self.update()
        self.setMinimumWidth(max(self.minimumWidth(), len(self._pixmaps) * (self._frame_size + 4) + 20))

    def set_current(self, idx):
        self._current = idx
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        fs = self._frame_size
        gap = 4
        total_w = len(self._pixmaps) * (fs + gap) + gap
        sx = max(gap, (w - total_w) // 2 + gap)
        sy = (h - fs - 22) // 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(24, 24, 37))
        painter.drawRoundedRect(0, 0, w, h, 8, 8)

        for i, pm in enumerate(self._pixmaps):
            x = sx + i * (fs + gap)
            if i == self._current:
                painter.setPen(QPen(QColor(137, 180, 250), 2))
                painter.setBrush(QColor(69, 71, 90))
            else:
                painter.setPen(QPen(QColor(69, 71, 90), 1))
                painter.setBrush(QColor(30, 30, 46))
            painter.drawRoundedRect(x - 2, sy - 2, fs + 4, fs + 4, 4, 4)

            thumb = pm.scaled(fs, fs, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            tx = x + (fs - thumb.width()) // 2
            ty = sy + (fs - thumb.height()) // 2
            painter.drawPixmap(tx, ty, thumb)

            painter.setPen(QColor(166, 166, 189))
            painter.setFont(QFont("Monospace", 8))
            painter.drawText(x, sy + fs + 14, str(i))

            if i < len(self._durations) and self._durations[i] > 0:
                painter.setPen(QColor(250, 179, 135))
                painter.drawText(x, sy + fs + 26, f"{self._durations[i]}ms")

        painter.end()

    def mousePressEvent(self, event):
        fs = self._frame_size
        gap = 4
        total_w = len(self._pixmaps) * (fs + gap) + gap
        sx = max(gap, (self.width() - total_w) // 2 + gap)
        sy = (self.height() - fs - 22) // 2

        for i in range(len(self._pixmaps)):
            x = sx + i * (fs + gap)
            if QRect(x - 2, sy - 2, fs + 4, fs + 4).contains(event.pos()):
                self._current = i
                self._drag_idx = i
                self.frame_selected.emit(i)
                self.update()
                return
        self._drag_idx = -1


# ═══════════════════════════════════════════════════════════════════════════════
#  THUMBNAIL GRID WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class ThumbnailGridWidget(QWidget):
    frame_double_clicked = Signal(int)
    selection_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmaps = []
        self._names = []
        self._selected = set()
        self._cell = 72
        self._gap = 4
        self.setMouseTracking(True)

    def set_frames(self, frames):
        self._pixmaps = []
        self._names = []
        self._selected.clear()
        for f in frames:
            pm = _pil_to_qpixmap(f.image) if f.image else QPixmap()
            self._pixmaps.append(pm)
            self._names.append(f.name)
        self.update()

    def get_selected_indices(self):
        return sorted(self._selected)

    def _idx_at(self, pos):
        cols = max(1, (self.width() - self._gap) // (self._cell + self._gap))
        col = (pos.x() - self._gap) // (self._cell + self._gap)
        row = (pos.y() - self._gap) // (self._cell + self._gap)
        idx = row * cols + col
        if 0 <= idx < len(self._pixmaps):
            # Check actual bounds of the cell
            x = self._gap + col * (self._cell + self._gap)
            y = self._gap + row * (self._cell + self._gap)
            if QRect(x, y, self._cell, self._cell).contains(pos):
                return idx
        return -1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cols = max(1, (self.width() - self._gap) // (self._cell + self._gap))
        for i, pm in enumerate(self._pixmaps):
            r, c = divmod(i, cols)
            x = self._gap + c * (self._cell + self._gap)
            y = self._gap + r * (self._cell + self._gap)
            if y + self._cell > self.height():
                break

            sel = i in self._selected
            if sel:
                painter.setPen(QPen(QColor(137, 180, 250), 2))
                painter.setBrush(QColor(69, 71, 90))
            else:
                painter.setPen(QPen(QColor(69, 71, 90), 1))
                painter.setBrush(QColor(30, 30, 46))
            painter.drawRoundedRect(x, y, self._cell, self._cell, 6, 6)

            if not pm.isNull():
                thumb = pm.scaled(self._cell - 8, self._cell - 20,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
                tx = x + (self._cell - thumb.width()) // 2
                ty = y + 4
                painter.drawPixmap(tx, ty, thumb)

            painter.setPen(QColor(166, 166, 189))
            painter.setFont(QFont("Sans", 7))
            painter.drawText(QRect(x, y + self._cell - 16, self._cell, 14),
                             Qt.AlignmentFlag.AlignCenter, self._names[i])
        painter.end()

    def mousePressEvent(self, event):
        idx = self._idx_at(event.pos())
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if idx >= 0:
                self._selected.symmetric_difference_update({idx})
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if idx >= 0:
                if self._selected:
                    lo = min(self._selected) 
                    hi = max(self._selected)
                    for j in range(min(lo, idx), max(hi, idx) + 1):
                        if j < len(self._pixmaps):
                            self._selected.add(j)
                else:
                    self._selected.add(idx)
        else:
            self._selected.clear()
            if idx >= 0:
                self._selected.add(idx)
        self.selection_changed.emit(sorted(self._selected))
        self.update()

    def mouseDoubleClickEvent(self, event):
        idx = self._idx_at(event.pos())
        if idx >= 0:
            self.frame_double_clicked.emit(idx)


# ═══════════════════════════════════════════════════════════════════════════════
#  ZOOM CONTROL BAR
# ═══════════════════════════════════════════════════════════════════════════════

class ZoomControlBar(QWidget):
    zoom_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(-50, 100)
        self._slider.setValue(0)
        self._slider.setFixedWidth(200)
        self._slider.setToolTip("Zoom level")
        self._slider.valueChanged.connect(self._on_slider)

        self._label = QLabel("100%")
        self._label.setFixedWidth(50)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        fit_btn = QPushButton("Fit")
        fit_btn.setFixedWidth(48)
        fit_btn.setToolTip("Fit to view (F)")
        fit_btn.clicked.connect(lambda: self.zoom_requested.emit(-1))

        actual_btn = QPushButton("1:1")
        actual_btn.setFixedWidth(42)
        actual_btn.setToolTip("Reset to 100% (0)")
        actual_btn.clicked.connect(lambda: self.zoom_requested.emit(1.0))

        layout.addWidget(QLabel("Zoom:"))
        layout.addWidget(self._slider)
        layout.addWidget(self._label)
        layout.addWidget(fit_btn)
        layout.addWidget(actual_btn)
        layout.addStretch()

    def set_zoom(self, level):
        if level < 0:
            return
        pct = level * 100
        if pct <= 100:
            val = int(pct) - 100
        else:
            val = int(math.log2(max(level, 1.01)) * 30)
        self._slider.blockSignals(True)
        self._slider.setValue(max(-50, min(100, val)))
        self._slider.blockSignals(False)
        self._label.setText(f"{int(pct)}%")

    def _on_slider(self, val):
        if val <= 0:
            z = 1.0 + val / 100.0
        else:
            z = 2.0 ** (val / 30.0)
        self._label.setText(f"{int(z * 100)}%")
        self.zoom_requested.emit(z)


# ═══════════════════════════════════════════════════════════════════════════════
#  TOAST NOTIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

class ToastNotification(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(300)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.finished.connect(self._on_anim_finished)

    def show_toast(self, message, duration=2500, color="#a6e3a1"):
        self._label = QLabel(message, self)
        self._label.setStyleSheet(f"""
            QLabel {{
                background: {color}; color: #1e1e2e;
                padding: 8px 18px; border-radius: 8px;
                font-weight: bold; font-size: 13px;
            }}
        """)
        self._label.adjustSize()
        self.setFixedSize(self._label.size())
        self._label.move(0, 0)

        if self.parent():
            pp = self.parent().mapToGlobal(QPoint(
                self.parent().width() - self.width() - 20, 60))
            self.move(pp)

        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(0.95)
        self._anim.start()
        self.show()
        self._timer.start(duration)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_finished(self):
        if self.windowOpacity() < 0.1:
            self.hide()


# ═══════════════════════════════════════════════════════════════════════════════
#  DIALOGS
# ═══════════════════════════════════════════════════════════════════════════════
# (Omitted for brevity, exactly the same as original)
class GridSliceDialog(QDialog):
    def __init__(self, image_width, image_height, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Grid Slice")
        self.setMinimumWidth(420)
        self._img_w, self._img_h = image_width, image_height
        self._syncing = False

        layout = QVBoxLayout(self)
        preview = QLabel(f"Source: {image_width} x {image_height} px")
        preview.setStyleSheet("color: #89b4fa; font-size: 12px; padding: 4px;")
        layout.addWidget(preview)

        form = QGridLayout()
        form.addWidget(QLabel("Columns:"), 0, 0)
        self.cols_spin = QSpinBox(); self.cols_spin.setRange(1, 256); self.cols_spin.setValue(max(1, 1))
        form.addWidget(self.cols_spin, 0, 1)

        form.addWidget(QLabel("Rows:"), 1, 0)
        self.rows_spin = QSpinBox(); self.rows_spin.setRange(1, 256); self.rows_spin.setValue(1)
        form.addWidget(self.rows_spin, 1, 1)

        form.addWidget(QLabel("Cell Width:"), 2, 0)
        self.cell_w = QSpinBox(); self.cell_w.setRange(1, 8192)
        self.cell_w.setValue(max(1, image_width))
        self.cell_w.valueChanged.connect(self._w_changed)
        form.addWidget(self.cell_w, 2, 1)

        form.addWidget(QLabel("Cell Height:"), 3, 0)
        self.cell_h = QSpinBox(); self.cell_h.setRange(1, 8192)
        self.cell_h.setValue(max(1, image_height))
        self.cell_h.valueChanged.connect(self._h_changed)
        form.addWidget(self.cell_h, 3, 1)

        form.addWidget(QLabel("Padding:"), 4, 0)
        self.pad_spin = QSpinBox(); self.pad_spin.setRange(0, 64)
        form.addWidget(self.pad_spin, 4, 1)

        form.addWidget(QLabel("Prefix:"), 5, 0)
        self.prefix = QLineEdit("frame")
        form.addWidget(self.prefix, 5, 1)
        layout.addLayout(form)

        auto_row = QHBoxLayout()
        auto_btn = QPushButton("Auto-detect 4x4")
        auto_btn.clicked.connect(lambda: (self.cell_w.setValue(image_width // 4),
                                           self.cell_h.setValue(image_height // 4)))
        auto_row.addWidget(auto_btn)
        auto_btn2 = QPushButton("Auto-detect 8x8")
        auto_btn2.clicked.connect(lambda: (self.cell_w.setValue(image_width // 8),
                                            self.cell_h.setValue(image_height // 8)))
        auto_row.addWidget(auto_btn2)
        layout.addLayout(auto_row)

        btns = QHBoxLayout()
        ok = QPushButton("Slice"); ok.setProperty("primary", True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def _w_changed(self, v):
        if self._syncing: return
        self._syncing = True
        self.cols_spin.setValue(max(1, self._img_w // v))
        self._syncing = False

    def _h_changed(self, v):
        if self._syncing: return
        self._syncing = True
        self.rows_spin.setValue(max(1, self._img_h // v))
        self._syncing = False

    def get_params(self):
        return {"cols": self.cols_spin.value(), "rows": self.rows_spin.value(),
                "cell_w": self.cell_w.value(), "cell_h": self.cell_h.value(),
                "padding": self.pad_spin.value(), "prefix": self.prefix.text() or "frame"}

class ExportDialog(QDialog):
    def __init__(self, parent=None, has_animations=False):
        super().__init__(parent)
        self.setWindowTitle("Export Sprite Atlas")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.engine = QComboBox()
        self.engine.addItems([
            "Unity  (Sprite Sheet + .meta)",
            "Godot  (.tres SpriteFrames)",
            "Unreal Engine  (TextureAtlas JSON)",
            "Generic  (JSON + PNG Atlas)",
            "Individual PNGs  (no atlas)",
        ])
        form.addRow("Target Engine:", self.engine)

        self.name_edit = QLineEdit("sprite_atlas")
        form.addRow("Output Name:", self.name_edit)

        self.pad_spin = QSpinBox(); self.pad_spin.setRange(0, 16); self.pad_spin.setValue(1)
        form.addRow("Padding:", self.pad_spin)

        self.pow2 = QCheckBox()
        form.addRow("Power of Two Size:", self.pow2)

        self.max_w = QSpinBox(); self.max_w.setRange(64, 8192); self.max_w.setValue(2048); self.max_w.setSingleStep(256)
        form.addRow("Max Atlas Width:", self.max_w)

        self.ppu_spin = QSpinBox(); self.ppu_spin.setRange(1, 10000); self.ppu_spin.setValue(100)
        self.ppu_spin.setToolTip("Pixels Per Unit (Unity) / scale reference")
        form.addRow("Pixels Per Unit:", self.ppu_spin)

        self.trim_check = QCheckBox()
        self.trim_check.setToolTip("Auto-trim transparent pixels before export")
        form.addRow("Auto-Trim Frames:", self.trim_check)
        layout.addLayout(form)

        btns = QHBoxLayout()
        ok = QPushButton("Export"); ok.setProperty("primary", True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def get_settings(self):
        return {"engine": self.engine.currentIndex(),
                "engine_name": self.engine.currentText(),
                "output_name": self.name_edit.text() or "sprite_atlas",
                "padding": self.pad_spin.value(),
                "power_of_two": self.pow2.isChecked(),
                "max_width": self.max_w.value(),
                "pixels_per_unit": self.ppu_spin.value(),
                "auto_trim": self.trim_check.isChecked()}

class AnimationEditDialog(QDialog):
    def __init__(self, anim=None, available_frames=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Animation")
        self.setMinimumSize(480, 500)
        available_frames = available_frames or []

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(anim.name if anim else "idle")
        form.addRow("Name:", self.name_edit)
        self.fps_spin = QDoubleSpinBox(); self.fps_spin.setRange(0.1, 120); self.fps_spin.setValue(anim.fps if anim else 12)
        self.fps_spin.setDecimals(1)
        form.addRow("FPS:", self.fps_spin)
        self.loop_check = QCheckBox(); self.loop_check.setChecked(anim.loop if anim else True)
        form.addRow("Loop:", self.loop_check)
        layout.addLayout(form)

        sel_layout = QHBoxLayout()
        self.all_list = QListWidget()
        self.all_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sel_list = QListWidget()
        self.sel_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        for name in available_frames:
            self.all_list.addItem(name)

        if anim:
            for idx in sorted(anim.frames, reverse=True):
                if 0 <= idx < self.all_list.count():
                    item = self.all_list.takeItem(idx)
                    self.sel_list.addItem(item)

        btn_col = QVBoxLayout()
        for text, slot in [(">>", self._add), ("<<", self._rem),
                           ("Up", self._up), ("Down", self._down)]:
            b = QPushButton(text); b.clicked.connect(slot); btn_col.addWidget(b)
        btn_col.addStretch()

        sel_layout.addWidget(QLabel("Available:")); sel_layout.addWidget(self.all_list, 1)
        sel_layout.addLayout(btn_col)
        sel_layout.addWidget(QLabel("Animation (ordered):")); sel_layout.addWidget(self.sel_list, 1)
        layout.addLayout(sel_layout)

        if anim and anim.per_frame_duration:
            dur_label = QLabel("Per-frame durations (ms, 0=auto):")
            layout.addWidget(dur_label)
            self.dur_edit = QLineEdit(",".join(str(d) for d in anim.per_frame_duration))
            layout.addWidget(self.dur_edit)
        else:
            self.dur_edit = None

        btns = QHBoxLayout()
        ok = QPushButton("OK"); ok.setProperty("primary", True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def _add(self):
        r = self.all_list.currentRow()
        if r >= 0: self.sel_list.addItem(self.all_list.takeItem(r))

    def _rem(self):
        r = self.sel_list.currentRow()
        if r >= 0: self.all_list.addItem(self.sel_list.takeItem(r))

    def _up(self):
        r = self.sel_list.currentRow()
        if r > 0:
            item = self.sel_list.takeItem(r)
            self.sel_list.insertItem(r - 1, item); self.sel_list.setCurrentRow(r - 1)

    def _down(self):
        r = self.sel_list.currentRow()
        if 0 <= r < self.sel_list.count() - 1:
            item = self.sel_list.takeItem(r)
            self.sel_list.insertItem(r + 1, item); self.sel_list.setCurrentRow(r + 1)

    def get_animation(self):
        a = Animation(self.name_edit.text(), self.fps_spin.value(), self.loop_check.isChecked())
        a.frames = list(range(self.sel_list.count()))
        if self.dur_edit:
            try:
                parts = [int(x.strip()) for x in self.dur_edit.text().split(",")]
                a.per_frame_duration = parts[:len(a.frames)]
                while len(a.per_frame_duration) < len(a.frames):
                    a.per_frame_duration.append(0)
            except ValueError:
                pass
        return a

class FramePropertiesDialog(QDialog):
    def __init__(self, frame: SpriteFrame, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Frame: {frame.name}")
        self.setMinimumWidth(360)
        self._frame = frame

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(frame.name)
        form.addRow("Name:", self.name_edit)

        self.tag_edit = QLineEdit(frame.tag)
        self.tag_edit.setToolTip("Tag for filtering (e.g. 'attack', 'item')")
        form.addRow("Tag:", self.tag_edit)

        self.pivot_x = QDoubleSpinBox(); self.pivot_x.setRange(0, 1); self.pivot_x.setSingleStep(0.05)
        self.pivot_x.setValue(frame.pivot_x); self.pivot_x.setDecimals(3)
        form.addRow("Pivot X:", self.pivot_x)

        self.pivot_y = QDoubleSpinBox(); self.pivot_y.setRange(0, 1); self.pivot_y.setSingleStep(0.05)
        self.pivot_y.setValue(frame.pivot_y); self.pivot_y.setDecimals(3)
        form.addRow("Pivot Y:", self.pivot_y)

        self.border_l = QSpinBox(); self.border_l.setRange(0, 999); self.border_l.setValue(frame.border[0])
        self.border_b = QSpinBox(); self.border_b.setRange(0, 999); self.border_b.setValue(frame.border[1])
        self.border_r = QSpinBox(); self.border_r.setRange(0, 999); self.border_r.setValue(frame.border[2])
        self.border_t = QSpinBox(); self.border_t.setRange(0, 999); self.border_t.setValue(frame.border[3])

        bord_grid = QGridLayout()
        bord_grid.addWidget(QLabel("L:"), 0, 0); bord_grid.addWidget(self.border_l, 0, 1)
        bord_grid.addWidget(QLabel("B:"), 0, 2); bord_grid.addWidget(self.border_b, 0, 3)
        bord_grid.addWidget(QLabel("R:"), 1, 0); bord_grid.addWidget(self.border_r, 1, 1)
        bord_grid.addWidget(QLabel("T:"), 1, 2); bord_grid.addWidget(self.border_t, 1, 3)
        form.addRow("9-Slice Borders:", bord_grid)
        layout.addLayout(form)

        if frame.image:
            pm = _pil_to_qpixmap(frame.image)
            pm = pm.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            preview_label = QLabel()
            preview_label.setPixmap(pm)
            preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(preview_label)

        btns = QHBoxLayout()
        ok = QPushButton("Apply"); ok.setProperty("primary", True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def get_values(self):
        return {"name": self.name_edit.text(), "tag": self.tag_edit.text(),
                "pivot_x": self.pivot_x.value(), "pivot_y": self.pivot_y.value(),
                "border": (self.border_l.value(), self.border_b.value(),
                           self.border_r.value(), self.border_t.value())}

class ImportAtlasDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Atlas")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Import sprite data from an existing atlas format."))

        self.format_combo = QComboBox()
        self.format_combo.addItems([
            "TexturePacker JSON",
            "Generic JSON (this tool's export)",
            "Aseprite JSON",
        ])
        form = QFormLayout()
        form.addRow("Format:", self.format_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel("Select the JSON metadata file:"))
        self.json_path = ""
        self.path_label = QLabel("(none)")
        self.path_label.setStyleSheet("color: #a6adc8; padding: 4px;")
        layout.addWidget(self.path_label)

        browse = QPushButton("Browse JSON...")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)

        layout.addWidget(QLabel("Select the accompanying PNG image:"))
        self.png_path = ""
        self.png_label = QLabel("(none)")
        self.png_label.setStyleSheet("color: #a6adc8; padding: 4px;")
        layout.addWidget(self.png_label)

        browse2 = QPushButton("Browse PNG...")
        browse2.clicked.connect(self._browse_png)
        layout.addWidget(browse2)

        btns = QHBoxLayout()
        ok = QPushButton("Import"); ok.setProperty("primary", True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select JSON", "", "JSON (*.json);;All (*)")
        if p:
            self.json_path = p; self.path_label.setText(p)

    def _browse_png(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select PNG", "", "PNG (*.png);;All (*)")
        if p:
            self.png_path = p; self.png_label.setText(p)

    def get_paths(self):
        return self.json_path, self.png_path, self.format_combo.currentIndex()


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPER: PIL → QPixmap
# ═══════════════════════════════════════════════════════════════════════════════

def _pil_to_qpixmap(img):
    if img is None:
        return QPixmap()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    data = img.tobytes()
    qimg = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class SpriteAssetCreator(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sprite Asset Creator v2")
        self.setMinimumSize(1360, 860)
        self.resize(1500, 950)
        self.setAcceptDrops(True)

        self._model = DocumentModel()
        self._toast = ToastNotification(self)
        self._recent_files = self._load_recent()

        self._build_menus()
        self._build_toolbar()
        self._build_central()
        self._build_docks()
        self._build_statusbar()
        self._connect()
        
        # Restore settings
        s = QSettings("SpriteAssetCreator", "v2")
        self.restoreGeometry(s.value("geometry", b""))
        self.restoreState(s.value("state", b""))
        
        self._update_empty_state()

    # ── Menus ──

    def _build_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("&File")
        self._a_open_frames = self._action(fm, "Add Frames...", "Ctrl+O", self._on_add_frames)
        self._a_load_sheet = self._action(fm, "Load Sprite Sheet...", "Ctrl+Shift+O", self._on_load_sheet)
        self._a_import_atlas = self._action(fm, "Import Atlas JSON...", "Ctrl+I", self._on_import_atlas)
        fm.addSeparator()
        self._a_save_proj = self._action(fm, "Save Project", "Ctrl+S", self._on_save_project)
        self._a_open_proj = self._action(fm, "Open Project...", "Ctrl+Shift+S", self._on_open_project)
        fm.addSeparator()
        self._recent_menu = fm.addMenu("Recent Files")
        self._rebuild_recent_menu()
        fm.addSeparator()
        self._a_clear = self._action(fm, "Clear All", "", self._on_clear_all)
        fm.addSeparator()
        self._a_quit = self._action(fm, "Quit", "Ctrl+Q", self.close)

        em = mb.addMenu("&Edit")
        self._a_undo = self._action(em, "Undo", "Ctrl+Z", self._model.undo_stack.undo)
        self._a_redo = self._action(em, "Redo", "Ctrl+Shift+Z", self._model.undo_stack.redo)
        em.addSeparator()
        self._a_remove = self._action(em, "Remove Selected", "Delete", self._on_remove_frame)
        self._a_dup = self._action(em, "Duplicate Selected", "Ctrl+D", self._on_duplicate_frame)
        self._a_rename = self._action(em, "Rename Frame", "F2", self._on_rename_frame)
        em.addSeparator()
        self._a_select_all = self._action(em, "Select All", "Ctrl+A", self._on_select_all)
        em.addSeparator()
        self._a_trim = self._action(em, "Auto-Trim Selected", "Ctrl+T", self._on_trim_frames)
        self._a_trim_all = self._action(em, "Auto-Trim All Frames", "Ctrl+Shift+T", self._on_trim_all_frames)
        self._a_props = self._action(em, "Frame Properties...", "Ctrl+P", self._on_frame_properties)

        tm = mb.addMenu("&Tools")
        self._a_slice = self._action(tm, "Grid Slice...", "", self._on_slice)
        self._a_pack = self._action(tm, "Pack Sprite Sheet", "", self._on_pack)
        tm.addSeparator()
        self._a_resize = self._action(tm, "Resize Selected Frames...", "", self._on_resize_frames)

        xm = mb.addMenu("&Export")
        self._a_export = self._action(xm, "Export...", "Ctrl+E", self._on_export)

        hm = mb.addMenu("&Help")
        self._a_about = self._action(hm, "About", "", self._on_about)

    def _action(self, menu, text, shortcut, slot):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    # ── Toolbar ──

    def _build_toolbar(self):
        tb = QToolBar("Main"); tb.setMovable(False)
        tb.addAction(self._a_open_frames)
        tb.addAction(self._a_load_sheet)
        tb.addAction(self._a_import_atlas)
        tb.addSeparator()
        tb.addAction(self._a_undo)
        tb.addAction(self._a_redo)
        tb.addSeparator()
        tb.addAction(self._a_slice)
        tb.addAction(self._a_pack)
        tb.addSeparator()
        tb.addAction(self._a_trim)
        tb.addAction(self._a_export)
        self.addToolBar(tb)

    # ── Central Widget ──

    def _build_central(self):
        splitter_v = QSplitter(Qt.Orientation.Vertical)
        self.setCentralWidget(splitter_v)

        self._tabs = QTabWidget()

        sheet_w = QWidget()
        sheet_l = QVBoxLayout(sheet_w); sheet_l.setContentsMargins(0, 0, 0, 0)
        self._canvas = SpriteCanvas()
        self._canvas.zoom_changed.connect(self._zoom_bar.set_zoom)
        self._canvas.region_clicked.connect(self._on_region_clicked)
        sheet_l.addWidget(self._canvas)
        self._zoom_bar = ZoomControlBar()
        self._zoom_bar.zoom_requested.connect(self._on_zoom_requested)
        sheet_l.addWidget(self._zoom_bar)

        grid_row = QHBoxLayout()
        self._grid_chk = QCheckBox("Show Grid")
        self._grid_chk.stateChanged.connect(lambda s: self._canvas.set_show_grid(s == Qt.CheckState.Checked.value))
        self._onion_chk = QCheckBox("Onion Skin")
        self._onion_chk.stateChanged.connect(lambda s: self._preview.set_onion_skin(s == Qt.CheckState.Checked.value))
        self._minimap_chk = QCheckBox("Minimap")
        self._minimap_chk.setChecked(True)
        self._minimap_chk.stateChanged.connect(lambda s: setattr(self._canvas, '_minimap_enabled', s == Qt.CheckState.Checked.value))
        grid_row.addWidget(self._grid_chk)
        grid_row.addWidget(self._onion_chk)
        grid_row.addWidget(self._minimap_chk)
        grid_row.addStretch()
        sheet_l.addLayout(grid_row)

        self._tabs.addTab(sheet_w, "  Sprite Sheet  ")

        prev_w = QWidget()
        prev_l = QVBoxLayout(prev_w); prev_l.setContentsMargins(0, 0, 0, 0)
        self._preview = AnimationPreview()
        prev_l.addWidget(self._preview)

        self._timeline = TimelineWidget()
        self._timeline.frame_selected.connect(self._on_timeline_frame_selected)
        prev_l.addWidget(self._timeline)

        ctrl_row = QHBoxLayout()
        for text, slot in [("Play", self._on_play), ("Pause", self._preview.pause),
                           ("Stop", self._on_stop)]:
            b = QPushButton(text); b.clicked.connect(slot); ctrl_row.addWidget(b)
        self._fps_label = QLabel("FPS: --")
        self._fps_label.setStyleSheet("color: #89b4fa; font-weight: bold;")
        ctrl_row.addWidget(self._fps_label)
        ctrl_row.addStretch()
        prev_l.addLayout(ctrl_row)

        self._tabs.addTab(prev_w, "  Animation Preview  ")
        splitter_v.addWidget(self._tabs)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setStyleSheet("background: #181825; color: #a6adc8; font-family: Monospace; font-size: 11px; border: 1px solid #45475a; border-radius: 6px;")
        splitter_v.addWidget(self._log)
        splitter_v.setSizes([700, 120])

    # ── Dock Panels ──

    def _build_docks(self):
        frames_dock = QDockWidget("Frames", self)
        frames_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        frames_w = QWidget()
        frames_l = QVBoxLayout(frames_w); frames_l.setContentsMargins(4, 4, 4, 4)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search frames...")
        self._search_edit.textChanged.connect(self._on_search)
        frames_l.addWidget(self._search_edit)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tag:"))
        self._tag_filter = QComboBox()
        self._tag_filter.addItem("All")
        self._tag_filter.setMinimumWidth(80)
        self._tag_filter.currentTextChanged.connect(self._on_search)
        tag_row.addWidget(self._tag_filter)
        frames_l.addLayout(tag_row)

        view_row = QHBoxLayout()
        self._view_list_btn = QPushButton("List")
        self._view_list_btn.setCheckable(True)
        self._view_list_btn.setChecked(True)
        self._view_list_btn.clicked.connect(lambda: self._set_view_mode("list"))
        self._view_grid_btn = QPushButton("Grid")
        self._view_grid_btn.setCheckable(True)
        self._view_grid_btn.clicked.connect(lambda: self._set_view_mode("grid"))
        view_row.addWidget(self._view_list_btn)
        view_row.addWidget(self._view_grid_btn)
        view_row.addStretch()
        frames_l.addLayout(view_row)

        self._frame_list = QListWidget()
        self._frame_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._frame_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._frame_list.setIconSize(QSize(48, 48))
        self._frame_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._frame_list.customContextMenuRequested.connect(self._frame_context_menu)
        frames_l.addWidget(self._frame_list)

        self._thumb_grid = ThumbnailGridWidget()
        self._thumb_grid.frame_double_clicked.connect(self._on_frame_double_click)
        self._thumb_grid.selection_changed.connect(self._on_grid_selection_changed)
        self._thumb_grid.hide()

        scroll = QScrollArea()
        scroll.setWidget(self._thumb_grid)
        scroll.setWidgetResizable(True)
        scroll.hide()
        frames_l.addWidget(scroll)

        btn_row = QHBoxLayout()
        add_b = QPushButton("+ Add"); add_b.clicked.connect(self._on_add_frames)
        rem_b = QPushButton("- Remove"); rem_b.clicked.connect(self._on_remove_frame)
        btn_row.addWidget(add_b); btn_row.addWidget(rem_b)
        frames_l.addLayout(btn_row)

        frames_dock.setWidget(frames_w)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, frames_dock)

        props_dock = QDockWidget("Properties", self)
        props_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        props_w = QWidget()
        props_l = QVBoxLayout(props_w); props_l.setContentsMargins(4, 4, 4, 4)

        info_grp = QGroupBox("Selected Frame")
        info_form = QFormLayout()
        self._info_name = QLabel("-"); self._info_name.setWordWrap(True)
        self._info_size = QLabel("-")
        self._info_dims = QLabel("-")
        self._info_path = QLabel("-"); self._info_path.setWordWrap(True)
        self._info_tag = QLabel("-")
        self._info_pivot = QLabel("-")
        info_form.addRow("Name:", self._info_name)
        info_form.addRow("Dimensions:", self._info_size)
        self._info_preview = QLabel()
        self._info_preview.setFixedSize(96, 96)
        self._info_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_preview.setStyleSheet("background: #181825; border: 1px solid #45475a; border-radius: 6px;")
        info_form.addRow("Preview:", self._info_preview)
        info_form.addRow("Tag:", self._info_tag)
        info_form.addRow("Pivot:", self._info_pivot)
        info_form.addRow("Source:", self._info_path)
        info_grp.setLayout(info_form)
        props_l.addWidget(info_grp)

        props_dock.setWidget(props_w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, props_dock)

        anim_dock = QDockWidget("Animations", self)
        anim_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        anim_w = QWidget()
        anim_l = QVBoxLayout(anim_w); anim_l.setContentsMargins(4, 4, 4, 4)

        self._anim_list = QListWidget()
        self._anim_list.setMaximumHeight(140)
        self._anim_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._anim_list.customContextMenuRequested.connect(self._anim_context_menu)
        anim_l.addWidget(self._anim_list)

        ab = QHBoxLayout()
        for text, slot in [("New", self._on_new_anim), ("Edit", self._on_edit_anim),
                           ("Delete", self._on_del_anim)]:
            btn = QPushButton(text); btn.clicked.connect(slot); ab.addWidget(btn)
        anim_l.addLayout(ab)

        pack_grp = QGroupBox("Pack Settings")
        pack_f = QFormLayout()
        self._pad_spin = QSpinBox(); self._pad_spin.setRange(0, 16); self._pad_spin.setValue(1)
        pack_f.addRow("Padding:", self._pad_spin)
        self._pow2_chk = QCheckBox()
        pack_f.addRow("Power of Two:", self._pow2_chk)
        self._maxw_spin = QSpinBox(); self._maxw_spin.setRange(64, 8192); self._maxw_spin.setValue(2048); self._maxw_spin.setSingleStep(256)
        pack_f.addRow("Max Width:", self._maxw_spin)
        self._ppu_spin = QSpinBox(); self._ppu_spin.setRange(1, 10000); self._ppu_spin.setValue(100)
        self._ppu_spin.setToolTip("Pixels per unit for Unity import")
        pack_f.addRow("Pixels/Unit:", self._ppu_spin)
        pack_grp.setLayout(pack_f)
        anim_l.addWidget(pack_grp)

        anim_dock.setWidget(anim_w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, anim_dock)

        self.tabifyDockWidget(props_dock, anim_dock)
        props_dock.raise_()

    # ── Status Bar ──

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status_label = QLabel("Ready")
        self._frame_count_label = QLabel("0 frames")
        self._mem_label = QLabel("")
        self._progress = QProgressBar()
        self._progress.setFixedWidth(160)
        self._progress.hide()
        sb.addWidget(self._status_label, 1)
        sb.addPermanentWidget(self._frame_count_label)
        sb.addPermanentWidget(self._mem_label)
        sb.addPermanentWidget(self._progress)

        self._mem_timer = QTimer(self)
        self._mem_timer.timeout.connect(self._update_memory)
        self._mem_timer.start(3000)
        self._update_memory()

    # ── Signals ──

    def _connect(self):
        self._model.frames_changed.connect(self._refresh_frames)
        self._model.animations_changed.connect(self._refresh_anims)
        self._frame_list.currentRowChanged.connect(self._on_frame_selected)
        self._frame_list.model().rowsMoved.connect(self._on_frames_reordered)
        self._anim_list.currentRowChanged.connect(self._on_anim_selected)
        self._model.undo_stack.cleanChanged.connect(self._update_title)

    # ════════════════════════════════════════════════════════════════════════
    #  FRAME MANAGEMENT
    # ════════════════════════════════════════════════════════════════════════

    def _on_add_frames(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Sprite Frames", "",
                    "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tga *.webp);;All (*)")
        if not paths: return
        new_frames = []
        for p in paths:
            f = self._load_frame(p)
            if f: new_frames.append(f)
        if new_frames:
            self._model.push(AddFramesCommand(self._model, new_frames))
            self._toast.show_toast(f"Added {len(new_frames)} frames")
            self._log_append(f"Added {len(new_frames)} frames")

    def _on_load_sheet(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load Sprite Sheet", "",
                    "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tga *.webp);;All (*)")
        if not p: return
        self._clear_frames_internal()
        f = self._load_frame(p)
        if f:
            self._model.push(AddFramesCommand(self._model, [f]))
            self._show_frame_on_canvas(0)
            self._toast.show_toast(f"Loaded sheet: {Path(p).name}")
            self._log_append(f"Loaded sprite sheet: {p}")

    def _load_frame(self, path):
        try:
            img = Image.open(path).convert("RGBA")
            name = Path(path).stem
            return SpriteFrame(name=name, image=img,
                               rect=(0, 0, img.width, img.height), filepath=path)
        except Exception as e:
            self._log_append(f"ERROR loading {path}: {e}")
            QMessageBox.warning(self, "Load Error", f"Failed to load {path}:\n{e}")
            return None

    def _on_remove_frame(self):
        rows = sorted(set(i.row() for i in self._frame_list.selectedItems()), reverse=True)
        if not rows: return
        self._model.push(RemoveFramesCommand(self._model, rows))
        self._toast.show_toast(f"Removed {len(rows)} frames", "#f38ba8")
        self._log_append(f"Removed {len(rows)} frames")

    def _on_duplicate_frame(self):
        rows = [i.row() for i in self._frame_list.selectedItems()]
        if not rows: return
        new = []
        for r in sorted(rows):
            if 0 <= r < len(self._model._frames):
                f = self._model._frames[r].copy()
                f.name += "_copy"
                new.append(f)
        if new:
            self._model.push(AddFramesCommand(self._model, new))
            self._toast.show_toast(f"Duplicated {len(new)} frames")

    def _on_rename_frame(self):
        row = self._frame_list.currentRow()
        if row < 0: return
        f = self._model._frames[row]
        new_name, ok = QInputDialog.getText(self, "Rename Frame", "New name:", text=f.name)
        if ok and new_name:
            new_f = f.copy()
            new_f.name = new_name
            self._model.push(ModifyFrameCommand(self._model, row, new_f, "Rename Frame"))
            self._frame_list.setCurrentRow(row)

    def _on_select_all(self):
        self._frame_list.selectAll()

    def _on_trim_frames(self):
        indices = sorted(set(i.row() for i in self._frame_list.selectedItems()))
        updates = []
        for idx in indices:
            new_f = self._model.auto_trim_frame(idx)
            if new_f:
                updates.append((idx, new_f))
        if updates:
            self._model.push(BatchModifyFramesCommand(self._model, updates, "Trim Frames"))
            self._toast.show_toast(f"Trimmed {len(updates)} frames", "#f9e2af")
            self._log_append(f"Auto-trimmed {len(updates)} frames")

    def _on_trim_all_frames(self):
        updates = []
        for i in range(len(self._model._frames)):
            new_f = self._model.auto_trim_frame(i)
            if new_f:
                updates.append((i, new_f))
        if updates:
            self._model.push(BatchModifyFramesCommand(self._model, updates, "Trim All Frames"))
            self._toast.show_toast(f"Trimmed {len(updates)} frames", "#f9e2af")

    def _on_frame_properties(self):
        row = self._frame_list.currentRow()
        if row < 0: return
        dlg = FramePropertiesDialog(self._model._frames[row], self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.get_values()
            f = self._model._frames[row].copy()
            f.name, f.tag = v["name"], v["tag"]
            f.pivot_x, f.pivot_y = v["pivot_x"], v["pivot_y"]
            f.border = v["border"]
            self._model.push(ModifyFrameCommand(self._model, row, f, "Modify Properties"))
            self._frame_list.setCurrentRow(row)

    def _on_resize_frames(self):
        indices = sorted(set(i.row() for i in self._frame_list.selectedItems()))
        if not indices: return
        dlg = QDialog(self)
        dlg.setWindowTitle("Resize Frames")
        fl = QFormLayout(dlg)
        w_spin = QSpinBox(); w_spin.setRange(1, 8192); w_spin.setValue(self._model._frames[indices[0]].image.width)
        h_spin = QSpinBox(); h_spin.setRange(1, 8192); h_spin.setValue(self._model._frames[indices[0]].image.height)
        method = QComboBox(); method.addItems(["Nearest", "Bilinear", "Lanczos"])
        fl.addRow("Width:", w_spin); fl.addRow("Height:", h_spin); fl.addRow("Method:", method)
        bb = QHBoxLayout()
        ok = QPushButton("Resize"); ok.setProperty("primary", True); ok.clicked.connect(dlg.accept)
        cc = QPushButton("Cancel"); cc.clicked.connect(dlg.reject)
        bb.addStretch(); bb.addWidget(cc); bb.addWidget(ok); fl.addRow(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            resample = [Image.NEAREST, Image.BILINEAR, Image.LANCZOS][method.currentIndex()]
            nw, nh = w_spin.value(), h_spin.value()
            updates = []
            for idx in indices:
                f = self._model._frames[idx].copy()
                f.image = f.image.resize((nw, nh), resample)
                f.rect = (0, 0, nw, nh)
                updates.append((idx, f))
            self._model.push(BatchModifyFramesCommand(self._model, updates, "Resize Frames"))
            self._toast.show_toast(f"Resized {len(updates)} frames to {nw}x{nh}")

    def _clear_frames_internal(self):
        self._model.undo_stack.clear()
        self._model._frames.clear()
        self._model._animations.clear()
        self._model._atlas_image = None
        self._model._atlas_regions = []
        self._frame_list.clear()
        self._canvas.set_pixmap(QPixmap())
        self._canvas.set_grid([])

    def _on_clear_all(self):
        if QMessageBox.question(self, "Clear All", "Remove all frames and animations?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._model.push(ClearAllCommand(self._model))
            self._preview.stop()
            self._preview.set_animation([])
            self._timeline.set_frames([])
            self._toast.show_toast("Cleared all", "#f38ba8")

    # ── Frame list / grid refresh ──

    def _refresh_frames(self):
        prev_selected = {it.data(Qt.ItemDataRole.UserRole) for it in self._frame_list.selectedItems()}
        self._frame_list.blockSignals(True)
        cur = self._frame_list.currentRow()
        self._frame_list.clear()
        search = self._search_edit.text().lower()
        tag_filter = self._tag_filter.currentText()
        for i, f in enumerate(self._model._frames):
            if search and search not in f.name.lower() and search not in f.tag.lower():
                continue
            if tag_filter != "All" and f.tag != tag_filter:
                continue
            item = QListWidgetItem(f"{i}: {f.name}  ({f.image.width}x{f.image.height})")
            pm = _pil_to_qpixmap(f.image)
            thumb = pm.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            item.setIcon(QIcon(thumb))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._frame_list.addItem(item)
            if i in prev_selected:
                item.setSelected(True)
        if 0 <= cur < self._frame_list.count():
            self._frame_list.setCurrentRow(cur)
        self._frame_list.blockSignals(False)

        self._thumb_grid.set_frames(self._model._frames)
        self._update_tags()
        self._frame_count_label.setText(f"{len(self._model._frames)} frames")
        self._update_empty_state()

    def _update_tags(self):
        current = self._tag_filter.currentText()
        self._tag_filter.blockSignals(True)
        self._tag_filter.clear()
        self._tag_filter.addItem("All")
        tags = set()
        for f in self._model._frames:
            if f.tag:
                tags.add(f.tag)
        for t in sorted(tags):
            self._tag_filter.addItem(t)
        idx = self._tag_filter.findText(current)
        if idx >= 0: self._tag_filter.setCurrentIndex(idx)
        self._tag_filter.blockSignals(False)

    def _on_search(self):
        self._refresh_frames()

    def _set_view_mode(self, mode):
        self._view_list_btn.setChecked(mode == "list")
        self._view_grid_btn.setChecked(mode == "grid")
        if mode == "list":
            self._frame_list.show()
            self._thumb_grid.parent().parent().hide()  # scroll area
        else:
            self._frame_list.hide()
            self._thumb_grid.parent().parent().show()

    def _on_frame_selected(self, row):
        if row < 0:
            self._info_preview.setPixmap(QPixmap())
            return
        data = self._frame_list.item(row).data(Qt.ItemDataRole.UserRole)
        if data is None: return
        if 0 <= data < len(self._model._frames):
            f = self._model._frames[data]
            self._info_name.setText(f.name)
            self._info_size.setText(f"{f.image.width} x {f.image.height} px")
            self._info_tag.setText(f.tag or "-")
            self._info_pivot.setText(f"({f.pivot_x:.2f}, {f.pivot_y:.2f})")
            self._info_path.setText(f.filepath or "N/A")
            pm = _pil_to_qpixmap(f.image)
            self._info_preview.setPixmap(pm.scaled(92, 92, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
            self._show_frame_on_canvas(data)

    def _show_frame_on_canvas(self, idx):
        if 0 <= idx < len(self._model._frames):
            pm = _pil_to_qpixmap(self._model._frames[idx].image)
            self._canvas.set_pixmap(pm)
            self._canvas.set_grid([])
            self._tabs.setCurrentIndex(0)

    def _on_frame_double_click(self, idx):
        self._show_frame_on_canvas(idx)
        self._frame_list.setCurrentRow(idx)

    def _on_grid_selection_changed(self, indices):
        self._frame_list.blockSignals(True)
        self._frame_list.clearSelection()
        for i in indices:
            if i < self._frame_list.count():
                self._frame_list.item(i).setSelected(True)
        self._frame_list.blockSignals(False)

    def _on_frames_reordered(self):
        new_order = []
        for i in range(self._frame_list.count()):
            old_idx = self._frame_list.item(i).data(Qt.ItemDataRole.UserRole)
            new_order.append(old_idx)
        if len(new_order) == len(self._model._frames):
            self._model.push(ReorderFramesCommand(self._model, 
                            list(range(len(new_order))), new_order))

    def _on_region_clicked(self, name):
        for i, f in enumerate(self._model._frames):
            if f.name == name:
                self._frame_list.setCurrentRow(i)
                break

    def _frame_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction("Rename (F2)", self._on_rename_frame)
        menu.addAction("Properties... (Ctrl+P)", self._on_frame_properties)
        menu.addSeparator()
        menu.addAction("Duplicate (Ctrl+D)", self._on_duplicate_frame)
        menu.addAction("Auto-Trim (Ctrl+T)", self._on_trim_frames)
        menu.addAction("Resize...", self._on_resize_frames)
        menu.addSeparator()
        menu.addAction("Remove (Del)", self._on_remove_frame)
        menu.exec(self._frame_list.mapToGlobal(pos))

    # ════════════════════════════════════════════════════════════════════════
    #  GRID SLICING
    # ════════════════════════════════════════════════════════════════════════

    def _on_slice(self):
        if not self._model._frames:
            self._toast.show_toast("Load a sprite sheet first", "#f38ba8"); return
        if len(self._model._frames) > 1:
            self._toast.show_toast("Slicing works on a single image", "#f38ba8"); return
        f = self._model._frames[0]
        dlg = GridSliceDialog(f.image.width, f.image.height, self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        p = dlg.get_params()
        src = f.image
        self._clear_frames_internal()
        grid_rects = []
        new_frames = []
        count = 0
        for r in range(p["rows"]):
            for c in range(p["cols"]):
                x = c * (p["cell_w"] + p["padding"])
                y = r * (p["cell_h"] + p["padding"])
                if x + p["cell_w"] <= src.width and y + p["cell_h"] <= src.height:
                    cropped = src.crop((x, y, x + p["cell_w"], y + p["cell_h"]))
                    new_frames.append(SpriteFrame(
                        name=f"{p['prefix']}_{count:04d}", image=cropped,
                        rect=(x, y, p["cell_w"], p["cell_h"]), filepath=f.filepath))
                    grid_rects.append(QRect(x, y, p["cell_w"], p["cell_h"]))
                    count += 1
        self._model.push(AddFramesCommand(self._model, new_frames, "Grid Slice"))
        pm = _pil_to_qpixmap(f.image)
        self._canvas.set_pixmap(pm)
        self._canvas.set_grid(grid_rects)
        self._grid_chk.setChecked(True)
        self._tabs.setCurrentIndex(0)
        self._toast.show_toast(f"Sliced into {count} frames", "#a6e3a1")
        self._log_append(f"Sliced into {count} frames ({p['cols']}x{p['rows']})")

    # ════════════════════════════════════════════════════════════════════════
    #  PACKING
    # ════════════════════════════════════════════════════════════════════════

    def _on_pack(self):
        if not self._model._frames:
            self._toast.show_toast("Add frames first", "#f38ba8"); return
        self._progress.show()
        self._progress.setValue(0)
        self._status_label.setText("Packing...")
        QApplication.processEvents()

        self._model._atlas_image, self._model._atlas_regions = pack_sprites(
            self._model._frames,
            padding=self._pad_spin.value(),
            power_of_two=self._pow2_chk.isChecked(),
            max_width=self._maxw_spin.value())

        pm = _pil_to_qpixmap(self._model._atlas_image)
        self._canvas.set_pixmap(pm)
        grid_rects = [QRect(r["x"], r["y"], r["w"], r["h"]) for r in self._model._atlas_regions]
        self._canvas.set_grid(grid_rects)
        self._canvas.set_region_names([r["name"] for r in self._model._atlas_regions])
        self._grid_chk.setChecked(True)
        self._tabs.setCurrentIndex(0)
        self._canvas.fit_to_view()

        self._progress.hide()
        self._toast.show_toast(f"Packed {len(self._model._frames)} frames", "#a6e3a1")
        self._log_append(f"Packed to {self._model._atlas_image.width}x{self._model._atlas_image.height}")
        self._set_status(f"Packed {len(self._model._frames)} frames -> {self._model._atlas_image.width}x{self._model._atlas_image.height}")

    # ════════════════════════════════════════════════════════════════════════
    #  ANIMATIONS
    # ════════════════════════════════════════════════════════════════════════

    def _refresh_anims(self):
        self._anim_list.clear()
        for a in self._model._animations:
            self._anim_list.addItem(f"{a.name}  ({len(a.frames)}f @ {a.fps:.1f}fps {'[loop]' if a.loop else ''}")

    def _on_new_anim(self):
        if not self._model._frames:
            self._toast.show_toast("Add frames first", "#f38ba8"); return
        names = [f.name for f in self._model._frames]
        dlg = AnimationEditDialog(available_frames=names, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            a = dlg.get_animation()
            self._model._animations.append(a)
            self._refresh_anims()
            self._anim_list.setCurrentRow(len(self._model._animations) - 1)
            self._toast.show_toast(f"Created animation: {a.name}")

    def _on_edit_anim(self):
        row = self._anim_list.currentRow()
        if row < 0: return
        a = self._model._animations[row]
        names = [f.name for f in self._model._frames]
        dlg = AnimationEditDialog(anim=a, available_frames=names, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_a = dlg.get_animation()
            self._model._animations[row] = new_a
            self._refresh_anims()
            self._anim_list.setCurrentRow(row)
            self._on_anim_selected(row)

    def _on_del_anim(self):
        row = self._anim_list.currentRow()
        if row < 0: return
        name = self._model._animations[row].name
        self._model._animations.pop(row)
        self._anim_list.takeItem(row)
        self._preview.stop()
        self._preview.set_animation([])
        self._timeline.set_frames([])
        self._toast.show_toast(f"Deleted animation: {name}", "#f38ba8")

    def _on_anim_selected(self, row):
        if row < 0 or row >= len(self._model._animations): return
        a = self._model._animations[row]
        pixmaps = []
        for idx in a.frames:
            if 0 <= idx < len(self._model._frames):
                pixmaps.append(_pil_to_qpixmap(self._model._frames[idx].image))
        durations = a.effective_durations()
        self._preview.set_animation(pixmaps, durations, a.fps, a.loop)
        self._timeline.set_frames(pixmaps, durations)
        self._fps_label.setText(f"FPS: {a.fps:.1f}")
        self._preview.play()
        self._tabs.setCurrentIndex(1)

    def _on_play(self):
        self._preview.play()

    def _on_stop(self):
        self._preview.stop()
        self._timeline.set_current(0)

    def _on_timeline_frame_selected(self, idx):
        self._preview.scrub_to(idx)
        self._timeline.set_current(idx)

    def _anim_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction("Edit...", self._on_edit_anim)
        menu.addAction("Duplicate", self._on_duplicate_anim)
        menu.addSeparator()
        menu.addAction("Delete", self._on_del_anim)
        menu.exec(self._anim_list.mapToGlobal(pos))

    def _on_duplicate_anim(self):
        row = self._anim_list.currentRow()
        if row < 0: return
        a = self._model._animations[row]
        dup = Animation(a.name + "_copy", a.fps, a.loop)
        dup.frames = list(a.frames)
        dup.per_frame_duration = list(a.per_frame_duration)
        self._model._animations.insert(row + 1, dup)
        self._refresh_anims()
        self._toast.show_toast(f"Duplicated animation: {a.name}")

    # ════════════════════════════════════════════════════════════════════════
    #  IMPORT ATLAS
    # ════════════════════════════════════════════════════════════════════════

    def _on_import_atlas(self):
        dlg = ImportAtlasDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        json_path, png_path, fmt = dlg.get_paths()
        if not json_path or not png_path:
            self._toast.show_toast("Select both JSON and PNG files", "#f38ba8"); return

        try:
            with open(json_path) as f:
                data = json.load(f)

            atlas_img = Image.open(png_path).convert("RGBA")
            new_frames = []

            if fmt in (0, 1):  # TexturePacker or Generic
                frames_data = data.get("frames", {})
                if isinstance(frames_data, dict):
                    frames_data = list(frames_data.values())
                for fd in frames_data:
                    fr = fd.get("frame", fd)
                    x, y, w, h = fr.get("x", 0), fr.get("y", 0), fr.get("w", 0), fr.get("h", 0)
                    if w > 0 and h > 0:
                        cropped = atlas_img.crop((x, y, x + w, y + h))
                        name = fd.get("name", f"frame_{len(new_frames)}")
                        new_frames.append(SpriteFrame(
                            name=name, image=cropped, rect=(x, y, w, h), filepath=png_path))

            self._model.push(AddFramesCommand(self._model, new_frames, "Import Atlas"))
            self._toast.show_toast(f"Imported {len(new_frames)} frames from atlas", "#a6e3a1")
            self._log_append(f"Imported from {json_path}")
        except Exception as e:
            self._log_append(f"Import Error: {traceback.format_exc()}")
            QMessageBox.critical(self, "Import Error", str(e))

    # ════════════════════════════════════════════════════════════════════════
    #  PROJECT SAVE / LOAD
    # ════════════════════════════════════════════════════════════════════════

    def _on_save_project(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save Project", self._model._project_path or "project.sacproj",
                                               "SAC Project (*.sacproj);;All (*)")
        if not path: return False
        try:
            proj = Project()
            proj.frames = self._model._frames
            proj.animations = self._model._animations
            proj.pack_padding = self._pad_spin.value()
            proj.pack_pow2 = self._pow2_chk.isChecked()
            proj.pack_max_width = self._maxw_spin.value()
            proj.pixels_per_unit = self._ppu_spin.value()
            Project.save(path, proj)
            self._model._project_path = path
            self._model.undo_stack.setClean()
            self._add_recent(path)
            self._update_title()
            self._toast.show_toast("Project saved", "#a6e3a1")
            self._log_append(f"Saved project: {path}")
            return True
        except Exception as e:
            self._log_append(f"Save Error: {traceback.format_exc()}")
            QMessageBox.critical(self, "Save Error", str(e))
            return False

    def _on_open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", "",
                                               "SAC Project (*.sacproj);;All (*)")
        if not path: return
        self._load_project(path)

    def _load_project(self, path):
        try:
            proj, frame_data, anim_data = Project.load(path)
            self._clear_frames_internal()

            for fd in frame_data:
                fp = fd.get("filepath", "")
                if fp and os.path.exists(fp):
                    img = Image.open(fp).convert("RGBA")
                else:
                    img = Image.new("RGBA", (32, 32), (128, 128, 128, 255))
                    self._log_append(f"WARNING: Missing file for frame '{fd.get('name')}'. Substituted gray placeholder.")
                f = SpriteFrame(name=fd.get("name", "frame"), image=img,
                                rect=tuple(fd.get("rect", (0, 0, img.width, img.height))),
                                filepath=fp, pivot_x=fd.get("pivot_x", 0.5),
                                pivot_y=fd.get("pivot_y", 0.5),
                                border=tuple(fd.get("border", (0, 0, 0, 0))),
                                tag=fd.get("tag", ""))
                self._model._frames.append(f)

            for ad in anim_data:
                a = Animation(name=ad.get("name", "idle"), fps=ad.get("fps", 12),
                              loop=ad.get("loop", True))
                a.frames = ad.get("frames", [])
                a.per_frame_duration = ad.get("per_frame_duration", [])
                self._model._animations.append(a)

            self._pad_spin.setValue(proj.pack_padding)
            self._pow2_chk.setChecked(proj.pack_pow2)
            self._maxw_spin.setValue(proj.pack_max_width)
            self._ppu_spin.setValue(proj.pixels_per_unit)

            self._refresh_frames()
            self._refresh_anims()
            self._model._project_path = path
            self._model.undo_stack.setClean()
            self._add_recent(path)
            self._update_title()
            self._toast.show_toast(f"Loaded project: {Path(path).name}", "#a6e3a1")
            self._log_append(f"Opened project: {path}")
        except Exception as e:
            self._log_append(f"Load Error: {traceback.format_exc()}")
            QMessageBox.critical(self, "Load Error", str(e))

    # ── Recent files ──

    def _recent_file_path(self):
        return os.path.expanduser("~/.sprite_asset_creator_recent.json")

    def _load_recent(self):
        try:
            with open(self._recent_file_path()) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_recent(self, files):
        with open(self._recent_file_path(), "w") as f:
            json.dump(files[:10], f)

    def _add_recent(self, path):
        r = self._load_recent()
        if path in r: r.remove(path)
        r.insert(0, path)
        self._save_recent(r)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        recent = self._load_recent()
        if not recent:
            none_action = self._recent_menu.addAction("(none)")
            none_action.setEnabled(False)
            return
        for p in recent[:8]:
            a = self._recent_menu.addAction(Path(p).name)
            a.setToolTip(p)
            a.triggered.connect(lambda checked, pp=p: self._load_project(pp))
        self._recent_menu.addSeparator()
        clear = self._recent_menu.addAction("Clear Recent")
        clear.triggered.connect(lambda: (self._save_recent([]), self._rebuild_recent_menu()))

    # ════════════════════════════════════════════════════════════════════════
    #  EXPORT
    # ════════════════════════════════════════════════════════════════════════

    def _on_export(self):
        if not self._model._frames:
            self._toast.show_toast("No frames to export", "#f38ba8"); return

        if self._model._atlas_image is None:
            self._on_pack()
        if self._model._atlas_image is None: return

        dlg = ExportDialog(self, has_animations=bool(self._model._animations))
        dlg.pad_spin.setValue(self._pad_spin.value())
        dlg.pow2.setChecked(self._pow2_chk.isChecked())
        dlg.max_w.setValue(self._maxw_spin.value())
        dlg.ppu_spin.setValue(self._ppu_spin.value())

        if dlg.exec() != QDialog.DialogCode.Accepted: return
        s = dlg.get_settings()
        out_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory")
        if not out_dir: return

        self.setEnabled(False)  # Block UI interaction during export
        self._progress.show(); self._progress.setValue(0)
        self._set_status("Exporting..."); QApplication.processEvents()

        try:
            engine = s["engine"]
            name = s["output_name"]

            if engine == 4:  # Individual PNGs
                self._export_individual(out_dir, name)
            elif engine == 0:
                self._export_unity(out_dir, name, s)
            elif engine == 1:
                self._export_godot(out_dir, name, s)
            elif engine == 2:
                self._export_unreal(out_dir, name, s)
            elif engine == 3:
                self._export_generic(out_dir, name, s)

            self._progress.hide()
            self._toast.show_toast(f"Exported to {Path(out_dir).name}", "#a6e3a1")
            self._log_append(f"Exported [{s['engine_name']}] to {out_dir}")
            self._set_status(f"Exported: {out_dir}")
        except Exception as e:
            self._progress.hide()
            self._log_append(f"Export ERROR: {traceback.format_exc()}")
            QMessageBox.critical(self, "Export Error", str(e))
        finally:
            self.setEnabled(True)

    def _export_individual(self, out_dir, name):
        sub = os.path.join(out_dir, name)
        os.makedirs(sub, exist_ok=True)
        for i, f in enumerate(self._model._frames):
            f.image.save(os.path.join(sub, f"{f.name}.png"))
            self._progress.setValue(int((i + 1) / len(self._model._frames) * 100))
            QApplication.processEvents()
        self._log_append(f"  -> {len(self._model._frames)} PNGs in {sub}")

    def _export_unity(self, out_dir, name, s):
        atlas_path = os.path.join(out_dir, f"{name}.png")
        self._model._atlas_image.save(atlas_path)
        regions = self._model._atlas_regions
        atlas = self._model._atlas_image
        ppu = s["pixels_per_unit"]

        guid = str(uuid.uuid4()).replace("-", "")[:32]
        meta = {
            "fileFormatVersion": 2, "guid": guid,
            "TextureImporter": {
                "serializedVersion": 13,
                "mipmaps": {"enableMipMap": 0, "sRGBTexture": 1},
                "textureType": 8, "spriteMode": 2,
                "spritePivot": {"x": 0.5, "y": 0.5},
                "spritePixelsToUnits": ppu,
                "textureWidth": atlas.width, "textureHeight": atlas.height,
                "textureSettings": {"filterMode": 1, "wrapU": 0, "wrapV": 0, "wrapW": 0},
                "sprites": []
            }, "type": "Sprite"
        }
        for i, r in enumerate(regions):
            fi = self._model._frames[i] if i < len(self._model._frames) else None
            meta["TextureImporter"]["sprites"].append({
                "name": r["name"],
                "rect": {"x": float(r["x"]), "y": float(atlas.height - r["y"] - r["h"]),
                         "width": float(r["w"]), "height": float(r["h"])},
                "alignment": 0,
                "pivot": {"x": fi.pivot_x if fi else 0.5, "y": fi.pivot_y if fi else 0.5},
                "border": {"x": fi.border[0] if fi else 0, "y": fi.border[1] if fi else 0,
                           "z": fi.border[2] if fi else 0, "w": fi.border[3] if fi else 0},
            })
        with open(os.path.join(out_dir, f"{name}.png.meta"), "w") as f:
            json.dump(meta, f, indent=2)

        if self._model._animations:
            anim_data = [{"name": a.name, "fps": a.fps, "loop": a.loop,
                          "frames": [regions[i]["name"] for i in a.frames if i < len(regions)]}
                         for a in self._model._animations]
            with open(os.path.join(out_dir, f"{name}_animations.json"), "w") as f:
                json.dump(anim_data, f, indent=2)

    def _export_godot(self, out_dir, name, s):
        self._model._atlas_image.save(os.path.join(out_dir, f"{name}.png"))
        regions = self._model._atlas_regions
        anims = list(self._model._animations)
        if not anims:
            anims = [Animation("default", 8, True, list(range(len(regions))))]

        lines = ['[gd_resource type="SpriteFrames" format=3 uid="uid://"]', '', '[resource]', 'animations = [']
        for ai, a in enumerate(anims):
            lines.append('{')
            lines.append(f'"name": "{a.name}",')
            lines.append(f'"speed": {a.fps / 8.0:.1f},')
            lines.append('"loop": true,' if a.loop else '"loop": false,')
            lines.append('"frames": [')
            entries = []
            for fi, idx in enumerate(a.frames):
                if idx < len(regions):
                    r = regions[idx]
                    dur = a.effective_durations()[fi] / 1000.0 if fi < len(a.effective_durations()) else 0.125
                    entries.append(f'{{"duration": {dur:.4f}, "texture": ExtResource("1"), "region": Rect2({r["x"]}, {r["y"]}, {r["w"]}, {r["h"]})}}')
            for j, e in enumerate(entries):
                lines.append(e + ("," if j < len(entries) - 1 else ""))
            lines.append(']')
            lines.append('}' + ("," if ai < len(anims) - 1 else ""))
        lines.append(']')
        with open(os.path.join(out_dir, f"{name}.tres"), "w") as f:
            f.write("\n".join(lines))

    def _export_unreal(self, out_dir, name, s):
        self._model._atlas_image.save(os.path.join(out_dir, f"{name}.png"))
        regions = self._model._atlas_regions
        atlas = self._model._atlas_image

        data = {"TextureAtlas": {"name": name, "imagePath": f"{name}.png",
                "format": "RGBA8888",
                "size": {"w": atlas.width, "h": atlas.height}, "scale": 1.0, "frames": []}}
        for r in regions:
            data["TextureAtlas"]["frames"].append({
                "name": r["name"], "frame": {"x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]},
                "rotated": r.get("rotated", False), "trimmed": False,
                "spriteSourceSize": {"x": 0, "y": 0, "w": r["w"], "h": r["h"]},
                "sourceSize": {"w": r["w"], "h": r["h"]}})
        if self._model._animations:
            data["Animations"] = [{"name": a.name, "fps": a.fps, "loop": a.loop,
                "frameNames": [regions[i]["name"] for i in a.frames if i < len(regions)]}
                for a in self._model._animations]

        with open(os.path.join(out_dir, f"{name}_atlas.json"), "w") as f:
            json.dump(data, f, indent=2)
        with open(os.path.join(out_dir, f"{name}_sprites.csv"), "w") as f:
            f.write("Name,X,Y,Width,Height\n")
            for r in regions:
                f.write(f'{r["name"]},{r["x"]},{r["y"]},{r["w"]},{r["h"]}\n')

    def _export_generic(self, out_dir, name, s):
        self._model._atlas_image.save(os.path.join(out_dir, f"{name}.png"))
        regions = self._model._atlas_regions
        atlas = self._model._atlas_image
        data = {"name": name, "image": f"{name}.png", "format": "RGBA8888",
                "size": {"w": atlas.width, "h": atlas.height}, "scale": 1.0,
                "frames": {}, "animations": [a.to_dict() for a in self._model._animations]}
        for r in regions:
            data["frames"][r["name"]] = {
                "frame": {"x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]},
                "rotated": r.get("rotated", False), "trimmed": False,
                "spriteSourceSize": {"x": 0, "y": 0, "w": r["w"], "h": r["h"]},
                "sourceSize": {"w": r["w"], "h": r["h"]}}
        with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
            json.dump(data, f, indent=2)

    # ════════════════════════════════════════════════════════════════════════
    #  ZOOM
    # ════════════════════════════════════════════════════════════════════════

    def _on_zoom_requested(self, level):
        if level < 0:
            self._canvas.fit_to_view()
        else:
            self._canvas._zoom = level
            self._canvas.setTransform(QTransform().scale(level, level))
        self._zoom_bar.set_zoom(self._canvas._zoom)

    # ════════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ════════════════════════════════════════════════════════════════════════

    def _set_status(self, msg):
        self._status_label.setText(msg)

    def _log_append(self, text):
        ts = time.strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {text}")
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _update_empty_state(self):
        if not self._model._frames:
            self._set_status("Ready  |  Add frames to get started (Ctrl+O or drag & drop)")
        else:
            self._set_status(f"{len(self._model._frames)} frames  |  {len(self._model._animations)} animations")

    def _update_title(self):
        clean = self._model.undo_stack.isClean()
        path = self._model._project_path
        name = Path(path).stem if path else "Untitled"
        star = "" if clean else " *"
        self.setWindowTitle(f"Sprite Asset Creator v2  -  {name}{star}")

    def _update_memory(self):
        try:
            total = sum(f.image.width * f.image.height * 4 for f in self._model._frames)
            if total > 1048576:
                self._mem_label.setText(f"Mem: {total / 1048576:.1f} MB")
            else:
                self._mem_label.setText(f"Mem: {total / 1024:.0f} KB")
        except Exception:
            pass

    def _on_about(self):
        QMessageBox.about(self, "About Sprite Asset Creator v2",
            "<h2>Sprite Asset Creator v2</h2>"
            "<p>A professional cross-engine sprite asset creation tool.</p>"
            "<p><b>Engines:</b> Unity, Godot, Unreal Engine, Generic</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Frame import, grid slicing, atlas packing</li>"
            "<li>Animation editor with per-frame durations</li>"
            "<li>Auto-trim, resize, pivot & 9-slice editing</li>"
            "<li>Project save/load with undo/redo</li>"
            "<li>Import from TexturePacker / generic JSON</li>"
            "<li>Thumbnail grid & list views, search & tag filtering</li>"
            "<li>Minimap, zoom bar, onion skinning</li>"
            "</ul>"
            "<p>Built with PySide6 & Pillow</p>")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tga', '.webp')):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event):
        new = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and p.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tga', '.webp')):
                f = self._load_frame(p)
                if f: new.append(f)
        if new:
            self._model.push(AddFramesCommand(self._model, new))
            self._toast.show_toast(f"Dropped {len(new)} frames")

    def closeEvent(self, event):
        s = QSettings("SpriteAssetCreator", "v2")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("state", self.saveState())
        
        if not self._model.undo_stack.isClean():
            r = QMessageBox.question(self, "Unsaved Changes",
                "Save changes before closing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Save:
                if not self._on_save_project():
                    event.ignore()
                    return
            elif r == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        event.accept()

    def keyPressEvent(self, event):
        # Use QShortcut for global app shortcuts instead of overwriting keyPressEvent
        super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    
    # Register global shortcuts
    window = SpriteAssetCreator()
    
    QShortcut(QKeySequence("F"), window, window._canvas.fit_to_view)
    QShortcut(QKeySequence("Ctrl+F"), window, window._search_edit.setFocus)
    QShortcut(QKeySequence("Ctrl+0"), window, window._canvas.reset_zoom)
    QShortcut(QKeySequence("Space"), window, lambda: window._preview.play() if not window._preview._playing else window._preview.pause())
    
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()