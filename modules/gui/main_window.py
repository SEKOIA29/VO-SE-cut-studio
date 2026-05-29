"""
main_window.py — Phase 1 完全実装版
VO-SE Cut Studio

Phase 1 変更点:
  TimelineHeader  : scroll_offset 対応 (ティック・プレイヘッド座標変換)
  TimelineTrack   : scroll_offset 対応 (描画・マウスイベント・クリップ検索)
  TimelineWidget  : スクロールバー同期 / update_scroll_range / scroll_to_playhead
  CutStudioMain   : position_updated → scroll_to_playhead 接続
                    add_clip 後に update_scroll_range() を呼ぶ
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
from PySide6.QtCore import Qt, QRect, QRectF, Signal, Slot
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent,
    QPixmap, QPainterPath, QFontMetrics, QContextMenuEvent, QWheelEvent,
)

from playback_engine import PlaybackEngine, TransportController

_SYS = platform.system()
_FONT_FAMILY = (
    ".AppleSystemUIFont" if _SYS == "Darwin"
    else "Segoe UI"       if _SYS == "Windows"
    else "Inter"
)

# ══════════════════════════════════════════════════════════════════
# VO-SE Engine
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
        def synthesize(self, text: str, output_path: str,
                       speed: float = 1.0) -> Tuple[bool, str]: ...

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
# C++ バインディング
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

    def render(self, notes_list: List[Dict[str, Any]],
               output_file: str = "output.wav") -> None:
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
            
            # 安全のため、基本となる長さを確定 (デフォルトは50点)
            pitch   = list(data.get("pitch", [150.0] * 50))
            length  = len(pitch)
            
            # pitchの長さに他パラメータの長さを強制同期させてSegfaultを絶対防ぐ
            gender  = list(data.get("gender",  [0.5] * length))[:length]
            tension = list(data.get("tension", [0.5] * length))[:length]
            breath  = list(data.get("breath",  [0.1] * length))[:length]
            
            c_wav = phoneme.encode("utf-8")
            c_p   = (ctypes.c_double * length)(*pitch)
            c_g   = (ctypes.c_double * length)(*gender)
            c_t   = (ctypes.c_double * length)(*tension)
            c_b   = (ctypes.c_double * length)(*breath)
            
            # PythonのGC(ゴミ箱ポイ)からポインタを保護する防壁
            self.keep_alive.extend([c_wav, c_p, c_g, c_t, c_b])
            
            c_notes[i].wav_path         = c_wav
            c_notes[i].pitch_length     = length
            c_notes[i].pitch_curve      = c_p
            c_notes[i].gender_curve     = c_g
            c_notes[i].tension_curve    = c_t
            c_notes[i].breath_curve     = c_b
            c_notes[i].offset_ms        = float(data.get("offset", 0.0))
            c_notes[i].consonant_ms     = float(data.get("consonant", 0.0))
            c_notes[i].cutoff_ms        = float(data.get("cutoff", 0.0))
            c_notes[i].pre_utterance_ms = float(data.get("pre_utterance", 0.0))
            c_notes[i].overlap_ms       = float(data.get("overlap", 0.0))
            
        try:
            out_bytes = output_file.encode("utf-8")
            self.lib.execute_render(c_notes, count, out_bytes)
        except Exception as e:
            # E501を確実に回避する改行スタイル
            err_msg = f"❌ execute_render error: {e}\n{traceback.format_exc()}"
            print(err_msg)


# ══════════════════════════════════════════════════════════════════
# Apple Dark Theme
# ══════════════════════════════════════════════════════════════════

APP_STYLE = """
QWidget { background-color: #1c1c1e; color: #ffffff; font-size: 13px; }
QSplitter::handle:horizontal { width:  1px; background-color: #3a3a3c; }
QSplitter::handle:vertical   { height: 1px; background-color: #3a3a3c; }
QFrame  { border: none; }
QLabel  { background: transparent; }
QTextEdit {
    background-color: #2c2c2e; color: #ffffff;
    border: 1px solid #3a3a3c; border-radius: 8px; padding: 8px 10px;
    selection-background-color: #0a84ff;
}
QTextEdit:focus { border: 1.5px solid #0a84ff; }
QPushButton {
    background-color: #3a3a3c; color: #ffffff; border: none;
    border-radius: 8px; padding: 8px 16px; font-size: 13px; font-weight: 500;
}
QPushButton:hover   { background-color: #48484a; }
QPushButton:pressed { background-color: #2c2c2e; }
QListWidget {
    background-color: #2c2c2e; border: 1px solid #3a3a3c;
    border-radius: 10px; color: #ffffff; padding: 4px; outline: none;
}
QListWidget::item          { padding: 7px 10px; border-radius: 6px; margin: 1px 2px; }
QListWidget::item:hover    { background-color: #3a3a3c; }
QListWidget::item:selected { background-color: #0a84ff; }
QScrollBar:horizontal         { height: 6px; background: transparent; border: none; }
QScrollBar::handle:horizontal { background: #48484a; border-radius: 3px; min-width: 40px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar:vertical           { width: 6px; background: transparent; border: none; }
QScrollBar::handle:vertical   { background: #48484a; border-radius: 3px; min-height: 40px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar {
    background-color: #1c1c1e; color: #636366;
    font-size: 11px; border-top: 1px solid #3a3a3c;
}
QMenu {
    background-color: #2c2c2e; border: 1px solid #48484a;
    border-radius: 10px; padding: 5px; color: #ffffff;
}
QMenu::item          { padding: 7px 18px; border-radius: 6px; font-size: 13px; margin: 1px; }
QMenu::item:selected { background-color: #0a84ff; }
QMenu::separator     { height: 1px; background-color: #3a3a3c; margin: 4px 10px; }
QGraphicsView { background-color: #000000; border: none; }
"""


# ══════════════════════════════════════════════════════════════════
# TimelineHeader — Phase 1: scroll_offset 対応
# ══════════════════════════════════════════════════════════════════

class TimelineHeader(QWidget):
    positionChanged = Signal(int)   # タイムライン座標 (px) を emit

    _RED  = QColor(255, 69, 58)
    _TICK = QColor(72, 72, 78)
    _LBL  = QColor(110, 110, 120)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background-color: #111113;")
        self.playhead_x:   int  = 50    # タイムライン座標 (px)
        self.is_dragging:  bool = False
        self.px_per_sec:   int  = 100
        self.scroll_offset: int = 0     # Phase 1

    # ── 描画 ──────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(QFont(_FONT_FAMILY, 8))

        step   = self.px_per_sec
        offset = self.scroll_offset

        # ── ティック (スクロール対応) ────────────────────────
        # 画面左端に来るタイムライン位置から描き始める
        screen_x = -(offset % step)
        while screen_x < self.width():
            tl_px = screen_x + offset          # タイムライン px
            sec   = int(tl_px) // step

            if screen_x >= 0:
                p.setPen(QPen(self._TICK, 1))
                p.drawLine(screen_x, 16, screen_x, 28)
                p.setPen(self._LBL)
                p.drawText(screen_x + 4, 13,
                           f"{sec // 60:d}:{sec % 60:02d}")

            hx = screen_x + step // 2
            if 0 <= hx < self.width():
                p.setPen(QPen(QColor(50, 50, 56), 1))
                p.drawLine(hx, 22, hx, 28)

            screen_x += step

        # ── プレイヘッド (タイムライン → スクリーン変換) ────
        spx = float(self.playhead_x - offset)
        if -10 <= spx <= self.width() + 10:
            p.setPen(QPen(self._RED, 2))
            p.drawLine(int(spx), 0, int(spx), 28)
            path = QPainterPath()
            path.moveTo(spx - 5, 0)
            path.lineTo(spx + 5, 0)
            path.lineTo(spx,     9)
            path.closeSubpath()
            p.setBrush(QBrush(self._RED))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        p.end()

    # ── マウスイベント ────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self._update_from_screen(int(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self._update_from_screen(int(event.position().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self.is_dragging = False
            if hasattr(self, "_on_seek_from_header"):
                self._on_seek_from_header(self.playhead_x / self.px_per_sec)

    def _update_from_screen(self, sx: int) -> None:
        """スクリーン x → タイムライン px に変換して保存"""
        tl_x = max(0, sx + self.scroll_offset)
        self.playhead_x = tl_x
        self.update()
        self.positionChanged.emit(tl_x)

    # ── API ───────────────────────────────────────────────────

    def set_playhead(self, timeline_px: int) -> None:
        """タイムライン座標 (px) でプレイヘッドを設定"""
        self.playhead_x = max(0, timeline_px)
        self.update()

    @Slot(int)
    def set_scroll_offset(self, offset: int) -> None:
        self.scroll_offset = max(0, offset)
        self.update()


# ══════════════════════════════════════════════════════════════════
# TimelineTrack — Phase 1: scroll_offset 対応
# ══════════════════════════════════════════════════════════════════

class TimelineTrack(QFrame):
    synthesize_requested = Signal(str, int)
    HEADER_W = 110

    def __init__(self, name: str,
                 color: QColor = QColor(10, 132, 255),
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
        self.scroll_offset: int = 0     # Phase 1

    # ── Phase 1 API ───────────────────────────────────────────

    @Slot(int)
    def set_scroll_offset(self, offset: int) -> None:
        self.scroll_offset = max(0, offset)
        self.update()

    def content_right_edge(self) -> int:
        """全クリップの右端最大値 (タイムライン px)"""
        if not self.clips:
            return 0
        return max(c["x"] + c["width"] for c in self.clips)

    # ── クリップ追加 ──────────────────────────────────────────

    def add_clip(self, x: int, width: int, text: str,
                 color: Optional[QColor] = None,
                 raw_text: str = "") -> None:
        self.clips.append({
            "x":        x,
            "width":    max(4, width),
            "text":     text,
            "raw_text": raw_text or text,
            "color":    color or self.track_color,
        })
        self.update()

    # ── イベント ──────────────────────────────────────────────

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        tl_x = event.pos().x() - self.HEADER_W + self.scroll_offset
        clip = self._clip_at(tl_x)
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
        tl_x = event.position().x() - self.HEADER_W + self.scroll_offset
        for i, clip in enumerate(reversed(self.clips)):
            idx = len(self.clips) - 1 - i
            if clip["x"] <= tl_x <= clip["x"] + clip["width"]:
                self.dragging_clip_idx = idx
                self.drag_start_offset = int(tl_x - clip["x"])
                break

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.dragging_clip_idx is not None:
            tl_x  = event.position().x() - self.HEADER_W + self.scroll_offset
            new_x = tl_x - self.drag_start_offset
            self.clips[self.dragging_clip_idx]["x"] = max(0, int(new_x))
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.dragging_clip_idx = None

    # ── 描画 ──────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor(20, 20, 22))
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(0, h - 1, w, h - 1)

        # ヘッダー
        p.fillRect(0, 0, self.HEADER_W, h, QColor(26, 26, 28))
        p.fillRect(0, 0, 3, h, self.track_color)
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(self.HEADER_W, 0, self.HEADER_W, h)
        p.setFont(QFont(_FONT_FAMILY, 10, QFont.Weight.Medium))
        p.setPen(QColor(190, 190, 198))
        p.drawText(QRect(12, 0, self.HEADER_W - 14, h),
                   Qt.AlignmentFlag.AlignVCenter, self.track_name)

        # クリップ (ヘッダー右側のみにクリッピング)
        p.setClipRect(self.HEADER_W, 0, w - self.HEADER_W, h)
        for clip in self.clips:
            self._paint_clip(p, clip, h)
        p.setClipping(False)
        p.end()

    def _paint_clip(self, p: QPainter, clip: Dict[str, Any],
                    track_h: int) -> None:
        CX = float(self.HEADER_W + clip["x"] - self.scroll_offset)
        CY, CW, CH = 7.0, float(clip["width"]), float(track_h - 14)
        c: QColor = clip["color"]

        # 可視範囲外はスキップ
        if CX + CW < self.HEADER_W or CX > self.width():
            return

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
            elided    = QFontMetrics(font).elidedText(
                clip["text"], Qt.TextElideMode.ElideRight, text_rect.width())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)

    def _clip_at(self, tl_x: float) -> Optional[Dict[str, Any]]:
        """タイムライン座標でクリップを検索"""
        for clip in reversed(self.clips):
            if clip["x"] <= tl_x <= clip["x"] + clip["width"]:
                return clip
        return None


# ══════════════════════════════════════════════════════════════════
# TimelineWidget — Phase 1: スクロール同期・自動追従
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
            QScrollBar::handle:horizontal {
                background: #3a3a3c; border-radius: 4px; min-width: 40px;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal { width: 0; }
        """)
        self.h_scrollbar.setMinimum(0)
        self.h_scrollbar.setMaximum(0)
        self.h_scrollbar.setSingleStep(20)

        # Phase 1: スクロールバー → 全ウィジェットに伝播
        self.h_scrollbar.valueChanged.connect(self._on_scroll)

        layout.addWidget(self.h_scrollbar)

    # ── Phase 1: スクロール API ───────────────────────────────

    @Slot(int)
    def _on_scroll(self, value: int) -> None:
        self.header.set_scroll_offset(value)
        self.voice_track.set_scroll_offset(value)
        self.video_track.set_scroll_offset(value)

    def update_scroll_range(self) -> None:
        """
        クリップ右端に基づいてスクロールバーの範囲を更新する。
        クリップを追加・削除した後に呼ぶこと。
        """
        right_edge = max(
            self.voice_track.content_right_edge(),
            self.video_track.content_right_edge(),
            0,
        )
        visible_w  = max(1, self.voice_track.width() - TimelineTrack.HEADER_W)
        max_scroll = max(0, right_edge + 200 - visible_w)

        self.h_scrollbar.setPageStep(visible_w)
        self.h_scrollbar.setMaximum(max_scroll)
        if self.h_scrollbar.value() > max_scroll:
            self.h_scrollbar.setValue(max_scroll)

    def scroll_to_playhead(self, timeline_px: int) -> None:
        """
        プレイヘッド (タイムライン px) が画面外に出たとき追従スクロール。
        PlaybackEngine.position_updated から呼ばれる。
        """
        visible_w      = max(1, self.voice_track.width() - TimelineTrack.HEADER_W)
        current_scroll = self.h_scrollbar.value()
        LOOKAHEAD      = 80     # 右端より何 px 手前で追従を開始するか

        if timeline_px < current_scroll:
            # 左へシーク
            self.h_scrollbar.setValue(max(0, timeline_px - 50))
        elif timeline_px > current_scroll + visible_w - LOOKAHEAD:
            # 右へはみ出しそう
            new_val = min(
                self.h_scrollbar.maximum(),
                timeline_px - visible_w + LOOKAHEAD + 50,
            )
            self.h_scrollbar.setValue(new_val)


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
# CutStudioMain — Phase 1 完全統合版
# ══════════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio")
        self.resize(1440, 900)
        self.setMinimumSize(960, 600)

        self.video: Optional[video_engine.VideoEngine] = None
        try:
            self.video = video_engine.VideoEngine()
        except Exception as e:
            print(f"⚠️ VideoEngine Initialization Failed: {e}")

        self.bridge: VOSEBridge = VOSEBridge()
        self.analyzer: Optional[IntonationAnalyzer]    = None
        self.talk_manager: Optional[TalkManager]       = None
        if is_engine_available:
            self.analyzer     = IntonationAnalyzer()
            self.talk_manager = TalkManager()

        self.video_preview = PreviewView()
        self._transport_btns: List[QPushButton] = []
        self._export_btn: Optional[QPushButton] = None

        self._init_ui()

        # ── PlaybackEngine ─────────────────────────────────────
        self.playback_engine = PlaybackEngine(
            preview_view    = self.video_preview,
            timeline_header = self.timeline.header,
            status_bar      = self._status,
        )

        # Phase 1: 再生位置更新 → タイムライン自動追従
        self.playback_engine.position_updated.connect(
            lambda sec: self.timeline.scroll_to_playhead(
                int(sec * self.timeline.header.px_per_sec)
            )
        )

        # ヘッダードラッグシーク
        self.timeline.header._on_seek_from_header = (  # type: ignore[attr-defined]
            lambda sec: self.playback_engine.seek(sec)
        )

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
        self.timeline.voice_track.synthesize_requested.connect(
            self._on_synthesize_from_clip)
        self.timeline.video_track.synthesize_requested.connect(
            self._on_synthesize_from_clip)
        tl_layout.addWidget(self.timeline)

        v_split.addWidget(h_split)
        v_split.addWidget(tl_frame)
        v_split.setStretchFactor(0, 6)
        v_split.setStretchFactor(1, 4)
        v_split.setSizes([580, 220])

        root.addWidget(v_split)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("VO-SE Cut Studio  —  Phase 1 完了")

    def _make_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setObjectName("toolbar")
        bar.setStyleSheet(
            "QFrame#toolbar { background-color: #232325; border-bottom: 1px solid #3a3a3c; }"
        )
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
                font-size: 16px; border-radius: 6px; min-width: 32px; min-height: 28px;
            }
            QPushButton:hover   { background-color: #3a3a3c; color: #ffffff; }
            QPushButton:pressed { background-color: #2c2c2e; }
        """
        for icon, tip in [("⏮","先頭へ"),("⏪","5秒戻る"),("▶","再生"),("⏩","5秒進む"),("⏭","末尾へ")]:
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
                border-radius: 7px; padding: 0 18px; font-size: 13px; font-weight: 500;
            }
            QPushButton:hover   { background-color: #409cff; }
            QPushButton:pressed { background-color: #0070e0; }
        """)
        layout.addWidget(export_btn)
        self._export_btn = export_btn
        return bar

    def _make_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setObjectName("sidebar")
        sidebar.setStyleSheet(
            "QFrame#sidebar { background-color: #1c1c1e; border-right: 1px solid #3a3a3c; }"
        )
        ly = QVBoxLayout(sidebar)
        ly.setContentsMargins(12, 16, 12, 16)
        ly.setSpacing(8)

        ly.addWidget(self._section_label("素材ライブラリ"))
        self.asset_list = QListWidget()
        self.asset_list.setFixedHeight(130)
        self.asset_list.setToolTip("ダブルクリックで再生")
        self.asset_list.itemDoubleClicked.connect(self._on_asset_double_clicked)
        ly.addWidget(self.asset_list)

        add_btn = QPushButton("＋  ファイルを追加")
        add_btn.setFixedHeight(28)
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2e; color: #ebebf5;
                border: 1px solid #3a3a3c; border-radius: 7px; font-size: 12px;
            }
            QPushButton:hover   { background-color: #3a3a3c; }
            QPushButton:pressed { background-color: #1c1c1e; }
        """)
        add_btn.clicked.connect(self._on_add_asset)
        ly.addWidget(add_btn)

        ly.addWidget(self._divider())
        ly.addSpacing(4)

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

    # ── Slots ──────────────────────────────────────────────────

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

        # Phase 1: クリップ追加後にスクロール範囲更新 → 追従
        self.timeline.update_scroll_range()
        self.timeline.scroll_to_playhead(start_x + clip_width)

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
        self.timeline.update_scroll_range()
        self.timeline.scroll_to_playhead(clip_x + clip_width)
        self._status.showMessage(f"✅  クリップ合成完了: {short_text}")

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
            edl_entries = [
                {"in":  c["x"] / 100.0,
                 "out": (c["x"] + c["width"]) / 100.0,
                 "enabled": True}
                for c in self.timeline.voice_track.clips
            ]
            ok  = self.video.export_hw(json.dumps(edl_entries), path, quality=23)
            msg = f"✅  書き出し完了: {path}" if ok else "❌  書き出しに失敗しました"
        else:
            msg = "⚠️  VideoEngine が利用できません"
        self._status.showMessage(msg)

    def _on_add_asset(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "素材ファイルを追加", "",
            "動画・音声 (*.mp4 *.mov *.mkv *.avi *.wav *.mp3 *.aac *.flac)"
        )
        for path in paths:
            name = os.path.basename(path)
            icon = "🔊" if path.lower().endswith(
                (".wav", ".mp3", ".aac", ".flac")) else "🎬"
            item = QListWidgetItem(f"{icon}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.asset_list.addItem(item)

    def _on_asset_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        ok = self.playback_engine.load(path)
        if ok and self.has_video_engine and self.video:
            self.video.load_video(path)

    def closeEvent(self, event) -> None:  # type: ignore[override]
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
