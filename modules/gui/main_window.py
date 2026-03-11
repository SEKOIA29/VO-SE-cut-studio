# main_window.py

"""
# VO-SE Cut Studio — メインウィンドウ
"""
# =================================================

from __future__ import annotations

import sys
import os
import ctypes
import platform
import traceback
from typing import Any, List, Dict, Optional, Tuple, TYPE_CHECKING

# =================================================
# PySide6
# =================================================
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTextEdit, QPushButton, QLabel, QListWidget,
    QFrame, QStackedWidget, QGraphicsView, QGraphicsScene,
    QGraphicsTextItem, QScrollBar,
)
from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPaintEvent, QMouseEvent
)

# ══════════════════════════════════════════════════════════════
# VO-SE Engine — 型定義と動的ロード（Pyright完全対応版）
# ══════════════════════════════════════════════════════════════

is_engine_available: bool = False

if TYPE_CHECKING:
    # CI環境用モック定義
    class IntonationAnalyzer:
        def __init__(self) -> None:
            ...

        def analyze(self, text: str) -> str:
            ...

        def analyze_to_phonemes(self, text: str) -> List[str]:
            ...

        def analyze_to_accent_phrases(self, text: str) -> Any:
            ...

    class TalkManager:
        def __init__(self) -> None:
            ...

        def set_voice(self, path: str) -> bool:
            ...

        def synthesize(
            self,
            text: str,
            output_path: str,
            speed: float = 1.0
        ) -> Tuple[bool, str]:
            ...

    def generate_talk_events(
        text: str,
        analyzer: IntonationAnalyzer
    ) -> List[Dict[str, Any]]:
        ...

else:
    # 実行環境での動的ロード
    try:
        import vo_se_engine
        IntonationAnalyzer = vo_se_engine.IntonationAnalyzer
        TalkManager = vo_se_engine.TalkManager
        generate_talk_events = vo_se_engine.generate_talk_events
        is_engine_available = True
    except (ImportError, AttributeError) as e:
        print(f"⚠️ VO-SE Engine integration failed: {e}")

        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        class IntonationAnalyzer:
            pass

        class TalkManager:
            pass

        is_engine_available = False

# 外部公開用（if-elseの外に配置）
__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]

else:
    # 実行環境での動的ロード
    try:
        import vo_se_engine
        IntonationAnalyzer = vo_se_engine.IntonationAnalyzer
        TalkManager = vo_se_engine.TalkManager
        generate_talk_events = vo_se_engine.generate_talk_events
        is_engine_available = True
    except (ImportError, AttributeError) as e:
        print(f"⚠️ VO-SE Engine integration failed: {e}")
        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []
            
        IntonationAnalyzer = type("IntonationAnalyzer", (object,), {})
        TalkManager = type("TalkManager", (object,), {})
        is_engine_available = False

# ══════════════════════════════════════════════════════════════
# 1. C++ 構造体バインディング（UTAU 対応フルセット）
# ══════════════════════════════════════════════════════════════

class NoteEvent(ctypes.Structure):
    """
    VO-SE C++ エンジン用構造体。
    C++ 側の struct NoteEvent とメモリ配置を完全一致させること。
    """
    _fields_ = [
        ("wav_path",          ctypes.c_char_p),
        ("pitch_length",      ctypes.c_int),
        ("pitch_curve",       ctypes.POINTER(ctypes.c_double)),
        ("gender_curve",      ctypes.POINTER(ctypes.c_double)),
        ("tension_curve",     ctypes.POINTER(ctypes.c_double)),
        ("breath_curve",      ctypes.POINTER(ctypes.c_double)),
        # UTAU 互換パラメータ（oto.ini 対応）
        ("offset_ms",         ctypes.c_double),   # 原音の開始位置
        ("consonant_ms",      ctypes.c_double),   # 固定範囲（子音部）
        ("cutoff_ms",         ctypes.c_double),   # 右ブランク
        ("pre_utterance_ms",  ctypes.c_double),   # 先行発声
        ("overlap_ms",        ctypes.c_double),   # オーバーラップ
    ]

# ══════════════════════════════════════════════════════════════
# 2. DLL/dylib ブリッジ
# ══════════════════════════════════════════════════════════════

class VOSEBridge:
    """
    Python ↔ C++ DLL/dylib ブリッジ。
    - macOS: RTLD_GLOBAL でシンボルをグローバル公開
    - keep_alive で GC によるクラッシュを防止
    """

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

            # init_official_engine（存在する場合のみ呼び出す）
            if hasattr(self.lib, "init_official_engine"):
                self.lib.init_official_engine.argtypes = []
                self.lib.init_official_engine.restype = None
                self.lib.init_official_engine()

            # execute_render の型定義
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
        self,
        notes_list: List[Dict[str, Any]],
        output_file: str = "output.wav",
    ) -> None:
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

            # C 型に変換
            c_wav = phoneme.encode("utf-8")
            c_p   = (ctypes.c_double * len(pitch))(*pitch)
            c_g   = (ctypes.c_double * len(gender))(*gender)
            c_t   = (ctypes.c_double * len(tension))(*tension)
            c_b   = (ctypes.c_double * len(breath))(*breath)

            # ★ GC 対策：C 側にポインタを渡す間、参照を保持
            self.keep_alive.extend([c_wav, c_p, c_g, c_t, c_b])

            c_notes[i].wav_path         = c_wav
            c_notes[i].pitch_length     = len(pitch)
            c_notes[i].pitch_curve      = c_p
            c_notes[i].gender_curve     = c_g
            c_notes[i].tension_curve    = c_t
            c_notes[i].breath_curve     = c_b
            # UTAU パラメータ（oto.ini から外部差し込み可能）
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

        # 目盛り
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.setFont(QFont("Consolas", 8))
        for x in range(0, self.width(), 50):
            painter.drawLine(x, 20, x, 30)
            painter.drawText(x + 5, 15, f"{x // 50:02d}:00")

        # 再生ヘッド（赤ライン）
        painter.setPen(QPen(QColor(255, 60, 60), 2))
        painter.drawLine(self.playhead_x, 0, self.playhead_x, 30)

        # 三角マーカー
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


