#main_window.py

#=================================================
#基本ライブラリのインポート
#=================================================
import sys
import ctypes
import os
import platform
from typing import List, Dict, Any, Optional, Union

#=================================================
#PYQT
#=================================================
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTextEdit,
    QPushButton,
    QLabel,
    QListWidget,
    QFrame,
    QStackedWidget,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsTextItem,
    QScrollBar,
)
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QEvent
from PySide6.QtGui import (
    QColor, QBrush, QPainter, QPen, QFont, QPolygon, QPaintEvent, QMouseEvent
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QScrollBar
)


#==================================================================
# VO-SE Engine 用の構造体定義 
class NoteEvent(ctypes.Structure):
    _fields_ = [
        ("wav_path", ctypes.c_char_p),      # char*
        ("pitch_length", ctypes.c_int),     # int
        ("pitch_curve", ctypes.POINTER(ctypes.c_double)),   # double*
        ("gender_curve", ctypes.POINTER(ctypes.c_double)),  # double*
        ("tension_curve", ctypes.POINTER(ctypes.c_double)), # double*
        ("breath_curve", ctypes.POINTER(ctypes.c_double)),  # double*
    ]
# --- macOS / Windows 両対応のライブラリロード設定 ---
class VOSEBridge:
    def __init__(self):
        self.lib: Optional[ctypes.CDLL] = None
        # 型を List[Any] として明示し、Unknown を回避
        self.keep_alive: List[Any] = [] 
        self.load_engine()

    def load_engine(self):
        # 1. OS別のライブラリパス設定
        is_mac = platform.system() == "Darwin"
        ext = ".dylib" if is_mac else ".dll"
        
        # 実行ファイルのディレクトリを取得
        base_dir = os.path.dirname(os.path.abspath(__file__))
        lib_path = os.path.join(base_dir, "bin", f"libvo_se_cut{ext}")

        if not os.path.exists(lib_path):
            print(f"⚠️ Warning: Engine not found at {lib_path}")
            return

        try:
            # 2. ライブラリのロード
            if is_mac:
                self.lib = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
            else:
                # WindowsではWinDLLを使う場合があるが、基本はCDLLでOK
                self.lib = ctypes.CDLL(lib_path)
            
            # 3. 関数の型定義 (Pythonに引数の種類を教える)
            # void execute_render(NoteEvent* notes, int note_count, const char* output_path)
            self.lib.execute_render.argtypes = [
                ctypes.POINTER(NoteEvent), # 第1引数: NoteEventの配列(ポインタ)
                ctypes.c_int,              # 第2引数: ノート数
                ctypes.c_char_p            # 第3引数: 出力ファイル名
            ]
            self.lib.execute_render.restype = None # 戻り値なし(void)

            print(f"✅ VO-SE Engine (v1.0) Successfully loaded: {lib_path}")
            
        except Exception as e:
            print(f"❌ Failed to load engine or define functions: {e}")

    def render(self, notes_list: List[Dict[str, Any]], output_file: str = "output.wav") -> None:
        """
        Pythonのリストからエンジンを呼び出すヘルパー関数。
        Pyrightのエラー(reportUnknownParameterType等)を完全に解決し、メモリ安全性を確保します。
        """
        if not self.lib:
            print("❌ Engine not loaded.")
            return

        # 1. データの個数を確定 (int型であることを明示)
        count: int = len(notes_list)
        if count == 0:
            print("⚠️ No notes to render.")
            return

        # 2. C言語側の構造体配列を作成
        # 型ヒントを明示的に指定して Pyright の警告を回避
        NotesArrayType = NoteEvent * count
        notes_array = NotesArrayType()

        count: int = len(notes_list)
        NotesArray = NoteEvent * count
        notes_array = NotesArray()

        # 3. 前回のレンダリングで使用した一時メモリをクリア
        # self.keep_alive はクラスの __init__ で List[Any] として定義してください
        self.keep_alive = []

        for i, data in enumerate(notes_list):

            temp_list: List[Any] = [c_wav_path, c_pitch, c_gender, c_tension, c_breath]
            self.keep_alive.extend(temp_list)
            # 辞書からデータを取り出し、型をキャストして安全性を確保
            # これにより Pyright の "Argument type is unknown" を解決
            phoneme_str: str = str(data.get('phoneme', 'a'))
            pitch_list: List[float] = list(data.get('pitch', [150.0] * 50))
            gender_list: List[float] = list(data.get('gender', [0.5] * 50))
            tension_list: List[float] = list(data.get('tension', [0.5] * 50))
            breath_list: List[float] = list(data.get('breath', [0.0] * 50))

            # --- C言語用の型に変換 ---
            # 文字列をバイト列に変換
            c_wav_path = phoneme_str.encode('utf-8')
            
            # リストを C の double 配列に変換
            # ※ここで生成した配列オブジェクトを保持しておかないと、Cの関数実行前にGCされる
            c_pitch = (ctypes.c_double * len(pitch_list))(*pitch_list)
            c_gender = (ctypes.c_double * len(gender_list))(*gender_list)
            c_tension = (ctypes.c_double * len(tension_list))(*tension_list)
            c_breath = (ctypes.c_double * len(breath_list))(*breath_list)

            # メモリが解放されないようリストに保持
            self.keep_alive.extend([c_wav_path, c_pitch, c_gender, c_tension, c_breath])

            # 構造体へ代入
            notes_array[i].wav_path = c_wav_path
            notes_array[i].pitch_length = len(pitch_list)
            notes_array[i].pitch_curve = c_pitch
            notes_array[i].gender_curve = c_gender
            notes_array[i].tension_curve = c_tension
            notes_array[i].breath_curve = c_breath

        # 4. C言語のエンジンを実行
        # 文字列引数も encode して型を合わせる
        try:
            output_path_bytes = output_file.encode('utf-8')
            self.lib.execute_render(notes_array, count, output_path_bytes)
            print(f"🎬 Render complete: {output_file}")
        except Exception as e:
            print(f"❌ Execution error: {e}")
        
