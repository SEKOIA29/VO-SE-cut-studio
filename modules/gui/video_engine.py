import ctypes
import os
from typing import Optional, List

class VideoEngine:
    def __init__(self, lib_path: str = "./libvideo_engine.dylib") -> None:
        self.lib: Optional[ctypes.CDLL] = None
        self.handle: Optional[ctypes.c_void_p] = None

        # 1. まずファイルの存在を確認
        if not os.path.exists(lib_path):
            print(f"⚠️  Engine library not found: {lib_path}")
            return

        try:
            # 2. ロードを試行
            lib = ctypes.CDLL(lib_path)
            # 3. 関数シグネチャを確定（self.lib をセットする前に設定を済ませる）
            self._setup_signatures(lib)
            
            self.lib = lib
            self.handle = lib.vose_create()
        except Exception as e:
            print(f"⚠️  Failed to load engine: {e}")

    def _setup_signatures(self, lib: ctypes.CDLL) -> None:
        """引数として受け取った lib に対してシグネチャを定義する"""
        lib.vose_create.argtypes = []
        lib.vose_create.restype  = ctypes.c_void_p

        lib.vose_destroy.argtypes = [ctypes.c_void_p]
        lib.vose_destroy.restype  = None

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
    # 公開 API (Walrus Operator で型チェックと実行を同時に行う)
    # ------------------------------------------------------------------
    def load_video(self, path: str) -> bool:
        if (lib := self.lib) and (h := self.handle):
            return lib.vose_load(h, path.encode("utf-8")) == 1
        return False

    @property
    def duration(self) -> float:
        if (lib := self.lib) and (h := self.handle):
            return float(lib.vose_duration(h))
        return 0.0

    @property
    def width(self) -> int:
        if (lib := self.lib) and (h := self.handle):
            return int(lib.vose_width(h))
        return 0

    @property
    def height(self) -> int:
        if (lib := self.lib) and (h := self.handle):
            return int(lib.vose_height(h))
        return 0

    @property
    def fps(self) -> float:
        if (lib := self.lib) and (h := self.handle):
            return float(lib.vose_fps(h))
        return 0.0

    @property
    def has_audio(self) -> bool:
        if (lib := self.lib) and (h := self.handle):
            return lib.vose_has_audio(h) == 1
        return False

    def save_preview(self, time_sec: float, output_path: str) -> bool:
        if (lib := self.lib) and (h := self.handle):
            return lib.vose_save_frame(h, time_sec, output_path.encode("utf-8")) == 1
        return False

    def extract_waveform(self, chunks: int = 512) -> List[float]:
        if (lib := self.lib) and (h := self.handle):
            buf = (ctypes.c_float * chunks)()
            n = lib.vose_waveform(h, buf, chunks, chunks)
            return list(buf[:n])
        return []

    def export_edl(self, edl_json: str, out_path: str) -> bool:
        if (lib := self.lib) and (h := self.handle):
            return lib.vose_export_edl(h, edl_json.encode("utf-8"), out_path.encode("utf-8")) == 1
        return False

    def export_hw(self, edl_json: str, out_path: str, quality: int = 23) -> bool:
        if (lib := self.lib) and (h := self.handle):
            return lib.vose_export_hw(h, edl_json.encode("utf-8"), out_path.encode("utf-8"), quality) == 1
        return False

    def build_keyframe_index(self) -> int:
        if (lib := self.lib) and (h := self.handle):
            return int(lib.vose_build_keyframe_index(h))
        return 0

    def nearest_keyframe(self, time_sec: float) -> float:
        if (lib := self.lib) and (h := self.handle):
            return float(lib.vose_nearest_keyframe(h, time_sec))
        return time_sec

    def __del__(self) -> None:
        if (lib := self.lib) and (h := self.handle):
            lib.vose_destroy(h)
            self.handle = None
