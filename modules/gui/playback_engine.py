"""
playback_engine.py
VO-SE Cut Studio — 再生エンジン

設計方針:
  - デコードスレッド(QThread) が AVFrame を非同期にデコードしてキューに積む
  - QTimer(display_timer) が ~16ms ごとに起動し、キューから1フレームを取り出して
    PreviewView に表示する（映像同期）
  - 音声は PyAudio + threading.Thread でデコードキューから並走再生
  - タイムライン再生ヘッドは positionChanged シグナル経由で同期

使い方:
  from playback_engine import PlaybackEngine

  engine = PlaybackEngine(preview_view, timeline_header, status_bar)
  engine.load("movie.mp4")
  engine.play()   # ▶
  engine.pause()  # ⏸
  engine.stop()   # ⏹
  engine.seek(3.5)  # 秒単位シーク
"""
from __future__ import annotations

import time
import queue
import threading
import traceback
from typing import Optional, Callable

# ──────────────────────────────────────────────────────────────────
# PyAV (python-av) — FFmpeg バインディング
# ──────────────────────────────────────────────────────────────────
try:
    import av                          # pip install av
    _AV_AVAILABLE = True
except ImportError:
    _AV_AVAILABLE = False
    print("⚠️  PyAV not found. Video playback disabled. (pip install av)")

# ──────────────────────────────────────────────────────────────────
# PyAudio — 音声出力
# ──────────────────────────────────────────────────────────────────
try:
    import pyaudio                     # pip install pyaudio
    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False
    print("⚠️  PyAudio not found. Audio playback disabled. (pip install pyaudio)")

from PySide6.QtCore import (
    QObject, QThread, QTimer, Signal, Slot, Qt, QMutex, QMutexLocker,
)
from PySide6.QtGui  import QPixmap, QImage
from PySide6.QtWidgets import QStatusBar


# ══════════════════════════════════════════════════════════════════
# 定数
# ══════════════════════════════════════════════════════════════════

_DISPLAY_INTERVAL_MS  = 16      # ~60fps 表示ポーリング間隔
_VIDEO_QUEUE_MAX      = 8       # デコード先読みフレーム数
_AUDIO_QUEUE_MAX      = 16      # 音声チャンクバッファ数
_AUDIO_CHUNK_FRAMES   = 1024    # PyAudio コールバック単位
_SEEK_FLUSH_TIMEOUT   = 0.5     # シーク後フラッシュ待機(秒)


# ══════════════════════════════════════════════════════════════════
# DecoderWorker — QThread でデコードを非同期実行
# ══════════════════════════════════════════════════════════════════

