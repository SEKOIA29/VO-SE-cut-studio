# video_engine.py
import ctypes
import os
from typing import Optional, List


class VideoEngine:
    def __init__(self, lib_path: str = "./libvideo_engine.dylib") -> None:
        if not os.path.exists(lib_path):
            # クラッシュではなく警告にとどめ、メソッド呼び出し時に安全に失敗させる
            print(f"⚠️  Engine library not found: {lib_path}")
            self.lib = None
            self.handle = None
            return

        self.lib = ctypes.CDLL(lib_path)
        self._setup_signatures()
        self.handle = self.lib.vose_create()

    # ------------------------------------------------------------------
    # C 関数シグネチャの一括定義
    # ------------------------------------------------------------------
    def _setup_signatures(self) -> None:
        lib = self.lib

        lib.vose_create.argtypes = []
        lib.vose_create.restype  = ctypes.c_void_p

        lib.vose_destroy.argtypes = [ctypes.c_void_p]
        lib.vose_destroy.restype  = None                     # ← 追加（必須）

        lib.vose_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.vose_load.restype  = ctypes.c_int

        lib.vose_duration.argtypes = [ctypes.c_void_p]
        lib.vose_duration.restype  = ctypes.c_double

        lib.vose_width.argtypes  = [ctypes.c_void_p]
        lib.vose_width.restype   = ctypes.c_int

        lib.vose_height.argtypes = [ctypes.c_void_p]
        lib.vose_height.restype  = ctypes.c_int

        lib.vose_fps.argtypes = [ctypes.c_void_p]
        lib.vose_fps.restype  = ctypes.c_double

        lib.vose_has_audio.argtypes = [ctypes.c_void_p]
        lib.vose_has_audio.restype  = ctypes.c_int

        lib.vose_save_frame.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_char_p]
        lib.vose_save_frame.restype  = ctypes.c_int

        lib.vose_waveform.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.vose_waveform.restype = ctypes.c_int

        lib.vose_export_edl.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.vose_export_edl.restype  = ctypes.c_int

        lib.vose_export_hw.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int
        ]
        lib.vose_export_hw.restype = ctypes.c_int

        lib.vose_build_keyframe_index.argtypes = [ctypes.c_void_p]
        lib.vose_build_keyframe_index.restype  = ctypes.c_int

        lib.vose_nearest_keyframe.argtypes = [ctypes.c_void_p, ctypes.c_double]
        lib.vose_nearest_keyframe.restype  = ctypes.c_double

    # ------------------------------------------------------------------
    # ガード用ヘルパー
    # ------------------------------------------------------------------
    def _ok(self) -> bool:
        return self.lib is not None and self.handle is not None

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------
    def load_video(self, path: str) -> bool:
        if not self._ok():
            return False
        return self.lib.vose_load(self.handle, path.encode("utf-8")) == 1

    @property
    def duration(self) -> float:
        return self.lib.vose_duration(self.handle) if self._ok() else 0.0

    @property
    def width(self) -> int:
        return self.lib.vose_width(self.handle) if self._ok() else 0

    @property
    def height(self) -> int:
        return self.lib.vose_height(self.handle) if self._ok() else 0

    @property
    def fps(self) -> float:
        return self.lib.vose_fps(self.handle) if self._ok() else 0.0

    @property
    def has_audio(self) -> bool:
        return self._ok() and self.lib.vose_has_audio(self.handle) == 1

    def save_preview(self, time_sec: float, output_path: str) -> bool:
        if not self._ok():
            return False
        return self.lib.vose_save_frame(
            self.handle, time_sec, output_path.encode("utf-8")
        ) == 1

    def extract_waveform(self, chunks: int = 512) -> List[float]:
        """ピーク振幅の配列を返す（chunks 個）"""
        if not self._ok():
            return []
        buf = (ctypes.c_float * chunks)()
        n = self.lib.vose_waveform(self.handle, buf, chunks, chunks)
        return list(buf[:n])

    def export_edl(self, edl_json: str, out_path: str) -> bool:
        if not self._ok():
            return False
        return self.lib.vose_export_edl(
            self.handle, edl_json.encode("utf-8"), out_path.encode("utf-8")
        ) == 1

    def export_hw(self, edl_json: str, out_path: str, quality: int = 23) -> bool:
        """Apple VideoToolbox（または libx264）でエンコードしてエクスポート"""
        if not self._ok():
            return False
        return self.lib.vose_export_hw(
            self.handle, edl_json.encode("utf-8"), out_path.encode("utf-8"), quality
        ) == 1

    def build_keyframe_index(self) -> int:
        """キーフレームインデックスを構築し、検出数を返す"""
        if not self._ok():
            return 0
        return self.lib.vose_build_keyframe_index(self.handle)

    def nearest_keyframe(self, time_sec: float) -> float:
        """指定時刻以前の最近傍キーフレーム時刻を返す"""
        if not self._ok():
            return time_sec
        return self.lib.vose_nearest_keyframe(self.handle, time_sec)

    # ------------------------------------------------------------------
    def __del__(self) -> None:
        if self._ok():
            self.lib.vose_destroy(self.handle)
            self.handle = None