class TimelineTrack(QFrame):
    """タイムラインの各トラック（音声・動画・モーション）"""

    def __init__(self, name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setFixedHeight(60)
        self.setStyleSheet("background-color: #2a2a2a; border: 1px solid #3f3f3f;")
        self.track_name = name

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor(180, 180, 180))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.fillRect(QRect(0, 0, 100, 60), QColor(45, 45, 45))
        painter.drawText(10, 35, self.track_name)
        painter.end()


class TimelineWidget(QWidget):
    """タイムライン全体の管理ウィジェット"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = TimelineHeader()

        self.scroll_content = QWidget()
        tracks_layout = QVBoxLayout(self.scroll_content)
        tracks_layout.setContentsMargins(0, 0, 0, 0)
        tracks_layout.setSpacing(1)
        tracks_layout.addWidget(TimelineTrack("🎙️ VOICE"))
        tracks_layout.addWidget(TimelineTrack("🎬 VIDEO"))
        tracks_layout.addWidget(TimelineTrack("🦴 MOTION"))
        tracks_layout.addStretch()

        self.h_scrollbar = QScrollBar(Qt.Orientation.Horizontal)

        layout.addWidget(self.header)
        layout.addWidget(self.scroll_content)
        layout.addWidget(self.h_scrollbar)


class PreviewView(QGraphicsView):
    """プレビューエリア（FFmpeg 出力 / ボーンオーバーレイ）"""

    def __init__(self, label: str = "Preview") -> None:
        super().__init__()
        scene = QGraphicsScene()
        self.setScene(scene)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = scene.addText(label)
        text.setDefaultTextColor(QColor(200, 200, 200))

# ══════════════════════════════════════════════════════════════
# 4. メインウィンドウ
# ══════════════════════════════════════════════════════════════

class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio - Early Alpha")
        self.resize(1280, 800)

        # エンジン初期化
        self.bridge = VOSEBridge()
        
        self.analyzer: Optional[IntonationAnalyzer] = (
            IntonationAnalyzer() if is_engine_available else None
        )
        self.talk_manager: Optional[TalkManager] = (
            TalkManager() if is_engine_available else None
        )

        # UI
        self.preview_stack = QStackedWidget()
        self.video_preview = PreviewView("Preview Area (FFmpeg Output / Bone Overlay)")
        self.motion_editor = PreviewView("Motion Editor")
        self._init_ui()

    def _init_ui(self) -> None:
        self.analyzer: Optional[IntonationAnalyzer] = (
            IntonationAnalyzer() if is_engine_available else None
        )
        self.talk_manager: Optional[TalkManager] = (
            TalkManager() if is_engine_available else None
        )
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

        # ── 中央パネル（プレビュー）──────────────
        self.preview_stack.addWidget(self.video_preview)
        self.preview_stack.addWidget(self.motion_editor)

        # ── 右パネル（モード切替）────────────────
        right = QFrame()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("🛠️ モード"))

        self.btn_video = QPushButton("🎬 動画編集")
        self.btn_video.setCheckable(True)
        self.btn_video.setChecked(True)
        self.btn_video.clicked.connect(lambda: self._switch_mode(0))

        self.btn_motion = QPushButton("🦴 モーション")
        self.btn_motion.setCheckable(True)
        self.btn_motion.clicked.connect(lambda: self._switch_mode(1))

        right_layout.addWidget(self.btn_video)
        right_layout.addWidget(self.btn_motion)
        right_layout.addStretch()

        # 上部結合
        h_splitter.addWidget(left)
        h_splitter.addWidget(self.preview_stack)
        h_splitter.addWidget(right)
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

    def _switch_mode(self, index: int) -> None:
        self.preview_stack.setCurrentIndex(index)
        self.btn_video.setChecked(index == 0)
        self.btn_motion.setChecked(index == 1)

    def _on_generate_clicked(self) -> None:
        text = self.tts_input.toPlainText().strip()
        if not text:
            return

        if not is_engine_available or self.analyzer is None:
            print("⚠️ vo_se_engine が利用できないため合成をスキップします。")
            return

        print(f"🎙️ 合成開始: {text}")

        # 1. トークイベント生成（音素 → NoteEvent リスト）
        notes = generate_talk_events(text, self.analyzer)

        # 2. C++ レンダリング（VO-SE エンジン）
        if notes:
            self.bridge.render(notes, output_file="output.wav")

        # 3. TalkManager で WAV を保存（pyopenjtalk 経由）
        if self.talk_manager:
            ok, result = self.talk_manager.synthesize(text, "output_tts.wav")
            if ok:
                print(f"✅ TTS 保存完了: {result}")
            else:
                print(f"❌ TTS 失敗: {result}")

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