class DecoderWorker(QObject):
    """
    PyAV を使って映像・音声をデコードし、それぞれのキューに積む。
    QThread::start() で起動、stop_event で停止。
    """

    # (pts_sec, QImage)
    frame_decoded   = Signal(float, object)   # 映像フレーム → display_timer で受信
    seek_done       = Signal(float)            # シーク完了通知
    error_occurred  = Signal(str)

    def __init__(
        self,
        video_queue: queue.Queue,
        audio_queue: queue.Queue,
        stop_event:  threading.Event,
        seek_event:  threading.Event,
    ) -> None:
        super().__init__()
        self.video_queue   = video_queue
        self.audio_queue   = audio_queue
        self.stop_event    = stop_event
        self.seek_event    = seek_event
        self.file_path:    Optional[str]   = None
        self.seek_target:  float           = 0.0
        self._container:   Optional[object] = None   # av.container
        self._video_stream = None
        self._audio_stream = None
        self._paused        = False
        self._mutex         = QMutex()

    # ── 外部から呼ぶ API ──────────────────────────────────────────

    def set_file(self, path: str) -> None:
        with QMutexLocker(self._mutex):
            self.file_path = path

    def request_seek(self, sec: float) -> None:
        with QMutexLocker(self._mutex):
            self.seek_target = sec
        self.seek_event.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    # ── メインループ ─────────────────────────────────────────────

    @Slot()
    def run(self) -> None:
        if not _AV_AVAILABLE or not self.file_path:
            return
        try:
            self._container = av.open(self.file_path)
        except Exception as e:
            self.error_occurred.emit(f"デコード失敗: {e}")
            return

        container = self._container
        streams = container.streams

        try:
            self._video_stream = streams.video[0]
            self._video_stream.thread_type = "AUTO"
        except (IndexError, AttributeError):
            self._video_stream = None

        try:
            self._audio_stream = streams.audio[0]
        except (IndexError, AttributeError):
            self._audio_stream = None

        selected = [s for s in [self._video_stream, self._audio_stream] if s]
        if not selected:
            self.error_occurred.emit("映像・音声ストリームが見つかりません")
            return

        for packet in container.demux(*selected):
            # ── 停止チェック ──
            if self.stop_event.is_set():
                break

            # ── シークリクエスト ──
            if self.seek_event.is_set():
                self.seek_event.clear()
                target = self.seek_target
                try:
                    container.seek(int(target * av.time_base ** -1),
                                   any_frame=False, backward=True)
                except Exception:
                    pass
                # キューをフラッシュ
                while not self.video_queue.empty():
                    try: self.video_queue.get_nowait()
                    except queue.Empty: break
                while not self.audio_queue.empty():
                    try: self.audio_queue.get_nowait()
                    except queue.Empty: break
                self.seek_done.emit(target)

            # ── ポーズ中はスリープして待機 ──
            while self._paused and not self.stop_event.is_set():
                time.sleep(0.02)
            if self.stop_event.is_set():
                break

            # ── デコード ──
            try:
                for frame in packet.decode():
                    if self.stop_event.is_set():
                        return

                    if packet.stream == self._video_stream:
                        # NumPy → QImage 変換
                        img = self._frame_to_qimage(frame)
                        pts = float(frame.pts * frame.time_base) if frame.pts else 0.0
                        # キューが満杯なら古いフレームを捨てて最新優先
                        if self.video_queue.full():
                            try: self.video_queue.get_nowait()
                            except queue.Empty: pass
                        self.video_queue.put((pts, img), block=False)

                    elif packet.stream == self._audio_stream:
                        # interleaved int16 PCM に変換
                        resampled = frame.to_ndarray(format="s16", layout="stereo")
                        pts = float(frame.pts * frame.time_base) if frame.pts else 0.0
                        if not self.audio_queue.full():
                            self.audio_queue.put((pts, resampled.tobytes()), block=False)

            except av.AVError:
                continue

        # EOF — sentinel を積んで再生完了を通知
        self.video_queue.put(None)

    @staticmethod
    def _frame_to_qimage(frame) -> QImage:
        """av.VideoFrame → QImage (RGB888)"""
        rgb = frame.to_rgb()
        arr = rgb.to_ndarray()          # (H, W, 3) uint8
        h, w, _ = arr.shape
        return QImage(arr.tobytes(), w, h,
                      w * 3, QImage.Format.Format_RGB888).copy()


# ══════════════════════════════════════════════════════════════════
# AudioPlayer — threading.Thread で PyAudio 出力
# ══════════════════════════════════════════════════════════════════

class AudioPlayer:
    def __init__(self, audio_queue: queue.Queue) -> None:
        self.audio_queue = audio_queue
        self._thread: Optional[threading.Thread] = None
        self._stop    = threading.Event()
        self._paused  = threading.Event()
        self._pa: Optional[object] = None
        self._stream  = None

    def start(self, sample_rate: int = 44100, channels: int = 2) -> None:
        if not _AUDIO_AVAILABLE:
            return
        self._stop.clear()
        self._paused.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(sample_rate, channels),
            daemon=True,
        )
        self._thread.start()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        self._paused.clear()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self, sample_rate: int, channels: int) -> None:
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                output=True,
                frames_per_buffer=_AUDIO_CHUNK_FRAMES,
            )
            while not self._stop.is_set():
                # ポーズ中
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.02)
                if self._stop.is_set():
                    break
                try:
                    pts, pcm_bytes = self.audio_queue.get(timeout=0.1)
                    stream.write(pcm_bytes)
                except queue.Empty:
                    continue
                except Exception:
                    break
            stream.stop_stream()
            stream.close()
            pa.terminate()
        except Exception as e:
            print(f"⚠️  AudioPlayer error: {e}")


# ══════════════════════════════════════════════════════════════════
# PlaybackEngine — UIに公開するメインクラス
# ══════════════════════════════════════════════════════════════════

