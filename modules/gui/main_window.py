# main_window.py

"""
# VO-SE Cut Studio — メインウィンドウ（動画編集専用）
"""
# =================================================

from __future__ import annotations

import wave
import sys
import os
import ctypes
import platform
import traceback
from typing import Any, List, Dict, Optional, Tuple, TYPE_CHECKING

# =================================================
# ファイルインポート
# =================================================

import video_engine # 映像編集用

# =================================================
# PySide6
# =================================================
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QSplitter, QTextEdit,
    QPushButton, QLabel, QListWidget, QFrame,
    QGraphicsView, QGraphicsScene, QScrollBar,
    QGraphicsPixmapItem
)
from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent,
    QPixmap
)

# ══════════════════════════════════════════════════════════════
# VO-SE Engine — 型定義と動的ロード（Pyright完全対応版）
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

    def generate_talk_events(text: str, analyzer: IntonationAnalyzer) -> List[Dict[str, Any]]: ...

else:
    try:
        import vo_se_engine
        IntonationAnalyzer = vo_se_engine.IntonationAnalyzer
        TalkManager = vo_se_engine.TalkManager
        generate_talk_events = vo_se_engine.generate_talk_events
        is_engine_available = True
    except (ImportError, AttributeError) as e:
        print(f"VO-SE Engine integration failed: {e}")

        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        class IntonationAnalyzer:
            pass

        class TalkManager:
            pass

        is_engine_available = False

__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]


def get_wav_duration_px(file_path: str, px_per_sec: int = 100) -> int:
    """WAVファイルの正確な長さをピクセルに変換する"""
    if not os.path.exists(file_path):
        return 200
    try:
        with wave.open(file_path, 'rb') as wr:
            frames = wr.getnframes()
            rate = wr.getframerate()
            duration = frames / float(rate)
            return int(duration * px_per_sec)
    except Exception as e:
        print(f"Error reading WAV: {e}")
        return 200


# ══════════════════════════════════════════════════════════════
# 1. C++ 構造体バインディング（UTAU 対応フルセット）
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
# 2. DLL/dylib ブリッジ
# ══════════════════════════════════════════════════════════════

class VOSEBridge:
    def __init__(self) -> None:
        self.lib: Optional[ctypes.CDLL] = None
        self.keep_alive: List[Any] = []
        self._load_engine()

    def _load_engine(self) -> None:
        is_mac = platform.system() == "Darwin"
        ext = ".dylib" if is_mac else ".dll"
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
                self.lib.init_official_engine.restype = None
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

    def render(self, notes_list: List[Dict[str, Any]], output_file: str = "output.wav") -> None:
        if not self.lib:
            print("❌ Engine not loaded.")
            return

        count: int = len(notes_list)
        if count == 0:
            print("⚠️ No notes to render.")
            return

        NotesArray = NoteEvent * count
        c_notes = NotesArray()
        self.keep_alive = []

        for i, data in enumerate(notes_list):
            phoneme: str = str(data.get("phoneme", "a"))
            pitch: list[float] = list(data.get("pitch", [150.0] * 50))
            gender: list[float] = list(data.get("gender", [0.5] * 50))
            tension: list[float] = list(data.get("tension", [0.5] * 50))
            breath: list[float] = list(data.get("breath", [0.1] * 50))

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
# 3. UI コンポーネント
# ══════════════════════════════════════════════════════════════

class TimelineHeader(QWidget):
    """タイムライン上部の時間目盛り＆再生ヘッド"""

    positionChanged = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setStyleSheet("background-color: #333333;")
        self.playhead_x: int = 50
        self.is_dragging: bool = False

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.setFont(QFont("Consolas", 8))
        for x in range(0, self.width(), 50):
            painter.drawLine(x, 20, x, 30)
            painter.drawText(x + 5, 15, f"{x // 50:02d}:00")

        painter.setPen(QPen(QColor(255, 60, 60), 2))
        painter.drawLine(self.playhead_x, 0, self.playhead_x, 30)

        triangle = [
            QPoint(self.playhead_x - 5, 0),
            QPoint(self.playhead_x + 5, 0),
            QPoint(self.playhead_x, 8),
        ]
        painter.setBrush(QColor(255, 60, 60))
        painter.drawPolygon(triangle)
        painter.end()

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

    def _apply_playhead_update(self, x: int) -> None:
        self.playhead_x = max(0, min(x, self.width()))
        self.update()
        self.positionChanged.emit(self.playhead_x)


