"""
main_window.py 
VO-SE Cut Studio — メインウィンドウ（Apple風 Dark UI / 動画編集専用）

変更点 (vs 元の main_window.py):
  - _make_toolbar : ボタンの重複生成バグ修正 + self._transport_btns リスト化
  - _init_ui      : PlaybackEngine / TransportController を生成・接続
  - _make_sidebar : '+ 追加' ボタン、ダブルクリックロード
  - 新スロット    : _on_export_clicked / _on_add_asset / _on_asset_double_clicked
"""
from __future__ import annotations

import wave
import sys
import os
import ctypes
import platform
import traceback
from typing import Any, List, Dict, Optional, Tuple, TYPE_CHECKING

import video_engine

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTextEdit, QPushButton, QLabel, QListWidget, QListWidgetItem,
    QFrame, QGraphicsView, QGraphicsScene, QScrollBar, QGraphicsPixmapItem,
    QStatusBar, QMenu, QFileDialog,
)
from PySide6.QtCore import Qt, QRect, QRectF, Signal
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent,
    QPixmap, QPainterPath, QFontMetrics, QContextMenuEvent, QWheelEvent,
)

# 再生エンジン
from playback_engine import PlaybackEngine, TransportController

# ──────────────────────────────────────────────────────────────────
# プラットフォーム別フォント
# ──────────────────────────────────────────────────────────────────

_SYS = platform.system()
_FONT_FAMILY = (
    ".AppleSystemUIFont" if _SYS == "Darwin"
    else "Segoe UI"       if _SYS == "Windows"
    else "Inter"
)

# ══════════════════════════════════════════════════════════════════
# VO-SE Engine — 型定義と動的ロード
# ══════════════════════════════════════════════════════════════════

is_engine_available: bool = False

if TYPE_CHECKING:
    class IntonationAnalyzer:
        def __init__(self) -> None: ...
        def analyze(self, text: str) -> str: ...
        def analyze_to_phonemes(self, text: str) -> List[str]: ...
        def analyze_to_accent_phrases(self, text: str) -> Any: ...

    class TalkManager:
        def __init__(self) -> None: ...
        def set_voice(self, path: str) -> bool: ...
        def synthesize(self, text: str, output_path: str, speed: float = 1.0) -> Tuple[bool, str]: ...

    def generate_talk_events(text: str, analyzer: IntonationAnalyzer) -> List[Dict[str, Any]]: ...

else:
    try:
        import vo_se_engine
        IntonationAnalyzer   = vo_se_engine.IntonationAnalyzer
        TalkManager          = vo_se_engine.TalkManager
        generate_talk_events = vo_se_engine.generate_talk_events
        is_engine_available  = True
    except (ImportError, AttributeError) as e:
        print(f"VO-SE Engine not available: {e}")

        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        class IntonationAnalyzer:  # type: ignore[no-redef]
            pass

        class TalkManager:  # type: ignore[no-redef]
            pass

        is_engine_available = False

__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]


def get_wav_duration_px(file_path: str, px_per_sec: int = 100) -> int:
    if not os.path.exists(file_path):
        return 200
    try:
        with wave.open(file_path, "rb") as wr:
            return int(wr.getnframes() / float(wr.getframerate()) * px_per_sec)
    except Exception as e:
        print(f"WAV parse error: {e}")
        return 200


# ══════════════════════════════════════════════════════════════════
# C++ 構造体バインディング
# ══════════════════════════════════════════════════════════════════

class NoteEvent(ctypes.Structure):
    _fields_ = [
        ("wav_path",          ctypes.c_char_p),
        ("pitch_length",      ctypes.c_int),
        ("pitch_curve",       ctypes.POINTER(ctypes.c_double)),
        ("gender_curve",      ctypes.POINTER(ctypes.c_double)),
        ("tension_curve",     ctypes.POINTER(ctypes.c_double)),
        ("breath_curve",      ctypes.POINTER(ctypes.c_double)),
        ("offset_ms",         ctypes.c_double),
        ("consonant_ms",      ctypes.c_double),
        ("cutoff_ms",         ctypes.c_double),
        ("pre_utterance_ms",  ctypes.c_double),
        ("overlap_ms",        ctypes.c_double),
    ]