class PlaybackEngine(QObject):
    """
    使い方:
        engine = PlaybackEngine(preview_view, timeline_header, status_bar)
        engine.load("movie.mp4")
        engine.play()
    """

    playback_started  = Signal()
    playback_paused   = Signal()
    playback_stopped  = Signal()
    position_updated  = Signal(float)   # 秒
    duration_known    = Signal(float)   # ロード時に1回
    error_occurred    = Signal(str)

    def __init__(
        self,
        preview_view,       # PreviewView インスタンス
        timeline_header,    # TimelineHeader インスタンス
        status_bar: Optional[QStatusBar] = None,
    ) -> None:
        super().__init__()

        self._preview       = preview_view
        self._header        = timeline_header
        self._status        = status_bar

        self._file_path:    Optional[str]  = None
        self._duration:     float          = 0.0
        self._position:     float          = 0.0
        self._playing:      bool           = False
        self._px_per_sec:   int            = 100       # タイムラインのスケール

        # キュー
        self._video_queue:  queue.Queue    = queue.Queue(maxsize=_VIDEO_QUEUE_MAX)
        self._audio_queue:  queue.Queue    = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)

        # 停止・シークイベント
        self._stop_event    = threading.Event()
        self._seek_event    = threading.Event()

        # デコーダスレッド
        self._decoder_thread: Optional[QThread]       = None
        self._decoder_worker: Optional[DecoderWorker] = None

        # 音声プレーヤー
        self._audio_player  = AudioPlayer(self._audio_queue)

        # 表示タイマー(~60fps)
        self._display_timer = QTimer(self)
        self._display_timer.setInterval(_DISPLAY_INTERVAL_MS)
        self._display_timer.timeout.connect(self._on_display_tick)

        # 再生開始時刻（ウォールクロック同期用）
        self._play_start_wall: float = 0.0
        self._play_start_pts:  float = 0.0

    # ── 公開 API ─────────────────────────────────────────────────

    def load(self, file_path: str) -> bool:
        """動画ファイルをロードして準備する。"""
        self.stop()
        if not _AV_AVAILABLE:
            self._show_status("❌  PyAV が必要です: pip install av")
            return False

        try:
            container = av.open(file_path)
            dur = float(container.duration) / 1_000_000  # AV_TIME_BASE = 1e6
            container.close()
        except Exception as e:
            self._show_status(f"❌  ロード失敗: {e}")
            return False

        self._file_path = file_path
        self._duration  = dur
        self._position  = 0.0
        self.duration_known.emit(dur)
        self._show_status(f"📂  読み込み完了: {file_path}  ({dur:.1f}s)")
        return True

    def play(self) -> None:
        """再生開始 / ポーズ解除"""
        if not self._file_path:
            return

        if self._playing:
            return

        self._playing = True

        if self._decoder_worker and not self._stop_event.is_set():
            # ポーズ解除
            self._decoder_worker.set_paused(False)
            self._audio_player.resume()
        else:
            # 新規再生
            self._start_decoder()
            self._audio_player.start()

        self._play_start_wall = time.monotonic()
        self._play_start_pts  = self._position
        self._display_timer.start()
        self.playback_started.emit()
        self._show_status("▶  再生中")

    def pause(self) -> None:
        """一時停止"""
        if not self._playing:
            return
        self._playing = False
        if self._decoder_worker:
            self._decoder_worker.set_paused(True)
        self._audio_player.pause()
        self._display_timer.stop()
        self.playback_paused.emit()
        self._show_status(f"⏸  一時停止  {self._fmt_time(self._position)}")

    def stop(self) -> None:
        """完全停止・リソース解放"""
        self._playing = False
        self._display_timer.stop()
        self._stop_event.set()
        self._audio_player.stop()

        if self._decoder_thread:
            self._decoder_thread.quit()
            self._decoder_thread.wait(2000)
            self._decoder_thread = None
            self._decoder_worker = None

        # キューをフラッシュ
        for q in (self._video_queue, self._audio_queue):
            while not q.empty():
                try: q.get_nowait()
                except queue.Empty: break

        self._stop_event.clear()
        self._seek_event.clear()
        self._position = 0.0
        self._update_header(0.0)
        self.playback_stopped.emit()
        self._show_status("⏹  停止")

    def seek(self, sec: float) -> None:
        """指定秒数にシーク"""
        sec = max(0.0, min(sec, self._duration))
        self._position       = sec
        self._play_start_pts = sec
        self._play_start_wall = time.monotonic()
        self._update_header(sec)

        if self._decoder_worker:
            self._decoder_worker.request_seek(sec)
        self._show_status(f"⏩  シーク: {self._fmt_time(sec)}")

    def step_forward(self, sec: float = 5.0) -> None:
        self.seek(self._position + sec)

    def step_backward(self, sec: float = 5.0) -> None:
        self.seek(self._position - sec)

    def go_to_start(self) -> None:
        self.seek(0.0)

    def go_to_end(self) -> None:
        self.seek(self._duration)

    @property
    def position(self) -> float:
        return self._position

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def is_playing(self) -> bool:
        return self._playing

    # ── 内部: デコーダ起動 ───────────────────────────────────────

    def _start_decoder(self) -> None:
        self._stop_event.clear()

        self._decoder_worker = DecoderWorker(
            self._video_queue,
            self._audio_queue,
            self._stop_event,
            self._seek_event,
        )
        self._decoder_worker.set_file(self._file_path)
        if self._position > 0.0:
            self._decoder_worker.seek_target = self._position

        self._decoder_thread = QThread(self)
        self._decoder_worker.moveToThread(self._decoder_thread)
        self._decoder_thread.started.connect(self._decoder_worker.run)
        self._decoder_worker.error_occurred.connect(self._on_decoder_error)
        self._decoder_thread.start()

    # ── 内部: 表示タイマーコールバック(~60fps) ──────────────────

    @Slot()
    def _on_display_tick(self) -> None:
        if not self._playing:
            return

        # ウォールクロックで現在位置を推定
        elapsed = time.monotonic() - self._play_start_wall
        self._position = self._play_start_pts + elapsed

        # EOF チェック
        if self._duration > 0 and self._position >= self._duration:
            self.stop()
            return

        # 映像キューから最新フレームを取り出す
        frame_item = None
        while True:
            try:
                item = self._video_queue.get_nowait()
                if item is None:
                    # EOF sentinel
                    self.stop()
                    return
                pts, img = item
                # PTSがウォールクロックより未来ならキューに戻して待つ
                if pts > self._position + 0.033:
                    self.video_queue.put((pts, img))
                    break
                frame_item = (pts, img)
            except queue.Empty:
                break

        if frame_item is not None:
            _, img = frame_item
            self._show_frame(img)

        # タイムラインヘッドを更新
        self._update_header(self._position)
        self.position_updated.emit(self._position)

    # ── 内部: フレームをPreviewViewに表示 ───────────────────────

    def _show_frame(self, img: QImage) -> None:
        """QImage を PreviewView の QGraphicsScene に表示する"""
        scene = self._preview._scene

        # 既存の動画フレームアイテムを削除 (PixmapItem のみ)
        for item in scene.items():
            from PySide6.QtWidgets import QGraphicsPixmapItem
            if isinstance(item, QGraphicsPixmapItem):
                if getattr(item, "_is_playback_frame", False):
                    scene.removeItem(item)
                    break

        pix  = QPixmap.fromImage(img)
        item = scene.addPixmap(pix)
        item._is_playback_frame = True  # type: ignore[attr-defined]

        # 1920×1080 キャンバスに中央配置
        x = (1920 - img.width())  / 2
        y = (1080 - img.height()) / 2
        item.setPos(x, y)
        item.setZValue(-1)  # キャラクター等の下に配置

    # ── 内部: タイムラインヘッド同期 ─────────────────────────────

    def _update_header(self, sec: float) -> None:
        px = int(sec * self._px_per_sec)
        self._header.set_playhead(px)

    # ── 内部: エラー処理 ─────────────────────────────────────────

    @Slot(str)
    def _on_decoder_error(self, msg: str) -> None:
        self.stop()
        self._show_status(f"❌  {msg}")
        self.error_occurred.emit(msg)

    # ── ユーティリティ ────────────────────────────────────────────

    def _show_status(self, msg: str) -> None:
        if self._status:
            self._status.showMessage(msg)
        print(f"[PlaybackEngine] {msg}")

    @staticmethod
    def _fmt_time(sec: float) -> str:
        m = int(sec) // 60
        s = int(sec) % 60
        ms = int((sec - int(sec)) * 10)
        return f"{m:02d}:{s:02d}.{ms}"