class TimelineHeader(QWidget):
    """
    タイムライン上部の時間目盛りを表示するウィジェット
    """
    positionChanged = Signal(int)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setStyleSheet("background-color: #333333;")
        self.playhead_x = 50  # 再生ヘッドの初期位置（ピクセル単位）
        self.is_dragging = False

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 目盛りの描画
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.setFont(QFont("Consolas", 8))
        for x in range(0, self.width(), 50):
            painter.drawLine(x, 20, x, 30)
            painter.drawText(x + 5, 15, f"{x // 50:02d}:00")

        # 赤い再生ヘッドの描画
        painter.setPen(QPen(QColor(255, 60, 60), 2))
        painter.drawLine(self.playhead_x, 0, self.playhead_x, 30)
        # 上部の三角形マーカー
        # 三角形マーカーの描画（可読性とRuff制限を考慮した分割）
        triangle = [
            QPoint(self.playhead_x - 5, 0),
            QPoint(self.playhead_x + 5, 0),
            QPoint(self.playhead_x, 8)
        ]
        
        painter.setBrush(QColor(255, 60, 60))
        painter.drawPolygon(triangle)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self.update_playhead(int(event.pos().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.is_dragging:
            self.update_playhead(int(event.pos().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.is_dragging = False

    def update_playhead(self, x: int) -> None:
        # 位置を制限して更新
        self.playhead_x = max(0, min(x, self.width()))
        self.update()
        self.positionChanged.emit(self.playhead_x)


class TimelineTrack(QFrame):
    """
    タイムラインの各トラック（音声、動画、モーション用）
    """
    def __init__(self, name: str, parent: QWidget | None = None) -> None:
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
        # トラック名の背景ラベル
        painter.fillRect(QRect(0, 0, 100, 60), QColor(45, 45, 45))
        painter.drawText(10, 35, self.track_name)
        painter.end()


class TimelineWidget(QWidget):
    """
    タイムライン全体の管理ウィジェット
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.main_layout: QVBoxLayout = QVBoxLayout(self)
        self.init_ui()

    def init_ui(self) -> None:
        # 1. 属性名を main_layout に変更してメソッドとの衝突を回避
        self.main_layout: QVBoxLayout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # 時間目盛り
        self.header = TimelineHeader()
        
        # トラックエリア
        self.scroll_content = QWidget()
        self.tracks_layout: QVBoxLayout = QVBoxLayout(self.scroll_content)
        self.tracks_layout.setContentsMargins(0, 0, 0, 0)
        self.tracks_layout.setSpacing(1)

        # 主要トラックの追加
        self.tracks_layout.addWidget(TimelineTrack("🎙️ VOICE"))
        self.tracks_layout.addWidget(TimelineTrack("🎬 VIDEO"))
        self.tracks_layout.addWidget(TimelineTrack("🦴 MOTION"))
        self.tracks_layout.addStretch()

        # スクロールバー
        self.h_scrollbar: QScrollBar = QScrollBar(Qt.Orientation.Horizontal)

        # 2. main_layout を使用してウィジェットを登録
        self.main_layout.addWidget(self.header)
        self.main_layout.addWidget(self.scroll_content)
        self.main_layout.addWidget(self.h_scrollbar)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)

class PreviewView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._scene_obj: QGraphicsScene = QGraphicsScene()
        self.setScene(self._scene_obj)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

        self.placeholder_text: QGraphicsTextItem = self._scene_obj.addText(
            "Preview Area (FFmpeg Output / Bone Overlay)"
        )
        self.placeholder_text.setDefaultTextColor(QColor(200, 200, 200))


class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio - Early Alpha")
        self.resize(1280, 800)

        # 型明示
        self.preview_stack: QStackedWidget = QStackedWidget()
        self.video_preview: PreviewView = PreviewView()
        self.motion_editor: PreviewView = PreviewView()

        # UI構築
        self.init_main_ui()

    def init_main_ui(self) -> None:
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- 左パネル ---
        self.left_panel = QFrame()
        self.left_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.addWidget(QLabel("📂 素材ライブラリ"))
        self.asset_list = QListWidget()
        self.left_layout.addWidget(self.asset_list)

        # TTS入力
        self.tts_input = QTextEdit()
        self.tts_input.setPlaceholderText("文章を入力...")
        self.tts_input.setMaximumHeight(80)
        self.generate_button = QPushButton("🎙️ 合成して配置")
        self.generate_button.clicked.connect(self.on_generate_clicked)
        self.left_layout.addWidget(self.tts_input)
        self.left_layout.addWidget(self.generate_button)

        # --- 中央パネル ---
        self.preview_stack.addWidget(self.video_preview)
        self.preview_stack.addWidget(self.motion_editor)

        # --- 右パネル ---
        self.right_panel = QFrame()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.btn_edit_mode = QPushButton("🎬 動画編集")
        self.btn_edit_mode.setCheckable(True)
        self.btn_edit_mode.setChecked(True)
        self.btn_edit_mode.clicked.connect(lambda: self.switch_mode(0))
        self.btn_motion_mode = QPushButton("🦴 モーション")
        self.btn_motion_mode.setCheckable(True)
        self.btn_motion_mode.clicked.connect(lambda: self.switch_mode(1))
        self.right_layout.addWidget(QLabel("🛠️ モード"))
        self.right_layout.addWidget(self.btn_edit_mode)
        self.right_layout.addWidget(self.btn_motion_mode)
        self.right_layout.addStretch()

        # 上部レイアウト結合
        self.horizontal_splitter.addWidget(self.left_panel)
        self.horizontal_splitter.addWidget(self.preview_stack)
        self.horizontal_splitter.addWidget(self.right_panel)
        self.horizontal_splitter.setStretchFactor(1, 6)

        # --- 下部パネル (タイムライン) ---
        self.timeline_container = QFrame()
        self.timeline_layout = QVBoxLayout(self.timeline_container)
        self.timeline_layout.setContentsMargins(0, 0, 0, 0)
        
        # 魂を込めたタイムラインウィジェットをここに配置
        self.timeline_widget = TimelineWidget()
        self.timeline_layout.addWidget(self.timeline_widget)

        # 全体結合
        self.vertical_splitter.addWidget(self.horizontal_splitter)
        self.vertical_splitter.addWidget(self.timeline_container)
        self.vertical_splitter.setStretchFactor(0, 6)
        self.vertical_splitter.setStretchFactor(1, 4)

        self.main_layout.addWidget(self.vertical_splitter)

    def switch_mode(self, index: int) -> None:
        self.preview_stack.setCurrentIndex(index)
        self.btn_edit_mode.setChecked(index == 0)
        self.btn_motion_mode.setChecked(index == 1)

    def on_generate_clicked(self) -> None:
        text = self.tts_input.toPlainText()
        if text.strip():
            print(f"DEBUG: {text} の合成リクエストを受理。")
            self.tts_input.clear()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = CutStudioMain()
    window.show()
    sys.exit(app.exec())
