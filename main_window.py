import sys
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
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QBrush, QPainter, QPen, QFont


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

    def paintEvent(self, event) -> None:
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
        painter.setBrush(QColor(255, 60, 60))
        painter.drawPolygon([QPoint(self.playhead_x - 5, 0), QPoint(self.playhead_x + 5, 0), QPoint(self.playhead_x, 8)])
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self.update_playhead(event.pos().x())

    def mouseMoveEvent(self, event) -> None:
        if self.is_dragging:
            self.update_playhead(event.pos().x())

    def mouseReleaseEvent(self, event) -> None:
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

    def paintEvent(self, event) -> None:
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
        self.init_ui()

    def init_ui(self) -> None:
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        # 時間目盛り
        self.header = TimelineHeader()
        
        # トラックエリア
        self.scroll_content = QWidget()
        self.tracks_layout = QVBoxLayout(self.scroll_content)
        self.tracks_layout.setContentsMargins(0, 0, 0, 0)
        self.tracks_layout.setSpacing(1)

        # 代表、主要な3トラックを配置しました
        self.tracks_layout.addWidget(TimelineTrack("🎙️ VOICE"))
        self.tracks_layout.addWidget(TimelineTrack("🎬 VIDEO"))
        self.tracks_layout.addWidget(TimelineTrack("🦴 MOTION"))
        self.tracks_layout.addStretch()

        # スクロールバー
        self.h_scrollbar = QScrollBar(Qt.Orientation.Horizontal)

        self.layout.addWidget(self.header)
        self.layout.addWidget(self.scroll_content)
        self.layout.addWidget(self.h_scrollbar)


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