# ══════════════════════════════════════════════════════════════════
# TransportController — ツールバーボタンと PlaybackEngine を接続
# ══════════════════════════════════════════════════════════════════

class TransportController(QObject):
    """
    main_window.py の _make_toolbar() で生成したボタンと
    PlaybackEngine を結線するヘルパークラス。

    使い方:
        self.transport = TransportController(
            engine   = self.playback_engine,
            btn_top  = btn_go_start,
            btn_prev = btn_step_back,
            btn_play = btn_play,
            btn_next = btn_step_fwd,
            btn_end  = btn_go_end,
        )
    """

    def __init__(
        self,
        engine,
        btn_top,
        btn_prev,
        btn_play,
        btn_next,
        btn_end,
        export_btn=None,
        export_callback: Optional[Callable] = None,
    ) -> None:
        super().__init__()
        self._engine   = engine
        self._btn_play = btn_play
        self._is_playing = False

        btn_top .clicked.connect(engine.go_to_start)
        btn_prev.clicked.connect(engine.step_backward)
        btn_play.clicked.connect(self._toggle_play)
        btn_next.clicked.connect(engine.step_forward)
        btn_end .clicked.connect(engine.go_to_end)

        if export_btn and export_callback:
            export_btn.clicked.connect(export_callback)

        engine.playback_started.connect(lambda: self._set_play_icon(True))
        engine.playback_paused .connect(lambda: self._set_play_icon(False))
        engine.playback_stopped.connect(lambda: self._set_play_icon(False))

    def _toggle_play(self) -> None:
        if self._engine.is_playing:
            self._engine.pause()
        else:
            self._engine.play()

    def _set_play_icon(self, playing: bool) -> None:
        self._is_playing = playing
        self._btn_play.setText("⏸" if playing else "▶")


