"""
main_window.py — VO-SE Cut Studio  Phase 2 完全実装版
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 2 変更点 (全面リライト):
  ■ クリップ座標を「秒」ベースに統一 (px_per_sec に依存しない)
  ■ Undo / Redo  (QUndoStack + QUndoCommand)
  ■ 波形表示 (VideoEngine.extract_waveform 接続)
  ■ クリップトリミング (左右端ドラッグ)
  ■ スナップ (クリップ端・再生ヘッドへの磁力吸着)
  ■ タイムラインズーム (Ctrl+ホイール / ±ボタン)
  ■ プロジェクト保存・読み込み (JSON)
  ■ マルチトラック (トラック追加/削除)
  ■ EDL → VideoEngine に px_per_sec を渡して正確な書き出し
  ■ OS 別ライブラリパス自動解決 (video_engine.py 委譲)
  ■ キーボードショートカット (Space=再生, Z=Undo, Ctrl+S=保存 …)
"""
from __future__ import annotations

import json
import os
import platform
import sys
import traceback
import wave
import ctypes
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import video_engine as _ve_mod

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTextEdit, QPushButton, QLabel, QListWidget, QListWidgetItem,
    QFrame, QGraphicsView, QGraphicsScene, QScrollBar, QGraphicsPixmapItem,
    QStatusBar, QMenu, QFileDialog, QUndoStack, QUndoCommand,
    QSlider, QSizePolicy, QScrollArea, QToolBar, QInputDialog,
)
from PySide6.QtCore import (
    Qt, QRect, QRectF, Signal, Slot, QTimer, QPointF,
)
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent,
    QPixmap, QPainterPath, QFontMetrics, QContextMenuEvent, QWheelEvent,
    QKeySequence, QAction, QShortcut, QLinearGradient,
)

from playback_engine import PlaybackEngine, TransportController

_SYS = platform.system()
_FONT_FAMILY = (
    ".AppleSystemUIFont" if _SYS == "Darwin"
    else "Segoe UI"       if _SYS == "Windows"
    else "Inter"
)

# ══════════════════════════════════════════════════════════════════
# VO-SE Engine (フォールバック付き)
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
        text: str, analyzer: "IntonationAnalyzer"
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

        def generate_talk_events(*args: Any, **kwargs: Any) -> list:
            return []

        class IntonationAnalyzer:  # type: ignore[no-redef]
            pass

        class TalkManager:  # type: ignore[no-redef]
            pass


def get_wav_duration_sec(file_path: str) -> float:
    """WAVファイルの長さを秒で返す。失敗時は 2.0 秒。"""
    if not os.path.exists(file_path):
        return 2.0
    try:
        with wave.open(file_path, "rb") as wr:
            return wr.getnframes() / float(wr.getframerate())
    except Exception as e:
        print(f"WAV parse error: {e}")
        return 2.0


# ══════════════════════════════════════════════════════════════════
# C++ バインディング (VOSEBridge)
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
        ext      = ".dylib" if is_mac else (".dll" if _SYS == "Windows" else ".so")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        lib_path = os.path.join(base_dir, "bin", f"libvo_se_cut{ext}")
        if not os.path.exists(lib_path):
            print(f"⚠️  Bridge not found: {lib_path}")
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
            print(f"✅  VO-SE Bridge loaded: {lib_path}")
        except Exception as e:
            print(f"❌  Bridge load failed: {e}\n{traceback.format_exc()}")
            self.lib = None

    def render(self, notes_list: List[Dict[str, Any]],
               output_file: str = "output.wav") -> None:
        if not self.lib or not notes_list:
            return
        count      = len(notes_list)
        NotesArray = NoteEvent * count
        c_notes    = NotesArray()
        self.keep_alive = []

        for i, data in enumerate(notes_list):
            phoneme = str(data.get("phoneme", "a"))
            pitch   = list(data.get("pitch", [150.0] * 50))
            length  = len(pitch)
            gender  = list(data.get("gender",  [0.5] * length))[:length]
            tension = list(data.get("tension", [0.5] * length))[:length]
            breath  = list(data.get("breath",  [0.1] * length))[:length]

            c_wav = phoneme.encode("utf-8")
            c_p   = (ctypes.c_double * length)(*pitch)
            c_g   = (ctypes.c_double * length)(*gender)
            c_t   = (ctypes.c_double * length)(*tension)
            c_b   = (ctypes.c_double * length)(*breath)
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
            self.lib.execute_render(c_notes, count, output_file.encode("utf-8"))
        except Exception as e:
            print(f"❌  execute_render error: {e}\n{traceback.format_exc()}")


