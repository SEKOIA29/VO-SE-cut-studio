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
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush, QPainter


class PreviewView(QGraphicsView):
    """
    プレビュー兼キャンバス。
    編集モードでは動画を表示し、モーションモードではその上にボーンをオーバーレイ描画する。
    """

    def __init__(self) -> None:
        super().__init__()
        # Pyrightエラー回避: 親クラスのメソッド名 scene() と衝突しないよう名前を変更
        self._scene_obj: QGraphicsScene = QGraphicsScene()
        self.setScene(self._scene_obj)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))  # 背景は黒に近いグレー
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

        # モック用のテキスト
        self.placeholder_text: QGraphicsTextItem = self._scene_obj.addText(
            "Preview Area (FFmpeg Output / Bone Overlay)"
        )
        self.placeholder_text.setDefaultTextColor(QColor(200, 200, 200))


class CutStudioMain(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VO-SE Cut Studio - Early Alpha")
        self.resize(1280, 720)

        # --- [属性の定義] Pyright用の型明示 ---
        self.preview_stack: QStackedWidget = QStackedWidget()
        self.video_preview: PreviewView = PreviewView()
        self.motion_editor: PreviewView = PreviewView()

        # メインウィジェット
        self.central_widget: QWidget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout: QVBoxLayout = QVBoxLayout(self.central_widget)

        # --- [上下分割] メインエリア(上) / タイムラインエリア(下) ---
        self.vertical_splitter: QSplitter = QSplitter(Qt.Orientation.Vertical)

        # --- 上部パネル構成 (左:素材 / 中:プレビュー / 右:メニュー) ---
        self.upper_container: QWidget = QWidget()
        self.upper_layout: QHBoxLayout = QHBoxLayout(self.upper_container)
        self.upper_layout.setContentsMargins(0, 0, 0, 0)

        self.horizontal_splitter: QSplitter = QSplitter(Qt.Orientation.Horizontal)

        # 1. 左パネル: 素材・テンプレート・音声合成入力
        self.left_panel: QFrame = QFrame()
        self.left_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        self.left_layout: QVBoxLayout = QVBoxLayout(self.left_panel)

        self.left_layout.addWidget(QLabel("📂 素材・テロップテンプレート"))
        self.asset_list: QListWidget = QListWidget()
        self.left_layout.addWidget(self.asset_list)

        # 音声合成入力エリア (代表の設計図の核)
        self.tts_container: QWidget = QWidget()
        self.tts_layout: QVBoxLayout = QVBoxLayout(self.tts_container)
        self.tts_layout.setContentsMargins(0, 10, 0, 0)

        self.tts_input: QTextEdit = QTextEdit()
        self.tts_input.setPlaceholderText("ここに文章を入力し、Enterで合成・配置...")
        self.tts_input.setMaximumHeight(80)

        self.generate_button: QPushButton = QPushButton("🎙️ 音声合成して配置")
        self.generate_button.setMinimumHeight(40)
        self.generate_button.clicked.connect(self.on_generate_clicked)

        self.tts_layout.addWidget(self.tts_input)
        self.tts_layout.addWidget(self.generate_button)
        self.left_layout.addWidget(self.tts_container)

        # 2. 中央パネル: プレビュー (QStackedWidgetでモード切り替えに対応)
        self.preview_stack.addWidget(self.video_preview)
        self.preview_stack.addWidget(self.motion_editor)

        # 3. 右パネル: メニュー・モード切り替え
        self.right_panel: QFrame = QFrame()
        self.right_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        self.right_layout: QVBoxLayout = QVBoxLayout(self.right_panel)
        self.right_layout.setSpacing(10)

        self.right_layout.addWidget(QLabel("🛠️ メニュー / モード"))

        self.btn_edit_mode: QPushButton = QPushButton("🎬 動画編集モード")
        self.btn_edit_mode.setCheckable(True)
        self.btn_edit_mode.setChecked(True)
        self.btn_edit_mode.clicked.connect(lambda: self.switch_mode(0))

        self.btn_motion_mode: QPushButton = QPushButton("🦴 モーションモード")
        self.btn_motion_mode.setCheckable(True)
        self.btn_motion_mode.clicked.connect(lambda: self.switch_mode(1))

        self.right_layout.addWidget(self.btn_edit_mode)
        self.right_layout.addWidget(self.btn_motion_mode)

        self.right_layout.addStretch()  # 下部にスペースを確保

        # 横分割スプリッターに追加
        self.horizontal_splitter.addWidget(self.left_panel)
        self.horizontal_splitter.addWidget(self.preview_stack)
        self.horizontal_splitter.addWidget(self.right_panel)

        # 初期サイズ設定 (左2:中6:右2)
        self.horizontal_splitter.setStretchFactor(0, 2)
        self.horizontal_splitter.setStretchFactor(1, 6)
        self.horizontal_splitter.setStretchFactor(2, 2)

        # --- 下部パネル: タイムライン ---
        self.timeline_container: QFrame = QFrame()
        self.timeline_container.setFrameStyle(QFrame.Shape.StyledPanel)
        self.timeline_layout: QVBoxLayout = QVBoxLayout(self.timeline_container)
        self.timeline_layout.addWidget(QLabel("🎞️ タイムライン / グラフエディタ"))

        # タイムラインエリア
        self.timeline_area: QFrame = QFrame()
        self.timeline_area.setStyleSheet("background-color: #1a1a1a;")
        self.timeline_area.setMinimumHeight(250)
        self.timeline_layout.addWidget(self.timeline_area)

        # 縦分割スプリッターに上下を統合
        self.vertical_splitter.addWidget(self.horizontal_splitter)
        self.vertical_splitter.addWidget(self.timeline_container)
        self.vertical_splitter.setStretchFactor(0, 7)
        self.vertical_splitter.setStretchFactor(1, 3)

        self.main_layout.addWidget(self.vertical_splitter)

    def switch_mode(self, index: int) -> None:
        """
        0: 動画編集モード / 1: モーションモード
        """
        self.preview_stack.setCurrentIndex(index)
        self.btn_edit_mode.setChecked(index == 0)
        self.btn_motion_mode.setChecked(index == 1)

        mode_name = "動画編集モード" if index == 0 else "モーションモード"
        print(f"モード切り替え: {mode_name}")

    def on_generate_clicked(self) -> None:
        """音声合成ボタンが押された時の処理（フェーズ1の核）"""
        text = self.tts_input.toPlainText()
        if text.strip():
            print(f"音声合成開始: {text}")
            # ここで SpeechEngine (Open JTalk + ONNX) を呼び出す予定
            self.tts_input.clear()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # ダークテーマ的な配色を設定
    app.setStyle("Fusion")

    window = CutStudioMain()
    window.show()
    sys.exit(app.exec())