# ══════════════════════════════════════════════════════════════════
# main_window.py への統合パッチ
# ══════════════════════════════════════════════════════════════════
#
# main_window.py の CutStudioMain.__init__ / _make_toolbar に
# 以下の変更を加えるだけで再生エンジンが動作します。
#
# ① __init__ の末尾に追記:
#
#     from playback_engine import PlaybackEngine, TransportController
#
#     self.playback_engine = PlaybackEngine(
#         preview_view    = self.video_preview,
#         timeline_header = self.timeline.header,
#         status_bar      = self._status,
#     )
#     # サンプルとして最初に見つかった動画を自動ロード（任意）
#     # self.playback_engine.load("sample.mp4")
#
# ② _make_toolbar の末尾（return bar の直前）に追記:
#
#     # ボタンを名前で取得するためにインスタンス変数化する
#     # ※ _make_toolbar 内の controls ループを以下に置き換え:
#     #
#     #   btn_labels  = [("⏮","先頭"), ("⏪","戻る"), ("▶","再生"),
#     #                  ("⏩","進む"), ("⏭","末尾")]
#     #   self._transport_btns = []
#     #   for icon, tip in btn_labels:
#     #       btn = QPushButton(icon)
#     #       btn.setToolTip(tip)
#     #       btn.setStyleSheet(btn_style)
#     #       btn.setFixedSize(32, 28)
#     #       layout.addWidget(btn)
#     #       self._transport_btns.append(btn)
#     #
#     # _init_ui で呼ぶ: (toolbar を先に作る必要があるため)
#
#     self.transport = TransportController(
#         engine      = self.playback_engine,
#         btn_top     = self._transport_btns[0],
#         btn_prev    = self._transport_btns[1],
#         btn_play    = self._transport_btns[2],
#         btn_next    = self._transport_btns[3],
#         btn_end     = self._transport_btns[4],
#         export_btn  = export_btn,
#         export_callback = self._on_export_clicked,
#     )
#
# ③ _on_export_clicked を追加:
#
#     def _on_export_clicked(self) -> None:
#         from PySide6.QtWidgets import QFileDialog
#         path, _ = QFileDialog.getSaveFileName(
#             self, "書き出し先", "output.mp4", "MP4 (*.mp4);;MOV (*.mov)"
#         )
#         if not path:
#             return
#         self._status.showMessage(f"📤  書き出し中: {path}")
#         # EDL生成・VideoEngine.export_hw() 呼び出しはここに実装
#
# ④ 素材ライブラリの QListWidget をダブルクリックで動画ロード:
#
#     self.asset_list.itemDoubleClicked.connect(
#         lambda item: self.playback_engine.load(item.data(Qt.ItemDataRole.UserRole))
#     )
#
# ══════════════════════════════════════════════════════════════════