# ══════════════════════════════════════════════════════════════════
# Dark Theme
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
QSlider::groove:horizontal {
    height: 3px; background: #3a3a3c; border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 12px; height: 12px; margin: -5px 0;
    background: #ffffff; border-radius: 6px;
}
QSlider::sub-page:horizontal { background: #0a84ff; border-radius: 2px; }
"""

# ══════════════════════════════════════════════════════════════════
# Undo コマンド群
# ══════════════════════════════════════════════════════════════════

class AddClipCmd(QUndoCommand):
    def __init__(self, track: "TimelineTrack", clip: Dict[str, Any]) -> None:
        super().__init__(f"クリップ追加: {clip.get('text','')}")
        self._track = track
        self._clip  = clip

    def redo(self) -> None:
        self._track.clips.append(self._clip)
        self._track.update()

    def undo(self) -> None:
        if self._clip in self._track.clips:
            self._track.clips.remove(self._clip)
        self._track.update()


class RemoveClipCmd(QUndoCommand):
    def __init__(self, track: "TimelineTrack", clip: Dict[str, Any]) -> None:
        super().__init__(f"クリップ削除: {clip.get('text','')}")
        self._track = track
        self._clip  = clip

    def redo(self) -> None:
        if self._clip in self._track.clips:
            self._track.clips.remove(self._clip)
        self._track.update()

    def undo(self) -> None:
        self._track.clips.append(self._clip)
        self._track.update()


class MoveClipCmd(QUndoCommand):
    def __init__(self, track: "TimelineTrack", clip: Dict[str, Any],
                 old_start: float, new_start: float) -> None:
        super().__init__(f"クリップ移動: {clip.get('text','')}")
        self._track     = track
        self._clip      = clip
        self._old_start = old_start
        self._new_start = new_start

    def redo(self) -> None:
        self._clip["start"] = self._new_start
        self._track.update()

    def undo(self) -> None:
        self._clip["start"] = self._old_start
        self._track.update()


class TrimClipCmd(QUndoCommand):
    def __init__(self, track: "TimelineTrack", clip: Dict[str, Any],
                 old_start: float, old_dur: float,
                 new_start: float, new_dur: float) -> None:
        super().__init__(f"トリム: {clip.get('text','')}")
        self._track     = track
        self._clip      = clip
        self._old_start = old_start
        self._old_dur   = old_dur
        self._new_start = new_start
        self._new_dur   = new_dur

    def redo(self) -> None:
        self._clip["start"]    = self._new_start
        self._clip["duration"] = self._new_dur
        self._track.update()

    def undo(self) -> None:
        self._clip["start"]    = self._old_start
        self._clip["duration"] = self._old_dur
        self._track.update()


# ══════════════════════════════════════════════════════════════════
# TimelineHeader — スクロール + ズーム + スナップ対応
# ══════════════════════════════════════════════════════════════════

class TimelineHeader(QWidget):
    """
    タイムラインルーラー。
    内部座標はすべて「秒」。px_per_sec でスクリーン変換。
    """
    positionChanged = Signal(float)   # 秒を emit

    _RED  = QColor(255, 69, 58)
    _TICK = QColor(72, 72, 78)
    _LBL  = QColor(110, 110, 120)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background-color: #111113;")
        self.playhead_sec:  float = 0.0
        self.is_dragging:   bool  = False
        self.px_per_sec:    float = 100.0
        self.scroll_offset: float = 0.0   # 秒単位
        self._on_seek_from_header: Optional[Any] = None

    # ── 座標変換 ─────────────────────────────────────────────────

    def sec_to_screen(self, sec: float) -> float:
        return (sec - self.scroll_offset) * self.px_per_sec

    def screen_to_sec(self, sx: float) -> float:
        return sx / self.px_per_sec + self.scroll_offset

    # ── 描画 ──────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(QFont(_FONT_FAMILY, 8))

        # 適切なティック間隔を自動選択 (ズームに追従)
        tick_intervals = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
        min_px_between = 50
        tick_sec = 1.0
        for iv in tick_intervals:
            if iv * self.px_per_sec >= min_px_between:
                tick_sec = iv
                break

        visible_start = self.scroll_offset
        visible_end   = self.scroll_offset + self.width() / self.px_per_sec

        import math
        first_tick = math.floor(visible_start / tick_sec) * tick_sec
        t = first_tick
        while t <= visible_end + tick_sec:
            sx = self.sec_to_screen(t)
            if 0 <= sx <= self.width():
                p.setPen(QPen(self._TICK, 1))
                p.drawLine(int(sx), 16, int(sx), 28)
                m   = int(t) // 60
                s   = t % 60
                lbl = f"{m}:{s:04.1f}" if tick_sec < 1.0 else f"{m}:{int(s):02d}"
                p.setPen(self._LBL)
                p.drawText(int(sx) + 3, 13, lbl)
            # 半ティック
            half = t + tick_sec * 0.5
            hx   = self.sec_to_screen(half)
            if 0 <= hx <= self.width():
                p.setPen(QPen(QColor(50, 50, 56), 1))
                p.drawLine(int(hx), 22, int(hx), 28)
            t += tick_sec

        # プレイヘッド
        spx = self.sec_to_screen(self.playhead_sec)
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

    # ── マウスイベント ────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self._update_from_screen(event.position().x())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self._update_from_screen(event.position().x())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self.is_dragging = False
            if self._on_seek_from_header:
                self._on_seek_from_header(self.playhead_sec)

    def _update_from_screen(self, sx: float) -> None:
        sec = max(0.0, self.screen_to_sec(sx))
        self.playhead_sec = sec
        self.update()
        self.positionChanged.emit(sec)

    # ── API ───────────────────────────────────────────────────────

    def set_playhead(self, sec: float) -> None:
        self.playhead_sec = max(0.0, sec)
        self.update()

    @Slot(float)
    def set_scroll_offset_sec(self, sec: float) -> None:
        self.scroll_offset = max(0.0, sec)
        self.update()

    def set_px_per_sec(self, pps: float) -> None:
        self.px_per_sec = max(10.0, min(2000.0, pps))
        self.update()


# ══════════════════════════════════════════════════════════════════
# TimelineTrack — 秒ベース / 波形 / トリミング / スナップ
# ══════════════════════════════════════════════════════════════════

_TRIM_HIT = 6   # トリムハンドルの判定幅 (px)

class TimelineTrack(QFrame):
    """
    クリップスキーマ (すべて秒):
        start    : float  開始秒
        duration : float  長さ秒
        text     : str    表示ラベル
        raw_text : str    TTS 元テキスト
        color    : QColor
        wav_path : str    波形元 WAV (optional)
        waveform : List[float]  peaks_max キャッシュ
    """
    synthesize_requested = Signal(str, float)  # (text, start_sec)
    clip_changed         = Signal()            # Undo/Redo 後に親へ通知
    HEADER_W = 110

    def __init__(self, name: str,
                 color: QColor = QColor(10, 132, 255),
                 undo_stack: Optional[QUndoStack] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(68)
        self.setStyleSheet("")
        self.track_name    = name
        self.track_color   = color
        self.undo_stack    = undo_stack
        self.clips: List[Dict[str, Any]] = []
        self.px_per_sec:    float = 100.0
        self.scroll_offset: float = 0.0   # 秒

        # ドラッグ状態
        self._drag_clip:     Optional[Dict[str, Any]] = None
        self._drag_mode:     str  = ""    # "move" | "trim_l" | "trim_r"
        self._drag_start_x:  float = 0.0
        self._drag_old_start:float = 0.0
        self._drag_old_dur:  float = 0.0
        self._snap_sec:      Optional[float] = None  # スナップ候補

        self.setMouseTracking(True)

    # ── 座標変換 ─────────────────────────────────────────────────

    def sec_to_screen(self, sec: float) -> float:
        return self.HEADER_W + (sec - self.scroll_offset) * self.px_per_sec

    def screen_to_sec(self, sx: float) -> float:
        return (sx - self.HEADER_W) / self.px_per_sec + self.scroll_offset

    def content_right_edge_sec(self) -> float:
        if not self.clips:
            return 0.0
        return max(c["start"] + c["duration"] for c in self.clips)

    # ── API ───────────────────────────────────────────────────────

    def set_px_per_sec(self, pps: float) -> None:
        self.px_per_sec = max(10.0, min(2000.0, pps))
        self.update()

    @Slot(float)
    def set_scroll_offset_sec(self, sec: float) -> None:
        self.scroll_offset = max(0.0, sec)
        self.update()

    def add_clip(self, start: float, duration: float, text: str,
                 color: Optional[QColor] = None, raw_text: str = "",
                 wav_path: str = "",
                 waveform: Optional[List[float]] = None) -> Dict[str, Any]:
        clip: Dict[str, Any] = {
            "start":    max(0.0, start),
            "duration": max(0.01, duration),
            "text":     text,
            "raw_text": raw_text or text,
            "color":    color or self.track_color,
            "wav_path": wav_path,
            "waveform": waveform or [],
        }
        if self.undo_stack:
            self.undo_stack.push(AddClipCmd(self, clip))
        else:
            self.clips.append(clip)
            self.update()
        return clip

    # ── スナップ ─────────────────────────────────────────────────

    def _snap(self, sec: float, snap_targets: List[float],
              threshold_px: float = 8.0) -> float:
        thresh = threshold_px / self.px_per_sec
        best   = sec
        best_d = thresh
        for t in snap_targets:
            d = abs(sec - t)
            if d < best_d:
                best_d = d
                best   = t
        return best

    def _all_clip_edges(self) -> List[float]:
        edges: List[float] = []
        for c in self.clips:
            edges.append(c["start"])
            edges.append(c["start"] + c["duration"])
        return edges

    # ── マウス ───────────────────────────────────────────────────

    def _hit_test(self, sx: float, clip: Dict[str, Any]
                  ) -> str:
        """'trim_l' / 'trim_r' / 'move' / '' を返す"""
        cl = self.sec_to_screen(clip["start"])
        cr = self.sec_to_screen(clip["start"] + clip["duration"])
        if sx < cl or sx > cr:
            return ""
        if sx - cl <= _TRIM_HIT:
            return "trim_l"
        if cr - sx <= _TRIM_HIT:
            return "trim_r"
        return "move"

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        sx = event.position().x()
        for clip in reversed(self.clips):
            mode = self._hit_test(sx, clip)
            if mode:
                self._drag_clip      = clip
                self._drag_mode      = mode
                self._drag_start_x   = sx
                self._drag_old_start = clip["start"]
                self._drag_old_dur   = clip["duration"]
                break

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        sx = event.position().x()

        # カーソル形状
        if not self._drag_clip:
            for clip in reversed(self.clips):
                mode = self._hit_test(sx, clip)
                if mode in ("trim_l", "trim_r"):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                    return
                if mode == "move":
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                    return
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        # ドラッグ中
        delta_sec = (sx - self._drag_start_x) / self.px_per_sec
        snap_tgts = self._all_clip_edges()
        clip      = self._drag_clip

        if self._drag_mode == "move":
            raw    = self._drag_old_start + delta_sec
            snapped = self._snap(raw, snap_tgts)
            clip["start"] = max(0.0, snapped)

        elif self._drag_mode == "trim_l":
            raw_start  = self._drag_old_start + delta_sec
            raw_start  = self._snap(raw_start, snap_tgts)
            max_start  = self._drag_old_start + self._drag_old_dur - 0.05
            clip["start"]    = max(0.0, min(raw_start, max_start))
            consumed         = clip["start"] - self._drag_old_start
            clip["duration"] = self._drag_old_dur - consumed

        elif self._drag_mode == "trim_r":
            raw_end = self._drag_old_start + self._drag_old_dur + delta_sec
            raw_end = self._snap(raw_end, snap_tgts)
            new_dur = raw_end - clip["start"]
            clip["duration"] = max(0.05, new_dur)

        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_clip and self.undo_stack:
            clip = self._drag_clip
            if self._drag_mode == "move":
                if abs(clip["start"] - self._drag_old_start) > 0.001:
                    # MoveClipCmd はリアルタイム変更後に記録 (redo は no-op)
                    cmd = MoveClipCmd(self, clip,
                                      self._drag_old_start, clip["start"])
                    # すでに適用済みなので redo を空実装化して push
                    self.undo_stack.push(cmd)
            elif self._drag_mode in ("trim_l", "trim_r"):
                if (abs(clip["start"]    - self._drag_old_start) > 0.001 or
                        abs(clip["duration"] - self._drag_old_dur)   > 0.001):
                    cmd = TrimClipCmd(
                        self, clip,
                        self._drag_old_start, self._drag_old_dur,
                        clip["start"],        clip["duration"],
                    )
                    self.undo_stack.push(cmd)
            self.clip_changed.emit()
        self._drag_clip = None
        self._drag_mode = ""

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        sx   = event.pos().x()
        clip = None
        for c in reversed(self.clips):
            if self._hit_test(sx, c):
                clip = c
                break
        if not clip:
            return
        menu      = QMenu(self)
        synth_act = menu.addAction("🎙️  音声を合成")
        menu.addSeparator()
        del_act   = menu.addAction("🗑️  削除")
        chosen    = menu.exec(event.globalPos())
        if chosen == synth_act:
            self.synthesize_requested.emit(clip["raw_text"], clip["start"])
        elif chosen == del_act:
            if self.undo_stack:
                self.undo_stack.push(RemoveClipCmd(self, clip))
            else:
                self.clips.remove(clip)
                self.update()
            self.clip_changed.emit()

    # ── 描画 ──────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor(20, 20, 22))
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(0, h - 1, w, h - 1)

        # ── ヘッダー ──
        p.fillRect(0, 0, self.HEADER_W, h, QColor(26, 26, 28))
        p.fillRect(0, 0, 3, h, self.track_color)
        p.setPen(QPen(QColor(46, 46, 50), 1))
        p.drawLine(self.HEADER_W, 0, self.HEADER_W, h)
        p.setFont(QFont(_FONT_FAMILY, 10, QFont.Weight.Medium))
        p.setPen(QColor(190, 190, 198))
        p.drawText(QRect(12, 0, self.HEADER_W - 14, h),
                   Qt.AlignmentFlag.AlignVCenter, self.track_name)

        # ── クリップ ──
        p.setClipRect(self.HEADER_W, 0, w - self.HEADER_W, h)
        for clip in self.clips:
            self._paint_clip(p, clip, h)
        p.setClipping(False)
        p.end()

    def _paint_clip(self, p: QPainter, clip: Dict[str, Any], track_h: int) -> None:
        CX = self.sec_to_screen(clip["start"])
        CW = clip["duration"] * self.px_per_sec
        CY, CH = 6.0, float(track_h - 12)

        if CX + CW < self.HEADER_W or CX > self.width():
            return

        c: QColor = clip["color"]
        rect = QRectF(CX, CY, CW, CH)

        fill = QColor(c)
        fill.setAlpha(175)
        p.setBrush(QBrush(fill))
        border = QColor(c).lighter(145)
        border.setAlpha(190)
        p.setPen(QPen(border, 0.75))
        p.drawRoundedRect(rect, 5.0, 5.0)

        # 波形
        wf: List[float] = clip.get("waveform", [])
        if wf and CW > 8:
            self._paint_waveform(p, wf, CX, CY, CW, CH, c)
        else:
            # 上部グロス (Appleの立体感を出すグラデーション層)
            p.setBrush(QBrush(QColor(255, 255, 255, 26)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(CX + 1, CY + 1, CW - 2, (CH - 2) * 0.35), 4.0, 4.0)

        # テキスト
        if CW > 24:
            font = QFont(_FONT_FAMILY, 9, QFont.Weight.Medium)
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 220))
            trect = QRect(int(CX) + 7, int(CY), int(CW) - 14, int(CH))
            elided = QFontMetrics(font).elidedText(
                clip["text"], Qt.TextElideMode.ElideRight, trect.width())
            p.drawText(trect, Qt.AlignmentFlag.AlignVCenter, elided)

        # トリムハンドル
        p.setBrush(QBrush(QColor(255, 255, 255, 60)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(CX,          CY + 6, 4, CH - 12), 2.0, 2.0)
        p.drawRoundedRect(QRectF(CX + CW - 4, CY + 6, 4, CH - 12), 2.0, 2.0)

    def _paint_waveform(self, p: QPainter, wf: List[float],
                        cx: float, cy: float, cw: float, ch: float,
                        base_color: QColor) -> None:
        n    = len(wf)
        mid  = cy + ch * 0.5
        half = ch * 0.38

        wave_color = QColor(base_color).lighter(160)
        wave_color.setAlpha(160)
        p.setPen(QPen(wave_color, 1.0))

        for i in range(int(cw)):
            fi  = (i / cw) * n
            idx = int(fi)
            if idx >= n:
                break
            amp = wf[idx]
            sx  = cx + i
            if sx < self.HEADER_W:
                continue
            top = mid - amp * half
            bot = mid + amp * half
            p.drawLine(int(sx), int(top), int(sx), int(bot))


# ══════════════════════════════════════════════════════════════════
# TimelineWidget — ズーム・スクロール・マルチトラック統合
# ══════════════════════════════════════════════════════════════════

class TimelineWidget(QWidget):
    """
    複数 TimelineTrack + ルーラー + スクロールバー を束ねるコンテナ。
    スクロールは「秒」で統一。px_per_sec で全トラックに伝播。
    """

    PPS_MIN  =  10.0
    PPS_MAX  = 800.0
    PPS_DEF  = 100.0

    def __init__(self, undo_stack: QUndoStack,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #141416;")
        self.undo_stack  = undo_stack
        self.px_per_sec  = self.PPS_DEF
        self._tracks: List[TimelineTrack] = []
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = TimelineHeader()
        layout.addWidget(self.header)

        # トラックコンテナ (スクロール可)
        self._track_container = QWidget()
        self._track_container.setStyleSheet("background-color: #141416;")
        self._track_layout = QVBoxLayout(self._track_container)
        self._track_layout.setContentsMargins(0, 0, 0, 0)
        self._track_layout.setSpacing(0)

        self.voice_track = self._make_track("🎙  VOICE", QColor(10, 132, 255))
        self.video_track = self._make_track("🎬  VIDEO", QColor(48, 209, 88))

        self._track_layout.addStretch()
        layout.addWidget(self._track_container)

        # ズームバー
        zoom_row = QWidget()
        zoom_row.setStyleSheet("background:#0d0d0f;")
        zl = QHBoxLayout(zoom_row)
        zl.setContentsMargins(8, 2, 8, 2)

        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedSize(22, 22)
        zoom_out_btn.setStyleSheet(
            "QPushButton{background:#2c2c2e;color:#fff;border:none;"
            "border-radius:4px;font-size:14px;}"
            "QPushButton:hover{background:#3a3a3c;}"
        )
        zoom_out_btn.clicked.connect(lambda: self.zoom(-0.25))

        zoom_in_btn = QPushButton("＋")
        zoom_in_btn.setFixedSize(22, 22)
        zoom_in_btn.setStyleSheet(zoom_out_btn.styleSheet())
        zoom_in_btn.clicked.connect(lambda: self.zoom(0.25))

        self._zoom_label = QLabel(f"{int(self.px_per_sec)}px/s")
        self._zoom_label.setStyleSheet("color:#636366;font-size:10px;")

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
        self.h_scrollbar.setSingleStep(1)
        self.h_scrollbar.valueChanged.connect(self._on_hscroll)

        zl.addWidget(zoom_out_btn)
        zl.addWidget(self._zoom_label)
        zl.addWidget(zoom_in_btn)
        zl.addSpacing(8)
        zl.addWidget(self.h_scrollbar, 1)
        layout.addWidget(zoom_row)

    def _make_track(self, name: str, color: QColor) -> TimelineTrack:
        track = TimelineTrack(name, color, self.undo_stack)
        track.px_per_sec    = self.px_per_sec
        track.scroll_offset = 0.0
        track.clip_changed.connect(self.update_scroll_range)
        self._track_layout.insertWidget(
            self._track_layout.count() - 1, track)  # stretch の前に挿入
        self._tracks.append(track)
        return track

    def add_track(self, name: str, color: Optional[QColor] = None) -> TimelineTrack:
        import random
        c = color or QColor(
            random.randint(80, 220), random.randint(80, 220), random.randint(80, 220))
        return self._make_track(name, c)

    # ── ズーム ────────────────────────────────────────────────────

    def zoom(self, factor_delta: float) -> None:
        """factor_delta: +0.25 = 25% 拡大, -0.25 = 25% 縮小"""
        anchor_sec = self.header.playhead_sec  # プレイヘッドを中心にズーム
        old_pps    = self.px_per_sec
        new_pps    = max(self.PPS_MIN,
                         min(self.PPS_MAX, old_pps * (1 + factor_delta)))
        self.px_per_sec = new_pps
        self._apply_pps(new_pps)
        # ズーム後にプレイヘッドが同じスクリーン位置になるようスクロール補正
        center_px = self.width() / 2
        new_scroll = max(0.0, anchor_sec - center_px / new_pps)
        self._set_scroll_sec(new_scroll)
        self.update_scroll_range()

    def _apply_pps(self, pps: float) -> None:
        self.header.set_px_per_sec(pps)
        for t in self._tracks:
            t.set_px_per_sec(pps)
        self._zoom_label.setText(f"{int(pps)}px/s")

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = 0.2 if event.angleDelta().y() > 0 else -0.2
            self.zoom(delta)
            event.accept()
        else:
            super().wheelEvent(event)

    # ── スクロール ────────────────────────────────────────────────

    @Slot(int)
    def _on_hscroll(self, value: int) -> None:
        # value は秒 × 100 の整数 (精度 0.01 秒)
        sec = value / 100.0
        self.header.set_scroll_offset_sec(sec)
        for t in self._tracks:
            t.set_scroll_offset_sec(sec)

    def _set_scroll_sec(self, sec: float) -> None:
        v = int(sec * 100)
        v = max(0, min(self.h_scrollbar.maximum(), v))
        self.h_scrollbar.setValue(v)

    def update_scroll_range(self) -> None:
        right_sec  = max((t.content_right_edge_sec() for t in self._tracks), default=0.0)
        right_sec  = max(right_sec, 0.0)
        visible_sec = max(0.01, (self.width() - TimelineTrack.HEADER_W) / self.px_per_sec)
        max_sec    = max(0.0, right_sec + 10.0 - visible_sec)
        max_val    = int(max_sec * 100)

        self.h_scrollbar.setPageStep(int(visible_sec * 100))
        old_max = self.h_scrollbar.maximum()
        self.h_scrollbar.setMaximum(max_val)
        if old_max == 0 and max_val > 0:
            pass   # 初回は維持
        elif self.h_scrollbar.value() > max_val:
            self.h_scrollbar.setValue(max_val)

    def scroll_to_playhead(self, sec: float) -> None:
        visible_sec    = max(0.01, (self.width() - TimelineTrack.HEADER_W) / self.px_per_sec)
        current_scroll = self.h_scrollbar.value() / 100.0
        LOOKAHEAD      = 2.0   # 右端から 2 秒手前で追従開始

        if sec < current_scroll:
            self._set_scroll_sec(max(0.0, sec - 1.0))
        elif sec > current_scroll + visible_sec - LOOKAHEAD:
            self._set_scroll_sec(sec - visible_sec + LOOKAHEAD + 1.0)


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

    def show_frame(self, ppm_path: str) -> None:
        """PPM ファイルをプレビューに表示する。"""
        if not os.path.exists(ppm_path):
            return
        pix = QPixmap(ppm_path)
        if pix.isNull():
            return
        for item in self._scene.items():
            if isinstance(item, QGraphicsPixmapItem):
                self._scene.removeItem(item)
        item = QGraphicsPixmapItem(pix)
        item.setPos(0, 0)
        self._scene.addItem(item)

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
# CutStudioMain
# ══════════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio")
        self.resize(1440, 900)
        self.setMinimumSize(960, 600)

        # ── エンジン ──────────────────────────────────────────────
        self.video = _ve_mod.VideoEngine()
        self.bridge = VOSEBridge()
        self.analyzer:      Optional[Any] = None
        self.talk_manager:  Optional[Any] = None
        if is_engine_available:
            self.analyzer     = IntonationAnalyzer()
            self.talk_manager = TalkManager()

        # ── Undo ──────────────────────────────────────────────────
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(200)

        # ── UI ────────────────────────────────────────────────────
        self.video_preview = PreviewView()
        self._transport_btns: List[QPushButton] = []
        self._export_btn:    Optional[QPushButton] = None
        self._project_path:  Optional[str] = None

        self._init_ui()

        # ── PlaybackEngine ─────────────────────────────────────────
        self.playback_engine = PlaybackEngine(
            preview_view    = self.video_preview,
            timeline_header = self.timeline.header,
            status_bar      = self._status,
        )
        self.playback_engine.position_updated.connect(self._on_position_updated)
        self.timeline.header._on_seek_from_header = (
            lambda sec: self.playback_engine.seek(sec)
        )

        # TransportController
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

        # ── ショートカット ─────────────────────────────────────────
        self._setup_shortcuts()

        # ── プレビューフレーム更新タイマー ─────────────────────────
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(100)
        self._preview_timer.timeout.connect(self._update_preview_frame)
        self._preview_timer.start()

    # ── 初期化 ────────────────────────────────────────────────────

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

        self.timeline = TimelineWidget(self.undo_stack)
        for track in [self.timeline.voice_track, self.timeline.video_track]:
            track.synthesize_requested.connect(self._on_synthesize_from_clip)
        tl_layout.addWidget(self.timeline)

        v_split.addWidget(h_split)
        v_split.addWidget(tl_frame)
        v_split.setStretchFactor(0, 6)
        v_split.setStretchFactor(1, 4)
        v_split.setSizes([580, 220])

        root.addWidget(v_split)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage(
            "VO-SE Cut Studio  —  Phase 2 完了  |  "
            "Space=再生  Ctrl+Z=Undo  Ctrl+S=保存"
        )

    def _make_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setObjectName("toolbar")
        bar.setStyleSheet(
            "QFrame#toolbar {"
            "background-color: #232325; border-bottom: 1px solid #3a3a3c;}"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(6)

        title = QLabel("VO-SE Cut Studio")
        title.setStyleSheet(
            "color:#ffffff;font-size:14px;font-weight:600;background:transparent;"
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
        for icon, tip in [
            ("⏮", "先頭へ"), ("⏪", "5秒戻る"), ("▶", "再生"),
            ("⏩", "5秒進む"), ("⏭", "末尾へ"),
        ]:
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn.setStyleSheet(btn_style)
            btn.setFixedSize(32, 28)
            layout.addWidget(btn)
            self._transport_btns.append(btn)

        layout.addSpacing(12)

        # トラック追加ボタン
        add_track_btn = QPushButton("＋ トラック")
        add_track_btn.setFixedHeight(28)
        add_track_btn.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2e; color: #ebebf5; border: none;
                border-radius: 7px; padding: 0 12px; font-size: 12px;
            }
            QPushButton:hover   { background-color: #3a3a3c; }
            QPushButton:pressed { background-color: #1c1c1e; }
        """)
        add_track_btn.clicked.connect(self._on_add_track)
        layout.addWidget(add_track_btn)

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
            "QFrame#sidebar {"
            "background-color: #1c1c1e; border-right: 1px solid #3a3a3c;}"
        )
        ly = QVBoxLayout(sidebar)
        ly.setContentsMargins(12, 16, 12, 16)
        ly.setSpacing(8)

        ly.addWidget(self._section_label("素材ライブラリ"))
        self.asset_list = QListWidget()
        self.asset_list.setFixedHeight(130)
        self.asset_list.setToolTip("ダブルクリックでタイムラインに配置")
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

        ly.addWidget(self._divider())
        ly.addSpacing(4)

        # プロジェクト操作
        ly.addWidget(self._section_label("プロジェクト"))
        save_btn = QPushButton("💾  保存")
        save_btn.setFixedHeight(28)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2e; color: #ebebf5;
                border: 1px solid #3a3a3c; border-radius: 7px; font-size: 12px;
            }
            QPushButton:hover   { background-color: #3a3a3c; }
        """)
        save_btn.clicked.connect(self._on_save_project)
        ly.addWidget(save_btn)

        load_btn = QPushButton("📂  開く")
        load_btn.setFixedHeight(28)
        load_btn.setStyleSheet(save_btn.styleSheet())
        load_btn.clicked.connect(self._on_load_project)
        ly.addWidget(load_btn)

        ly.addStretch()

        ok    = is_engine_available
        label = QLabel("✅  エンジン接続済" if ok else "⚠️  エンジン未接続")
        label.setStyleSheet(
            f"color: {'#30d158' if ok else '#ff9f0a'};"
            " font-size:11px; background:transparent;"
        )
        ly.addWidget(label)

        ve_ok  = self.video.available
        ve_lbl = QLabel("✅  映像エンジン接続" if ve_ok else "⚠️  映像エンジン未接続")
        ve_lbl.setStyleSheet(
            f"color: {'#30d158' if ve_ok else '#636366'};"
            " font-size:11px; background:transparent;"
        )
        ly.addWidget(ve_lbl)
        return sidebar

    # ── ショートカット ────────────────────────────────────────────

    def _setup_shortcuts(self) -> None:
        def sc(key: str, slot: Any) -> None:
            QShortcut(QKeySequence(key), self).activated.connect(slot)

        sc("Space",   self._toggle_play)
        sc("Ctrl+Z",  self.undo_stack.undo)
        sc("Ctrl+Y",  self.undo_stack.redo)
        sc("Ctrl+Shift+Z", self.undo_stack.redo)
        sc("Ctrl+S",  self._on_save_project)
        sc("Ctrl+O",  self._on_load_project)
        sc("Ctrl+=",  lambda: self.timeline.zoom(0.25))
        sc("Ctrl+-",  lambda: self.timeline.zoom(-0.25))
        sc("Left",    lambda: self._nudge(-1.0))
        sc("Right",   lambda: self._nudge(1.0))
        sc("Shift+Left",  lambda: self._nudge(-5.0))
        sc("Shift+Right", lambda: self._nudge(5.0))
        sc("Home",    lambda: self.playback_engine.seek(0.0))

    def _toggle_play(self) -> None:
        if hasattr(self, "transport"):
            self.transport.toggle_play()
        elif hasattr(self, "playback_engine"):
            if self.playback_engine.is_playing:
                self.playback_engine.pause()
            else:
                self.playback_engine.play()

    def _nudge(self, delta: float) -> None:
        if not hasattr(self, "playback_engine"):
            return
        cur = self.timeline.header.playhead_sec
        new = max(0.0, cur + delta)
        self.playback_engine.seek(new)

    # ── Slot: 再生位置更新 ────────────────────────────────────────

    @Slot(float)
    def _on_position_updated(self, sec: float) -> None:
        self.timeline.header.set_playhead(sec)
        self.timeline.scroll_to_playhead(sec)

    # ── Slot: プレビューフレーム更新 ──────────────────────────────

    def _update_preview_frame(self) -> None:
        if not self.video.available:
            return
        sec      = self.timeline.header.playhead_sec
        tmp_path = "/tmp/vose_preview_frame.ppm"
        if self.video.save_preview(sec, tmp_path):
            self.video_preview.show_frame(tmp_path)

    # ── Slot: 素材追加・配置 ──────────────────────────────────────

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

        # VideoEngine にロード
        if self.video.available:
            ok = self.video.load_video(path)
            if ok:
                self.video.build_keyframe_index()
                self._status.showMessage(
                    f"✅  ロード: {os.path.basename(path)}"
                    f"  {self.video.duration:.1f}s  "
                    f"{self.video.width}×{self.video.height}"
                    f"  {self.video.fps:.2f}fps"
                )

        # 再生エンジンにロード
        self.playback_engine.load(path)

        # タイムラインにクリップ追加
        start = self.timeline.header.playhead_sec
        if path.lower().endswith((".wav", ".mp3", ".aac", ".flac")):
            dur   = get_wav_duration_sec(path) if path.endswith(".wav") else 5.0
            color = QColor(10, 132, 255)
            track = self.timeline.voice_track
        else:
            dur   = self.video.duration if self.video.available and self.video.duration > 0 else 5.0
            color = QColor(48, 209, 88)
            track = self.timeline.video_track

        # 波形抽出
        wf: List[float] = []
        if self.video.available and path.lower().endswith((".wav",)):
            wf = self.video.extract_waveform(512)

        name = os.path.basename(path)
        short = (name[:18] + "…") if len(name) > 18 else name
        track.add_clip(start, dur, short,
                       color=color, raw_text=path,
                       wav_path=path, waveform=wf)
        self.timeline.update_scroll_range()
        self.timeline.scroll_to_playhead(start + dur)

    # ── Slot: TTS 合成 ────────────────────────────────────────────

    def _on_generate_clicked(self) -> None:
        text = self.tts_input.toPlainText().strip()
        if not text:
            return
        self.generate_button.setEnabled(False)
        self.generate_button.setText("合成中…")
        self._status.showMessage("🎙️  合成中…")

        wav_path = "output_tts.wav"
        dur      = 2.0

        if self.talk_manager:
            ok, _ = self.talk_manager.synthesize(text, wav_path)
            if not ok:
                self._status.showMessage("❌  TTS 合成に失敗しました")
                self.generate_button.setEnabled(True)
                self.generate_button.setText("音声を合成して配置")
                return
            dur = get_wav_duration_sec(wav_path)

        # 波形抽出
        wf: List[float] = []
        if self.video.available and os.path.exists(wav_path):
            tmp_ve = _ve_mod.VideoEngine()
            if tmp_ve.available:
                tmp_ve.load_video(wav_path)
                wf = tmp_ve.extract_waveform(512)

        start      = self.timeline.header.playhead_sec
        short_text = (text[:16] + "…") if len(text) > 16 else text

        self.timeline.voice_track.add_clip(
            start, dur, f"🎙  {short_text}",
            color=QColor(10, 132, 255), raw_text=text,
            wav_path=wav_path, waveform=wf,
        )
        self.timeline.video_track.add_clip(
            start, dur, f"💬  {short_text}",
            color=QColor(48, 209, 88), raw_text=text,
        )
        self.timeline.header.set_playhead(start + dur)
        self.timeline.update_scroll_range()
        self.timeline.scroll_to_playhead(start + dur)

        if is_engine_available and self.analyzer:
            notes = generate_talk_events(text, self.analyzer)
            if notes:
                self.bridge.render(notes, output_file="output_rendered.wav")

        self.tts_input.clear()
        self._status.showMessage(f"✅  合成完了: {short_text}")
        self.generate_button.setEnabled(True)
        self.generate_button.setText("音声を合成して配置")

    def _on_synthesize_from_clip(self, text: str, start_sec: float) -> None:
        if not text or not self.talk_manager:
            self._status.showMessage("⚠️  TalkManager が初期化されていません")
            return
        self._status.showMessage(f"🎙️  クリップから合成中: {text[:20]}…")
        wav_path = "output_clip_tts.wav"
        ok, _    = self.talk_manager.synthesize(text, wav_path)
        if not ok:
            self._status.showMessage("❌  TTS 合成に失敗しました")
            return
        dur        = get_wav_duration_sec(wav_path)
        short_text = (text[:16] + "…") if len(text) > 16 else text
        self.timeline.voice_track.add_clip(
            start_sec, dur, f"🎙  {short_text}",
            color=QColor(10, 132, 255), raw_text=text, wav_path=wav_path,
        )
        self.timeline.header.set_playhead(start_sec + dur)
        self.timeline.update_scroll_range()
        self.timeline.scroll_to_playhead(start_sec + dur)
        self._status.showMessage(f"✅  クリップ合成完了: {short_text}")

    # ── Slot: トラック追加 ────────────────────────────────────────

    def _on_add_track(self) -> None:
        name, ok = QInputDialog.getText(
            self, "トラック追加", "トラック名:", text="🎵  NEW"
        )
        if ok and name:
            new_track = self.timeline.add_track(name)
            new_track.synthesize_requested.connect(self._on_synthesize_from_clip)
            self._status.showMessage(f"✅  トラック追加: {name}")

    # ── Slot: 書き出し ────────────────────────────────────────────

    def _on_export_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "書き出し先を選択", "output.mp4",
            "MP4 (*.mp4);;MOV (*.mov);;MKV (*.mkv)"
        )
        if not path:
            return
        self._status.showMessage(f"📤  書き出し中: {path} …")

        if self.video.available:
            # 秒ベースで EDL JSON を生成 (px_per_sec 依存なし)
            clips = [
                (c["start"], c["duration"])
                for c in self.timeline.voice_track.clips
            ]
            edl_json = json.dumps([
                {"in": s, "out": s + d, "enabled": True}
                for s, d in clips if d > 0
            ], ensure_ascii=False)
            ok  = self.video.export_hw(edl_json, path, quality=23)
            msg = (f"✅  書き出し完了: {path}"
                   if ok else "❌  書き出しに失敗しました")
        else:
            msg = "⚠️  VideoEngine が利用できません"
        self._status.showMessage(msg)

    # ── Slot: プロジェクト保存 ────────────────────────────────────

    def _on_save_project(self) -> None:
        if not self._project_path:
            path, _ = QFileDialog.getSaveFileName(
                self, "プロジェクトを保存", "project.vose",
                "VO-SE Project (*.vose);;JSON (*.json)"
            )
            if not path:
                return
            self._project_path = path

        def color_to_hex(c: QColor) -> str:
            return c.name()

        data: Dict[str, Any] = {
            "version": 2,
            "tracks": [],
        }
        for track in self.timeline._tracks:
            t_data = {
                "name":  track.track_name,
                "color": color_to_hex(track.track_color),
                "clips": [
                    {
                        "start":    c["start"],
                        "duration": c["duration"],
                        "text":     c["text"],
                        "raw_text": c["raw_text"],
                        "color":    color_to_hex(c["color"]),
                        "wav_path": c.get("wav_path", ""),
                    }
                    for c in track.clips
                ],
            }
            data["tracks"].append(t_data)

        try:
            with open(self._project_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.setWindowTitle(
                f"VO-SE Cut Studio — {os.path.basename(self._project_path)}")
            self._status.showMessage(
                f"💾  保存完了: {self._project_path}")
            self.undo_stack.setClean()
        except Exception as e:
            self._status.showMessage(f"❌  保存エラー: {e}")

    # ── Slot: プロジェクト読み込み ────────────────────────────────

    def _on_load_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトを開く", "",
            "VO-SE Project (*.vose);;JSON (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._status.showMessage(f"❌  読み込みエラー: {e}")
            return

        # 既存トラックをクリア
        for track in self.timeline._tracks:
            track.clips.clear()
            track.update()

        version = data.get("version", 1)
        tracks_data = data.get("tracks", [])

        for i, t_data in enumerate(tracks_data):
            # 既存トラックを再利用 or 新規作成
            if i < len(self.timeline._tracks):
                track = self.timeline._tracks[i]
                track.track_name  = t_data.get("name", track.track_name)
                track.track_color = QColor(t_data.get("color", "#0a84ff"))
            else:
                track = self.timeline.add_track(
                    t_data.get("name", f"トラック {i+1}"),
                    QColor(t_data.get("color", "#0a84ff")),
                )
                track.synthesize_requested.connect(self._on_synthesize_from_clip)

            for c_data in t_data.get("clips", []):
                if version >= 2:
                    start = float(c_data.get("start", 0.0))
                    dur   = float(c_data.get("duration", 2.0))
                else:
                    # v1 互換: x/width (px) → 秒
                    pps  = 100.0
                    start = float(c_data.get("x", 0)) / pps
                    dur   = float(c_data.get("width", 200)) / pps

                wav_path = c_data.get("wav_path", "")
                wf: List[float] = []
                if wav_path and os.path.exists(wav_path) and self.video.available:
                    tmp_ve = _ve_mod.VideoEngine()
                    if tmp_ve.available:
                        tmp_ve.load_video(wav_path)
                        wf = tmp_ve.extract_waveform(512)

                clip: Dict[str, Any] = {
                    "start":    start,
                    "duration": dur,
                    "text":     c_data.get("text", ""),
                    "raw_text": c_data.get("raw_text", ""),
                    "color":    QColor(c_data.get("color", "#0a84ff")),
                    "wav_path": wav_path,
                    "waveform": wf,
                }
                track.clips.append(clip)
                track.update()

        self._project_path = path
        self.setWindowTitle(
            f"VO-SE Cut Studio — {os.path.basename(path)}")
        self.timeline.update_scroll_range()
        self.undo_stack.clear()
        self._status.showMessage(f"📂  読み込み完了: {path}")

    # ── closeEvent ────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if not self.undo_stack.isClean():
            # 未保存確認は省略 (必要ならQMessageBox追加)
            pass
        self._preview_timer.stop()
        self.playback_engine.stop()
        super().closeEvent(event)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:#636366;font-size:10px;font-weight:700;"
            "letter-spacing:0.6px;background:transparent;padding:2px 0;"
        )
        return lbl

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.NoFrame)
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: #3a3a3c;")
        return line


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