class VOSEBridge:
    def __init__(self) -> None:
        self.lib: Optional[ctypes.CDLL] = None
        self.keep_alive: List[Any] = []
        self._load_engine()

    def _load_engine(self) -> None:
        is_mac   = (_SYS == "Darwin")
        ext      = ".dylib" if is_mac else ".dll"
        base_dir = os.path.dirname(os.path.abspath(__file__))
        lib_path = os.path.join(base_dir, "bin", f"libvo_se_cut{ext}")
        if not os.path.exists(lib_path):
            print(f"⚠️ Engine not found: {lib_path}")
            return
        try:
            if is_mac:
                self.lib = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
            else:
                self.lib = ctypes.CDLL(lib_path)
            if hasattr(self.lib, "init_official_engine"):
                self.lib.init_official_engine.argtypes = []
                self.lib.init_official_engine.restype  = None
                self.lib.init_official_engine()
            self.lib.execute_render.argtypes = [
                ctypes.POINTER(NoteEvent), ctypes.c_int,
                ctypes.c_char_p, ctypes.c_int,
            ]
            self.lib.execute_render.restype = None
            print(f"✅ VO-SE Engine loaded: {lib_path}")
        except Exception as e:
            print(f"❌ Engine load failed: {e}\n{traceback.format_exc()}")
            self.lib = None

    def render(self, notes_list: List[Dict[str, Any]], output_file: str = "output.wav") -> None:
        if not self.lib:
            return
        count = len(notes_list)
        if count == 0:
            return
        NotesArray = NoteEvent * count
        c_notes    = NotesArray()
        self.keep_alive = []
        for i, data in enumerate(notes_list):
            phoneme = str(data.get("phoneme", "a"))
            pitch   = list(data.get("pitch",   [150.0] * 50))
            gender  = list(data.get("gender",  [0.5]   * 50))
            tension = list(data.get("tension", [0.5]   * 50))
            breath  = list(data.get("breath",  [0.1]   * 50))
            c_wav = phoneme.encode("utf-8")
            c_p   = (ctypes.c_double * len(pitch))(*pitch)
            c_g   = (ctypes.c_double * len(gender))(*gender)
            c_t   = (ctypes.c_double * len(tension))(*tension)
            c_b   = (ctypes.c_double * len(breath))(*breath)
            self.keep_alive.extend([c_wav, c_p, c_g, c_t, c_b])
            c_notes[i].wav_path         = c_wav
            c_notes[i].pitch_length     = len(pitch)
            c_notes[i].pitch_curve      = c_p
            c_notes[i].gender_curve     = c_g
            c_notes[i].tension_curve    = c_t
            c_notes[i].breath_curve     = c_b
            c_notes[i].offset_ms        = float(data.get("offset",        0.0))
            c_notes[i].consonant_ms     = float(data.get("consonant",     0.0))
            c_notes[i].cutoff_ms        = float(data.get("cutoff",        0.0))
            c_notes[i].pre_utterance_ms = float(data.get("pre_utterance", 0.0))
            c_notes[i].overlap_ms       = float(data.get("overlap",       0.0))
        try:
            self.lib.execute_render(c_notes, count, output_file.encode("utf-8"))
            print(f"🎬 Render complete: {output_file}")
        except Exception as e:
            print(f"❌ execute_render error: {e}\n{traceback.format_exc()}")


# ══════════════════════════════════════════════════════════════════
# Apple Dark Theme
# ══════════════════════════════════════════════════════════════════

APP_STYLE = """
QWidget {
    background-color: #1c1c1e;
    color: #ffffff;
    font-size: 13px;
}
QSplitter::handle:horizontal { width:  1px; background-color: #3a3a3c; }
QSplitter::handle:vertical   { height: 1px; background-color: #3a3a3c; }
QFrame  { border: none; }
QLabel  { background: transparent; }

QTextEdit {
    background-color: #2c2c2e;
    color: #ffffff;
    border: 1px solid #3a3a3c;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: #0a84ff;
}
QTextEdit:focus { border: 1.5px solid #0a84ff; }

QPushButton {
    background-color: #3a3a3c;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover   { background-color: #48484a; }
QPushButton:pressed { background-color: #2c2c2e; }

QListWidget {
    background-color: #2c2c2e;
    border: 1px solid #3a3a3c;
    border-radius: 10px;
    color: #ffffff;
    padding: 4px;
    outline: none;
}
QListWidget::item             { padding: 7px 10px; border-radius: 6px; margin: 1px 2px; }
QListWidget::item:hover       { background-color: #3a3a3c; }
QListWidget::item:selected    { background-color: #0a84ff; }

QScrollBar:horizontal         { height: 6px; background: transparent; border: none; }
QScrollBar::handle:horizontal { background: #48484a; border-radius: 3px; min-width: 40px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar:vertical           { width: 6px; background: transparent; border: none; }
QScrollBar::handle:vertical   { background: #48484a; border-radius: 3px; min-height: 40px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QStatusBar {
    background-color: #1c1c1e;
    color: #636366;
    font-size: 11px;
    border-top: 1px solid #3a3a3c;
}
QMenu {
    background-color: #2c2c2e;
    border: 1px solid #48484a;
    border-radius: 10px;
    padding: 5px;
    color: #ffffff;
}
QMenu::item            { padding: 7px 18px; border-radius: 6px; font-size: 13px; margin: 1px; }
QMenu::item:selected   { background-color: #0a84ff; }
QMenu::separator       { height: 1px; background-color: #3a3a3c; margin: 4px 10px; }
QGraphicsView { background-color: #000000; border: none; }
"""