class TimelineTrack(QFrame):
    """タイムラインの各トラック"""

    def __init__(self, name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setFixedHeight(60)
        self.setStyleSheet("background-color: #2a2a2a; border: 1px solid #3f3f3f;")
        self.track_name = name
        self.dragging_clip_idx: Optional[int] = None
        self.drag_start_offset: int = 0
        self.setMouseTracking(True)
        self.clips: List[Dict[str, Any]] = []

    def mousePressEvent(self, event: QMouseEvent) -> None:
        header_width = 100
        x = event.position().x() - header_width
        for i, clip in enumerate(reversed(self.clips)):
            idx = len(self.clips) - 1 - i
            if clip["x"] <= x <= clip["x"] + clip["width"]:
                self.dragging_clip_idx = idx
                self.drag_start_offset = x - clip["x"]
                break

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.dragging_clip_idx is not None:
            header_width = 100
            new_x = event.position().x() - header_width - self.drag_start_offset
            self.clips[self.dragging_clip_idx]["x"] = max(0, int(new_x))
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.dragging_clip_idx = None

    def add_clip(
        self,
        x: int,
        width: int,
        text: str,
        color: QColor = QColor(70, 130, 180, 200)
    ) -> None:
        self.clips.append({"x": x, "width": width, "text": text, "color": color})
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        header_width = 100
        painter.fillRect(QRect(0, 0, header_width, 60), QColor(45, 45, 45))
        painter.setPen(QColor(180, 180, 180))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(10, 35, self.track_name)

        for clip in self.clips:
            clip_rect = QRect(header_width + clip["x"], 10, clip["width"], 40)
            painter.setBrush(clip["color"])
            painter.setPen(QPen(clip["color"].lighter(120), 1))
            painter.drawRoundedRect(clip_rect, 4, 4)
            painter.setPen(Qt.GlobalColor.white)
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                clip_rect.adjusted(5, 0, -5, 0),
                Qt.AlignmentFlag.AlignCenter,
                clip["text"]
            )


