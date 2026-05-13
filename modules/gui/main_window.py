# main_window.py
"""
VO-SE Cut Studio — メインウィンドウ（Apple風 Dark UI / 動画編集専用）
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
    QSplitter, QTextEdit, QPushButton, QLabel, QListWidget, QFrame,
    QGraphicsView, QGraphicsScene, QScrollBar, QGraphicsPixmapItem,
    QStatusBar, QMenu,
)
from PySide6.QtCore import Qt, QRect, QRectF, Signal
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent,
    QPixmap, QPainterPath, QFontMetrics, QContextMenuEvent, QWheelEvent,
)

# ──────────────────────────────────────────────────────────────
# プラットフォーム別フォント
# ──────────────────────────────────────────────────────────────

_SYS = platform.system()
_FONT_FAMILY = (
    ".AppleSystemUIFont" if _SYS == "Darwin"
    else "Segoe UI"       if _SYS == "Windows"
    else "Inter"
)

# ══════════════════════════════════════════════════════════════
# VO-SE Engine — 型定義と動的ロード（Pyright 完全対応版）
# ══════════════════════════════════════════════════════════════

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
        def synthesize(
            self, text: str, output_path: str, speed: float = 1.0
        ) -> Tuple[bool, str]: ...

    def generate_talk_events(
        text: str, analyzer: IntonationAnalyzer
    ) -> List[Dict[str, Any]]: ...

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
    """WAV ファイルの正確な長さをピクセルに変換する"""
    if not os.path.exists(file_path):
        return 200
    try:
        with wave.open(file_path, "rb") as wr:
            return int(wr.getnframes() / float(wr.getframerate()) * px_per_sec)
    except Exception as e:
        print(f"WAV parse error: {e}")
        return 200


# ══════════════════════════════════════════════════════════════
# C++ 構造体バインディング（UTAU 対応フルセット）
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# DLL / dylib ブリッジ
# ══════════════════════════════════════════════════════════════

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
                ctypes.POINTER(NoteEvent),
                ctypes.c_int,
                ctypes.c_char_p,
            ]
            self.lib.execute_render.restype = None
            print(f"✅ VO-SE Engine loaded: {lib_path}")
        except Exception as e:
            print(f"❌ Engine load failed: {e}\n{traceback.format_exc()}")
            self.lib = None

    def render(
        self, notes_list: List[Dict[str, Any]], output_file: str = "output.wav"
    ) -> None:
        if not self.lib:
            print("❌ Engine not loaded.")
            return
        count = len(notes_list)
        if count == 0:
            return

        NotesArray  = NoteEvent * count
        c_notes     = NotesArray()
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


# ══════════════════════════════════════════════════════════════
# Apple Dark Theme — アプリ全体のスタイルシート
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# TimelineHeader — 時間目盛り＆再生ヘッド
# ══════════════════════════════════════════════════════════════

class TimelineHeader(QWidget):
    positionChanged = Signal(int)

    _RED  = QColor(255, 69, 58)    # macOS systemRed
    _TICK = QColor(72, 72, 78)
    _LBL  = QColor(110, 110, 120)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background-color: #111113;")
        self.playhead_x: int  = 50
        self.is_dragging: bool = False
        self.px_per_sec: int  = 100

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont(_FONT_FAMILY, 8)
        p.setFont(font)

        step = self.px_per_sec
        for x in range(0, self.width(), step):
            sec = x // step
            # Major tick
            p.setPen(QPen(self._TICK, 1))
            p.drawLine(x, 16, x, 28)
            # Time label
            p.setPen(self._LBL)
            p.drawText(x + 4, 13, f"{sec // 60:d}:{sec % 60:02d}")
            # Minor tick at half-second
            hx = x + step // 2
            if hx < self.width():
                p.setPen(QPen(QColor(50, 50, 56), 1))
                p.drawLine(hx, 22, hx, 28)

        # Playhead line
        px = float(self.playhead_x)
        p.setPen(QPen(self._RED, 2))
        p.drawLine(int(px), 0, int(px), 28)

        # Playhead triangle
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
        self.is_dragging = False

    def _update_playhead(self, x: int) -> None:
        self.playhead_x = max(0, min(x, self.width()))
        self.update()
        self.positionChanged.emit(self.playhead_x)

    def set_playhead(self, x: int) -> None:
        self._update_playhead(x)


# ══════════════════════════════════════════════════════════════
# TimelineTrack — 各トラック（右クリックメニュー・raw_text 対応）
# ══════════════════════════════════════════════════════════════

class TimelineTrack(QFrame):
    """タイムラインの各トラック

    Signals
    -------
    synthesize_requested(raw_text: str, clip_x: int)
        右クリックメニュー「音声を合成」を選んだときに発火。
    """

    synthesize_requested = Signal(str, int)

    HEADER_W = 110   # ヘッダー部分の幅（px）

    def __init__(
        self,
        name:   str,
        color:  QColor = QColor(10, 132, 255),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedHeight(64)
        self.setStyleSheet("")          # 全描画を paintEvent で管理
        self.track_name  = name
        self.track_color = color
        self.dragging_clip_idx: Optional[int] = None
        self.drag_start_offset: int = 0
        self.setMouseTracking(True)
        self.clips: List[Dict[str, Any]] = []

    # ── Public API ────────────────────────────────────────────

    def add_clip(
        self,
        x:        int,
        width:    int,
        text:     str,
        color:    Optional[QColor] = None,
        raw_text: str = "",
    ) -> None:
        """クリップを追加する。

        Parameters
        ----------
        raw_text : str
            TTS に渡す完全なテキスト。省略時は `text` を使用。
        """
        self.clips.append({
            "x":        x,
            "width":    max(4, width),
            "text":     text,
            "raw_text": raw_text or text,
            "color":    color or self.track_color,
        })
        self.update()

    # ── Events ────────────────────────────────────────────────

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

    # ── Paint ─────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Track body
        p.fillRect(0, 0, w, h, QColor(20, 20, 22))

        # Bottom separator
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(0, h - 1, w, h - 1)

        # ── Header block ──────────────────────────────────────
        p.fillRect(0, 0, self.HEADER_W, h, QColor(26, 26, 28))

        # Colored left accent bar
        p.fillRect(0, 0, 3, h, self.track_color)

        # Vertical separator (header | body)
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(self.HEADER_W, 0, self.HEADER_W, h)

        # Track name
        font = QFont(_FONT_FAMILY, 10, QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QColor(190, 190, 198))
        p.drawText(
            QRect(12, 0, self.HEADER_W - 14, h),
            Qt.AlignmentFlag.AlignVCenter,
            self.track_name,
        )

        # ── Clips ─────────────────────────────────────────────
        for clip in self.clips:
            self._paint_clip(p, clip, h)

        p.end()

    def _paint_clip(self, p: QPainter, clip: Dict[str, Any], track_h: int) -> None:
        CX = float(self.HEADER_W + clip["x"])
        CY = 7.0
        CW = float(clip["width"])
        CH = float(track_h - 14)
        c: QColor = clip["color"]

        rect = QRectF(CX, CY, CW, CH)

        # Semi-transparent fill
        fill = QColor(c)
        fill.setAlpha(175)
        p.setBrush(QBrush(fill))

        # Lighter border
        border = QColor(c).lighter(145)
        border.setAlpha(190)
        p.setPen(QPen(border, 0.75))
        p.drawRoundedRect(rect, 5.0, 5.0)

        # Top highlight strip (top ~38% of clip height)
        p.setBrush(QBrush(QColor(255, 255, 255, 26)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(CX + 1, CY + 1, CW - 2, (CH - 2) * 0.38), 4.0, 4.0)

        # Elided label
        if CW > 24:
            font = QFont(_FONT_FAMILY, 9, QFont.Weight.Medium)
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 215))
            text_rect = QRect(int(CX) + 7, int(CY), int(CW) - 14, int(CH))
            elided    = QFontMetrics(font).elidedText(
                clip["text"], Qt.TextElideMode.ElideRight, text_rect.width()
            )
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)

    # ── Helper ────────────────────────────────────────────────

    def _clip_at(self, x: float) -> Optional[Dict[str, Any]]:
        for clip in reversed(self.clips):
            if clip["x"] <= x <= clip["x"] + clip["width"]:
                return clip
        return None


# ══════════════════════════════════════════════════════════════
# TimelineWidget — VOICE + VIDEO の 2 トラック管理
# ══════════════════════════════════════════════════════════════

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
        self.voice_track = TimelineTrack("🎙  VOICE", QColor(10, 132, 255))   # systemBlue
        self.video_track = TimelineTrack("🎬  VIDEO", QColor(48, 209, 88))    # systemGreen

        layout.addWidget(self.header)
        layout.addWidget(self.voice_track)
        layout.addWidget(self.video_track)
        layout.addStretch()

        self.h_scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.h_scrollbar.setStyleSheet("""
            QScrollBar:horizontal {
                height: 8px;
                background: #0d0d0f;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background: #3a3a3c;
                border-radius: 4px;
                min-width: 40px;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal { width: 0; }
        """)
        layout.addWidget(self.h_scrollbar)


# ══════════════════════════════════════════════════════════════
# PreviewView — 動画プレビュー（Ctrl+スクロールでズーム）
# ══════════════════════════════════════════════════════════════

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

        # 1920×1080 仮想キャンバス
        self._scene.setSceneRect(0, 0, 1920, 1080)
        self.centerOn(960, 540)

        # キャンバス外枠
        self._scene.addRect(
            self._scene.sceneRect(),
            QPen(QColor(50, 50, 55), 2),
            QBrush(QColor(12, 12, 14)),
        )

        # プレースホルダーテキスト
        ph = self._scene.addText("プレビューエリア")
        ph.setDefaultTextColor(QColor(65, 65, 70))
        ph.setFont(QFont(_FONT_FAMILY, 20))
        ph.setPos(960 - ph.boundingRect().width() / 2, 540 - 14)

        self.current_character: Optional[QGraphicsPixmapItem] = None

    def add_character(self, image_path: str) -> None:
        pix = QPixmap(image_path)
        if pix.isNull():
            print(f"❌ 画像読み込み失敗: {image_path}")
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
        # 1. 修飾キーの取得
        modifiers = event.modifiers()
        
        # 2. Controlキーが押されている場合（ズーム処理）
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Pyright の型定義では angleDelta() は常に QPoint を返すため None チェック不要
            delta_y = event.angleDelta().y()
            
            # y() の値によって拡大・縮小率を決定
            factor = 1.15 if delta_y > 0 else 0.87
            self.scale(factor, factor)
        else:
            # 3. それ以外は標準のスクロール挙動
            super().wheelEvent(event)


# ══════════════════════════════════════════════════════════════
# CutStudioMain — メインウィンドウ（Apple ライク UI）
# ══════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio")
        self.resize(1440, 900)
        self.setMinimumSize(960, 600)

        # 1. 映像エンジンの初期化（型を明示）
        self.video: Optional[video_engine.VideoEngine] = None
        try:
            # video_engine.py 側でライブラリロードに失敗すると例外が出る想定
            self.video = video_engine.VideoEngine()
        except Exception as e:
            print(f"⚠️ VideoEngine Initialization Failed: {e}")

        # 2. VO-SE エンジン（Bridge の存在を保証）
        self.bridge: VOSEBridge = VOSEBridge()
        
        # 3. エンジンが利用可能な場合のみ各マネージャーを生成
        self.analyzer: Optional[IntonationAnalyzer] = None
        self.talk_manager: Optional[TalkManager] = None

        if is_engine_available:
            self.analyzer = IntonationAnalyzer()
            self.talk_manager = TalkManager()

        # 4. プレビュー
        self.video_preview = PreviewView()

        self._init_ui()

    # Pyright エラー対策：安全にビデオエンジンを呼ぶためのプロパティ
    @property
    def has_video_engine(self) -> bool:
        return self.video is not None

    # ── UI 構築 ───────────────────────────────────────────────

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_toolbar())

        # 縦スプリッタ: [上部エリア] / [タイムライン]
        v_split = QSplitter(Qt.Orientation.Vertical)

        # 横スプリッタ: [サイドバー] | [プレビュー]
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.addWidget(self._make_sidebar())
        h_split.addWidget(self.video_preview)
        h_split.setStretchFactor(0, 0)
        h_split.setStretchFactor(1, 1)
        h_split.setSizes([220, 1100])

        # タイムラインコンテナ
        tl_frame = QFrame()
        tl_frame.setStyleSheet("background-color: #141416;")
        tl_layout = QVBoxLayout(tl_frame)
        tl_layout.setContentsMargins(0, 0, 0, 0)
        tl_layout.setSpacing(0)

        self.timeline = TimelineWidget()
        # 両トラックの右クリック合成シグナルを接続
        self.timeline.voice_track.synthesize_requested.connect(self._on_synthesize_from_clip)
        self.timeline.video_track.synthesize_requested.connect(self._on_synthesize_from_clip)
        tl_layout.addWidget(self.timeline)

        v_split.addWidget(h_split)
        v_split.addWidget(tl_frame)
        v_split.setStretchFactor(0, 6)
        v_split.setStretchFactor(1, 4)
        v_split.setSizes([580, 220])

        root.addWidget(v_split)

        # ステータスバー
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

        # アプリタイトル
        title = QLabel("VO-SE Cut Studio")
        title.setStyleSheet(
            "color:#ffffff; font-size:14px; font-weight:600; background:transparent;"
        )
        layout.addWidget(title)
        layout.addStretch()

        # トランスポートボタン
        btn_style = """
            QPushButton {
                background: transparent;
                color: #c5c5c7;
                border: none;
                font-size: 16px;
                border-radius: 6px;
                min-width: 32px;
                min-height: 28px;
            }
            QPushButton:hover  { background-color: #3a3a3c; color: #ffffff; }
            QPushButton:pressed { background-color: #2c2c2e; }
        """
        controls = [
            ("⏮", "先頭"), ("⏪", "戻る"), ("▶", "再生"), 
            ("⏩", "進む"), ("⏭", "末尾")
        ]
        for icon, tip in controls:
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn.setStyleSheet(btn_style)
            btn.setFixedSize(32, 28)
            layout.addWidget(btn)

        layout.addStretch()

        # 書き出しボタン
        export_btn = QPushButton("書き出し")
        export_btn.setFixedHeight(28)
        export_btn.setStyleSheet("""
            QPushButton {
                background-color: #0a84ff;
                color: #ffffff;
                border: none;
                border-radius: 7px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover   { background-color: #409cff; }
            QPushButton:pressed { background-color: #0070e0; }
        """)
        layout.addWidget(export_btn)
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

        # ── 素材ライブラリ ─────────────────────────────────
        ly.addWidget(self._section_label("素材ライブラリ"))
        self.asset_list = QListWidget()
        self.asset_list.setFixedHeight(130)
        ly.addWidget(self.asset_list)

        ly.addWidget(self._divider())
        ly.addSpacing(4)

        # ── 音声合成 (TTS) ────────────────────────────────
        ly.addWidget(self._section_label("音声合成 (TTS)"))

        self.tts_input = QTextEdit()
        self.tts_input.setPlaceholderText("テキストを入力…")
        self.tts_input.setFixedHeight(80)
        ly.addWidget(self.tts_input)

        self.generate_button = QPushButton("音声を合成して配置")
        self.generate_button.setFixedHeight(34)
        self.generate_button.setStyleSheet("""
            QPushButton {
                background-color: #0a84ff;
                color: #ffffff;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover   { background-color: #409cff; }
            QPushButton:pressed { background-color: #0070e0; }
            QPushButton:disabled {
                background-color: #1a3356;
                color: #5070a0;
            }
        """)
        self.generate_button.clicked.connect(self._on_generate_clicked)
        ly.addWidget(self.generate_button)

        ly.addStretch()

        # ── エンジンステータス ─────────────────────────────
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

    # ── Slots ─────────────────────────────────────────────────

    def _on_generate_clicked(self) -> None:
        """TTS 入力欄からテキストを合成してタイムラインに配置する"""
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
            start_x, clip_width,
            f"🎙  {short_text}",
            color=QColor(10, 132, 255),
            raw_text=text,
        )
        self.timeline.video_track.add_clip(
            start_x, clip_width,
            f"💬  {short_text}",
            color=QColor(48, 209, 88),
            raw_text=text,
        )

        self.timeline.header.set_playhead(start_x + clip_width)

        # VO-SE レンダリング（エンジンが有効な場合）
        if is_engine_available and self.analyzer:
            notes = generate_talk_events(text, self.analyzer)
            if notes:
                self.bridge.render(notes, output_file="output_rendered.wav")

        self.tts_input.clear()
        self._status.showMessage(f"✅  合成完了: {short_text}")
        self.generate_button.setEnabled(True)
        self.generate_button.setText("音声を合成して配置")

    def _on_synthesize_from_clip(self, text: str, clip_x: int) -> None:
        """タイムラインクリップの右クリック → 音声を合成"""
        if not text:
            return
        if not self.talk_manager:
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
            clip_x, clip_width,
            f"🎙  {short_text}",
            color=QColor(10, 132, 255),
            raw_text=text,
        )
        self.timeline.header.set_playhead(clip_x + clip_width)
        self._status.showMessage(f"✅  クリップ合成完了: {short_text}")


# ══════════════════════════════════════════════════════════════
# エントリーポイント
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # プラットフォーム別フォント設定
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