# ══════════════════════════════════════════════════════════════════
# TimelineHeader
# ══════════════════════════════════════════════════════════════════

class TimelineHeader(QWidget):
    positionChanged = Signal(int)

    _RED  = QColor(255, 69, 58)
    _TICK = QColor(72, 72, 78)
    _LBL  = QColor(110, 110, 120)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background-color: #111113;")
        self.playhead_x: int   = 50
        self.is_dragging: bool = False
        self.px_per_sec: int   = 100

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(_FONT_FAMILY, 8)
        p.setFont(font)
        step = self.px_per_sec
        for x in range(0, self.width(), step):
            sec = x // step
            p.setPen(QPen(self._TICK, 1))
            p.drawLine(x, 16, x, 28)
            p.setPen(self._LBL)
            p.drawText(x + 4, 13, f"{sec // 60:d}:{sec % 60:02d}")
            hx = x + step // 2
            if hx < self.width():
                p.setPen(QPen(QColor(50, 50, 56), 1))
                p.drawLine(hx, 22, hx, 28)
        px = float(self.playhead_x)
        p.setPen(QPen(self._RED, 2))
        p.drawLine(int(px), 0, int(px), 28)
        path = QPainterPath()
        path.moveTo(px - 5, 0)
        path.lineTo(px + 5, 0)
        path.lineTo(px,     9)
        path.closeSubpath()
        p.setBrush(QBrush(self._RED))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self._update_playhead(int(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self._update_playhead(int(event.position().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self.is_dragging = False
            # ★ ヘッド操作でシーク
            if hasattr(self, "_on_seek_from_header"):
                self._on_seek_from_header(self.playhead_x / self.px_per_sec)

    def _update_playhead(self, x: int) -> None:
        self.playhead_x = max(0, min(x, self.width()))
        self.update()
        self.positionChanged.emit(self.playhead_x)

    def set_playhead(self, x: int) -> None:
        self.playhead_x = max(0, x)
        self.update()


# ══════════════════════════════════════════════════════════════════
# TimelineTrack
# ══════════════════════════════════════════════════════════════════

class TimelineTrack(QFrame):
    synthesize_requested = Signal(str, int)
    HEADER_W = 110

    def __init__(self, name: str, color: QColor = QColor(10, 132, 255),
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(64)
        self.setStyleSheet("")
        self.track_name  = name
        self.track_color = color
        self.dragging_clip_idx: Optional[int] = None
        self.drag_start_offset: int = 0
        self.setMouseTracking(True)
        self.clips: List[Dict[str, Any]] = []

    def add_clip(self, x: int, width: int, text: str,
                 color: Optional[QColor] = None, raw_text: str = "") -> None:
        self.clips.append({
            "x": x, "width": max(4, width),
            "text": text, "raw_text": raw_text or text,
            "color": color or self.track_color,
        })
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        clip = self._clip_at(event.pos().x() - self.HEADER_W)
        if not clip:
            return
        menu      = QMenu(self)
        synth_act = menu.addAction("🎙️  音声を合成")
        menu.addSeparator()
        del_act   = menu.addAction("🗑️  削除")
        chosen    = menu.exec(event.globalPos())
        if chosen == synth_act:
            self.synthesize_requested.emit(clip["raw_text"], clip["x"])
        elif chosen == del_act:
            self.clips.remove(clip)
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x() - self.HEADER_W
        for i, clip in enumerate(reversed(self.clips)):
            idx = len(self.clips) - 1 - i
            if clip["x"] <= x <= clip["x"] + clip["width"]:
                self.dragging_clip_idx = idx
                self.drag_start_offset = int(x - clip["x"])
                break

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.dragging_clip_idx is not None:
            new_x = event.position().x() - self.HEADER_W - self.drag_start_offset
            self.clips[self.dragging_clip_idx]["x"] = max(0, int(new_x))
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.dragging_clip_idx = None

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(20, 20, 22))
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(0, h - 1, w, h - 1)
        p.fillRect(0, 0, self.HEADER_W, h, QColor(26, 26, 28))
        p.fillRect(0, 0, 3, h, self.track_color)
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(self.HEADER_W, 0, self.HEADER_W, h)
        font = QFont(_FONT_FAMILY, 10, QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QColor(190, 190, 198))
        p.drawText(QRect(12, 0, self.HEADER_W - 14, h),
                   Qt.AlignmentFlag.AlignVCenter, self.track_name)
        for clip in self.clips:
            self._paint_clip(p, clip, h)
        p.end()

    def _paint_clip(self, p: QPainter, clip: Dict[str, Any], track_h: int) -> None:
        CX = float(self.HEADER_W + clip["x"])
        CY, CW, CH = 7.0, float(clip["width"]), float(track_h - 14)
        c: QColor = clip["color"]
        rect = QRectF(CX, CY, CW, CH)
        fill = QColor(c); fill.setAlpha(175)
        p.setBrush(QBrush(fill))
        border = QColor(c).lighter(145); border.setAlpha(190)
        p.setPen(QPen(border, 0.75))
        p.drawRoundedRect(rect, 5.0, 5.0)
        p.setBrush(QBrush(QColor(255, 255, 255, 26)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(CX + 1, CY + 1, CW - 2, (CH - 2) * 0.38), 4.0, 4.0)
        if CW > 24:
            font = QFont(_FONT_FAMILY, 9, QFont.Weight.Medium)
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 215))
            text_rect = QRect(int(CX) + 7, int(CY), int(CW) - 14, int(CH))
            elided = QFontMetrics(font).elidedText(
                clip["text"], Qt.TextElideMode.ElideRight, text_rect.width())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)

    def _clip_at(self, x: float) -> Optional[Dict[str, Any]]:
        for clip in reversed(self.clips):
            if clip["x"] <= x <= clip["x"] + clip["width"]:
                return clip
        return None


# ══════════════════════════════════════════════════════════════════
# TimelineWidget
# ══════════════════════════════════════════════════════════════════

class TimelineWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #141416;")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header      = TimelineHeader()
        self.voice_track = TimelineTrack("🎙  VOICE", QColor(10, 132, 255))
        self.video_track = TimelineTrack("🎬  VIDEO", QColor(48, 209, 88))
        layout.addWidget(self.header)
        layout.addWidget(self.voice_track)
        layout.addWidget(self.video_track)
        layout.addStretch()
        self.h_scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.h_scrollbar.setStyleSheet("""
            QScrollBar:horizontal { height: 8px; background: #0d0d0f; border: none; }
            QScrollBar::handle:horizontal { background: #3a3a3c; border-radius: 4px; min-width: 40px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        """)
        layout.addWidget(self.h_scrollbar)


# ══════════════════════════════════════════════════════════════════
# PreviewView
# ══════════════════════════════════════════════════════════════════

class PreviewView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene()
        self.setScene(self._scene)
        self.setBackgroundBrush(QBrush(QColor(8, 8, 10)))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setStyleSheet("border: none;")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._scene.setSceneRect(0, 0, 1920, 1080)
        self.centerOn(960, 540)
        self._scene.addRect(
            self._scene.sceneRect(),
            QPen(QColor(50, 50, 55), 2),
            QBrush(QColor(12, 12, 14)),
        )
        ph = self._scene.addText("プレビューエリア")
        ph.setDefaultTextColor(QColor(65, 65, 70))
        ph.setFont(QFont(_FONT_FAMILY, 20))
        ph.setPos(960 - ph.boundingRect().width() / 2, 540 - 14)
        self.current_character: Optional[QGraphicsPixmapItem] = None

    def add_character(self, image_path: str) -> None:
        pix = QPixmap(image_path)
        if pix.isNull():
            return
        item = QGraphicsPixmapItem(pix)
        item.setFlags(
            QGraphicsPixmapItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsPixmapItem.GraphicsItemFlag.ItemIsSelectable
        )
        item.setPos(960 - pix.width() / 2, 540 - pix.height() / 2)
        self._scene.addItem(item)
        self.current_character = item

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 0.87
            self.scale(factor, factor)
        else:
            super().wheelEvent(event)


# ══════════════════════════════════════════════════════════════════
# CutStudioMain — 再生エンジン統合版
# ══════════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio")
        self.resize(1440, 900)
        self.setMinimumSize(960, 600)

        # ── VideoEngine ──────────────────────────────────────────
        self.video: Optional[video_engine.VideoEngine] = None
        try:
            self.video = video_engine.VideoEngine()
        except Exception as e:
            print(f"⚠️ VideoEngine Initialization Failed: {e}")

        # ── VO-SE Bridge ─────────────────────────────────────────
        self.bridge: VOSEBridge = VOSEBridge()
        self.analyzer: Optional[IntonationAnalyzer] = None
        self.talk_manager: Optional[TalkManager]    = None
        if is_engine_available:
            self.analyzer     = IntonationAnalyzer()
            self.talk_manager = TalkManager()

        # ── PreviewView ──────────────────────────────────────────
        self.video_preview = PreviewView()

        # ── Transport buttons (populated in _make_toolbar) ───────
        self._transport_btns: List[QPushButton] = []
        self._export_btn: Optional[QPushButton] = None

        self._init_ui()

        # ── PlaybackEngine (toolbar + preview が揃った後に生成) ──
        self.playback_engine = PlaybackEngine(
            preview_view    = self.video_preview,
            timeline_header = self.timeline.header,
            status_bar      = self._status,
        )

        # タイムラインヘッドのドラッグ → シーク接続
        self.timeline.header._on_seek_from_header = (  # type: ignore[attr-defined]
            lambda sec: self.playback_engine.seek(sec)
        )

        # ── TransportController ──────────────────────────────────
        if len(self._transport_btns) >= 5:
            self.transport = TransportController(
                engine          = self.playback_engine,
                btn_top         = self._transport_btns[0],
                btn_prev        = self._transport_btns[1],
                btn_play        = self._transport_btns[2],
                btn_next        = self._transport_btns[3],
                btn_end         = self._transport_btns[4],
                export_btn      = self._export_btn,
                export_callback = self._on_export_clicked,
            )

    @property
    def has_video_engine(self) -> bool:
        return self.video is not None

    # ── UI構築 ────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_toolbar())

        v_split = QSplitter(Qt.Orientation.Vertical)
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.addWidget(self._make_sidebar())
        h_split.addWidget(self.video_preview)
        h_split.setStretchFactor(0, 0)
        h_split.setStretchFactor(1, 1)
        h_split.setSizes([220, 1100])

        tl_frame = QFrame()
        tl_frame.setStyleSheet("background-color: #141416;")
        tl_layout = QVBoxLayout(tl_frame)
        tl_layout.setContentsMargins(0, 0, 0, 0)
        tl_layout.setSpacing(0)

        self.timeline = TimelineWidget()
        self.timeline.voice_track.synthesize_requested.connect(self._on_synthesize_from_clip)
        self.timeline.video_track.synthesize_requested.connect(self._on_synthesize_from_clip)
        tl_layout.addWidget(self.timeline)

        v_split.addWidget(h_split)
        v_split.addWidget(tl_frame)
        v_split.setStretchFactor(0, 6)
        v_split.setStretchFactor(1, 4)
        v_split.setSizes([580, 220])

        root.addWidget(v_split)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("VO-SE Cut Studio  —  Early Alpha")

    def _make_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setObjectName("toolbar")
        bar.setStyleSheet("""
            QFrame#toolbar {
                background-color: #232325;
                border-bottom: 1px solid #3a3a3c;
            }
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(6)

        title = QLabel("VO-SE Cut Studio")
        title.setStyleSheet(
            "color:#ffffff; font-size:14px; font-weight:600; background:transparent;"
        )
        layout.addWidget(title)
        layout.addStretch()

        btn_style = """
            QPushButton {
                background: transparent; color: #c5c5c7; border: none;
                font-size: 16px; border-radius: 6px;
                min-width: 32px; min-height: 28px;
            }
            QPushButton:hover   { background-color: #3a3a3c; color: #ffffff; }
            QPushButton:pressed { background-color: #2c2c2e; }
        """
        controls = [
            ("⏮", "先頭へ"),
            ("⏪", "5秒戻る"),
            ("▶", "再生 / 一時停止"),
            ("⏩", "5秒進む"),
            ("⏭", "末尾へ"),
        ]
        # ★ Fix: ボタンを1回だけ生成してリストに保持
        for icon, tip in controls:
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn.setStyleSheet(btn_style)
            btn.setFixedSize(32, 28)
            layout.addWidget(btn)
            self._transport_btns.append(btn)

        layout.addStretch()

        export_btn = QPushButton("書き出し")
        export_btn.setFixedHeight(28)
        export_btn.setStyleSheet("""
            QPushButton {
                background-color: #0a84ff; color: #ffffff; border: none;
                border-radius: 7px; padding: 0 18px;
                font-size: 13px; font-weight: 500;
            }
            QPushButton:hover   { background-color: #409cff; }
            QPushButton:pressed { background-color: #0070e0; }
        """)
        layout.addWidget(export_btn)
        self._export_btn = export_btn   # ★ インスタンス変数化
        return bar

    def _make_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setObjectName("sidebar")
        sidebar.setStyleSheet("""
            QFrame#sidebar {
                background-color: #1c1c1e;
                border-right: 1px solid #3a3a3c;
            }
        """)
        ly = QVBoxLayout(sidebar)
        ly.setContentsMargins(12, 16, 12, 16)
        ly.setSpacing(8)

        # ── 素材ライブラリ ─────────────────────────────────────
        ly.addWidget(self._section_label("素材ライブラリ"))
        self.asset_list = QListWidget()
        self.asset_list.setFixedHeight(130)
        self.asset_list.setToolTip("ダブルクリックで再生")
        # ★ ダブルクリックでロード
        self.asset_list.itemDoubleClicked.connect(self._on_asset_double_clicked)
        ly.addWidget(self.asset_list)

        # ★ ファイル追加ボタン
        add_btn = QPushButton("＋  ファイルを追加")
        add_btn.setFixedHeight(28)
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2e; color: #ebebf5;
                border: 1px solid #3a3a3c; border-radius: 7px;
                font-size: 12px;
            }
            QPushButton:hover   { background-color: #3a3a3c; }
            QPushButton:pressed { background-color: #1c1c1e; }
        """)
        add_btn.clicked.connect(self._on_add_asset)
        ly.addWidget(add_btn)

        ly.addWidget(self._divider())
        ly.addSpacing(4)

        # ── 音声合成 (TTS) ────────────────────────────────────
        ly.addWidget(self._section_label("音声合成 (TTS)"))
        self.tts_input = QTextEdit()
        self.tts_input.setPlaceholderText("テキストを入力…")
        self.tts_input.setFixedHeight(80)
        ly.addWidget(self.tts_input)

        self.generate_button = QPushButton("音声を合成して配置")
        self.generate_button.setFixedHeight(34)
        self.generate_button.setStyleSheet("""
            QPushButton {
                background-color: #0a84ff; color: #ffffff; border: none;
                border-radius: 8px; font-size: 13px; font-weight: 500;
            }
            QPushButton:hover    { background-color: #409cff; }
            QPushButton:pressed  { background-color: #0070e0; }
            QPushButton:disabled { background-color: #1a3356; color: #5070a0; }
        """)
        self.generate_button.clicked.connect(self._on_generate_clicked)
        ly.addWidget(self.generate_button)

        ly.addStretch()

        ok    = is_engine_available
        label = QLabel("✅  エンジン接続済" if ok else "⚠️  エンジン未接続")
        label.setStyleSheet(
            f"color: {'#30d158' if ok else '#ff9f0a'};"
            " font-size:11px; background:transparent;"
        )
        ly.addWidget(label)
        return sidebar

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:#636366; font-size:10px; font-weight:700;"
            " letter-spacing:0.6px; background:transparent; padding:2px 0;"
        )
        return lbl

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.NoFrame)
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: #3a3a3c;")
        return line

    # ── Slots ─────────────────────────────────────────────────────

    def _on_generate_clicked(self) -> None:
        text = self.tts_input.toPlainText().strip()
        if not text:
            return
        self.generate_button.setEnabled(False)
        self.generate_button.setText("合成中…")
        self._status.showMessage("🎙️  合成中…")

        wav_path   = "output_tts.wav"
        clip_width = 200

        if self.talk_manager:
            ok, _ = self.talk_manager.synthesize(text, wav_path)
            if not ok:
                self._status.showMessage("❌  TTS 合成に失敗しました")
                self.generate_button.setEnabled(True)
                self.generate_button.setText("音声を合成して配置")
                return
            clip_width = get_wav_duration_px(wav_path)

        start_x    = self.timeline.header.playhead_x
        short_text = (text[:16] + "…") if len(text) > 16 else text

        self.timeline.voice_track.add_clip(
            start_x, clip_width, f"🎙  {short_text}",
            color=QColor(10, 132, 255), raw_text=text,
        )
        self.timeline.video_track.add_clip(
            start_x, clip_width, f"💬  {short_text}",
            color=QColor(48, 209, 88), raw_text=text,
        )
        self.timeline.header.set_playhead(start_x + clip_width)

        if is_engine_available and self.analyzer:
            notes = generate_talk_events(text, self.analyzer)
            if notes:
                self.bridge.render(notes, output_file="output_rendered.wav")

        self.tts_input.clear()
        self._status.showMessage(f"✅  合成完了: {short_text}")
        self.generate_button.setEnabled(True)
        self.generate_button.setText("音声を合成して配置")

    def _on_synthesize_from_clip(self, text: str, clip_x: int) -> None:
        if not text or not self.talk_manager:
            self._status.showMessage("⚠️  TalkManager が初期化されていません")
            return
        self._status.showMessage(f"🎙️  クリップから合成中: {text[:20]}…")
        wav_path = "output_clip_tts.wav"
        ok, _    = self.talk_manager.synthesize(text, wav_path)
        if not ok:
            self._status.showMessage("❌  TTS 合成に失敗しました")
            return
        clip_width = get_wav_duration_px(wav_path)
        short_text = (text[:16] + "…") if len(text) > 16 else text
        self.timeline.voice_track.add_clip(
            clip_x, clip_width, f"🎙  {short_text}",
            color=QColor(10, 132, 255), raw_text=text,
        )
        self.timeline.header.set_playhead(clip_x + clip_width)
        self._status.showMessage(f"✅  クリップ合成完了: {short_text}")

    # ★ 書き出しダイアログ
    def _on_export_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "書き出し先を選択", "output.mp4",
            "MP4 (*.mp4);;MOV (*.mov);;MKV (*.mkv)"
        )
        if not path:
            return
        self._status.showMessage(f"📤  書き出し中: {path} …")

        if self.has_video_engine and self.video:
            import json
            edl_entries = []
            for clip in self.timeline.voice_track.clips:
                in_pt  = clip["x"] / 100.0
                out_pt = (clip["x"] + clip["width"]) / 100.0
                edl_entries.append({"in": in_pt, "out": out_pt, "enabled": True})
            edl_json = json.dumps(edl_entries)
            ok = self.video.export_hw(edl_json, path, quality=23)
            msg = f"✅  書き出し完了: {path}" if ok else "❌  書き出しに失敗しました"
        else:
            msg = "⚠️  VideoEngine が利用できません。書き出しをスキップ。"
        self._status.showMessage(msg)

    # ★ 素材追加
    def _on_add_asset(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "素材ファイルを追加", "",
            "動画・音声 (*.mp4 *.mov *.mkv *.avi *.wav *.mp3 *.aac *.flac)"
        )
        for path in paths:
            name = os.path.basename(path)
            icon = "🔊" if path.lower().endswith((".wav", ".mp3", ".aac", ".flac")) else "🎬"
            item = QListWidgetItem(f"{icon}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.asset_list.addItem(item)

    # ★ ダブルクリックで再生ロード
    def _on_asset_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        ok = self.playback_engine.load(path)
        if ok and self.has_video_engine and self.video:
            self.video.load_video(path)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """ウィンドウ閉じる時に再生を安全停止"""
        self.playback_engine.stop()
        super().closeEvent(event)


# ══════════════════════════════════════════════════════════════════
# エントリーポイント
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if _SYS == "Darwin":
        app.setFont(QFont(".AppleSystemUIFont", 13))
    elif _SYS == "Windows":
        app.setFont(QFont("Segoe UI", 10))
    else:
        app.setFont(QFont("Inter", 10))
    app.setStyleSheet(APP_STYLE)
    window = CutStudioMain()
    window.show()
    sys.exit(app.exec())