class TimelineWidget(QWidget):
    """タイムライン全体の管理ウィジェット（動画編集用：VOICE・VIDEO の2トラック）"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = TimelineHeader()

        self.scroll_content = QWidget()
        self.tracks_layout = QVBoxLayout(self.scroll_content)
        self.tracks_layout.setContentsMargins(0, 0, 0, 0)
        self.tracks_layout.setSpacing(1)

        # 動画編集に必要な2トラックのみ（MOTIONトラックは削除）
        self.voice_track = TimelineTrack("🎙️ VOICE")
        self.video_track = TimelineTrack("🎬 VIDEO")

        self.tracks_layout.addWidget(self.voice_track)
        self.tracks_layout.addWidget(self.video_track)
        self.tracks_layout.addStretch()

        layout.addWidget(self.header)
        layout.addWidget(self.scroll_content)

        self.h_scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        layout.addWidget(self.h_scrollbar)


class PreviewView(QGraphicsView):
    """動画プレビューエリア（画像表示・移動対応）"""

    def __init__(self) -> None:
        super().__init__()
        self._scene_obj = QGraphicsScene()
        self.setScene(self._scene_obj)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 1920x1080 の仮想キャンバス
        self._scene_obj.setSceneRect(0, 0, 1920, 1080)
        self.centerOn(960, 540)
        self._scene_obj.addRect(self._scene_obj.sceneRect(), QPen(QColor(60, 60, 60)))

        self.current_character: Optional[QGraphicsPixmapItem] = None

        self.debug_item = self._scene_obj.addText("Preview Area (FFmpeg Output)")
        self.debug_item.setDefaultTextColor(QColor(100, 100, 100))

    def add_character(self, image_path: str) -> None:
        """画像をプレビューに追加し、マウス操作を有効にする"""
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            print(f"❌ 画像の読み込みに失敗: {image_path}")
            return
        item = QGraphicsPixmapItem(pixmap)
        item.setFlags(
            QGraphicsPixmapItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsPixmapItem.GraphicsItemFlag.ItemIsSelectable
        )
        item.setPos(960 - pixmap.width() / 2, 540 - pixmap.height() / 2)
        self._scene_obj.addItem(item)
        self.current_character = item
        print(f"✅ キャラクタ表示成功: {image_path}")


# ══════════════════════════════════════════════════════════════
# 4. メインウィンドウ
# ══════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.video = video_engine.VideoEngine("./libvideo_engine.dylib") #映像エンジンの初期化
        self.setWindowTitle("VO-SE Cut Studio - Early Alpha")
        self.resize(1280, 800)

        self.bridge = VOSEBridge()

        self.analyzer: Optional[IntonationAnalyzer] = (
            IntonationAnalyzer() if is_engine_available else None
        )
        self.talk_manager: Optional[TalkManager] = (
            TalkManager() if is_engine_available else None
        )

        # プレビューは動画編集の1画面のみ
        self.video_preview = PreviewView()
        self._init_ui()

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左パネル ──────────────────────────────
        left = QFrame()
        left.setFrameStyle(QFrame.Shape.StyledPanel)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("📂 素材ライブラリ"))

        self.asset_list = QListWidget()
        left_layout.addWidget(self.asset_list)

        left_layout.addWidget(QLabel("🎙️ TTS 入力"))
        self.tts_input = QTextEdit()
        self.tts_input.setPlaceholderText("文章を入力...")
        self.tts_input.setMaximumHeight(80)
        left_layout.addWidget(self.tts_input)

        self.generate_button = QPushButton("🎙️ 合成して配置")
        self.generate_button.clicked.connect(self._on_generate_clicked)
        left_layout.addWidget(self.generate_button)

        # ── 中央パネル（動画プレビュー）──────────────
        h_splitter.addWidget(left)
        h_splitter.addWidget(self.video_preview)
        h_splitter.setStretchFactor(1, 6)

        # ── 下部パネル（タイムライン）────────────
        timeline_container = QFrame()
        tl_layout = QVBoxLayout(timeline_container)
        tl_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline = TimelineWidget()
        tl_layout.addWidget(self.timeline)

        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(timeline_container)
        v_splitter.setStretchFactor(0, 6)
        v_splitter.setStretchFactor(1, 4)

        root_layout.addWidget(v_splitter)

    # ----------------------------------------------------------
    # スロット
    # ----------------------------------------------------------

    def _on_generate_clicked(self) -> None:
        text = self.tts_input.toPlainText().strip()
        if not text:
            return

        print(f"🎙️ 合成開始: {text}")

        wav_filename = "output_tts.wav"
        if self.talk_manager:
            ok, _ = self.talk_manager.synthesize(text, wav_filename)
            if not ok:
                print("❌ TTS合成に失敗しました")
                return

        clip_width = 200
        try:
            with wave.open(wav_filename, 'rb') as wr:
                duration = wr.getnframes() / float(wr.getframerate())
                clip_width = int(duration * 100)
        except Exception as e:
            print(f"⚠️ WAV解析エラー: {e}")

        start_x = self.timeline.header.playhead_x

        self.timeline.voice_track.add_clip(
            start_x, clip_width, f"Voice: {text[:10]}..."
        )
        self.timeline.video_track.add_clip(
            start_x, clip_width, f"Text: {text}",
            color=QColor(60, 179, 113, 200)
        )

        self.timeline.header.set_playhead(start_x + clip_width)

        if is_engine_available and self.analyzer:
            notes = generate_talk_events(text, self.analyzer)
            if notes:
                self.bridge.render(notes, output_file="output_rendered.wav")

        self.tts_input.clear()


# ══════════════════════════════════════════════════════════════
# エントリーポイント
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = CutStudioMain()
    window.show()
    sys.exit(app.exec())
