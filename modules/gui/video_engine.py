"""
video_engine.py — VO-SE Cut Studio
フルリライト: OS別ライブラリ自動解決 / EDL変換修正 / 型安全化
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from typing import List, Optional, Tuple

_SYS = platform.system()


def _find_lib() -> str:
    """
    OS別にライブラリパスを自動解決する。
    検索順: 実行ファイルと同じディレクトリ → bin/ → システムパス
    """
    base = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv[0] else __file__))
    name_map = {
        "Darwin":  "libvideo_engine.dylib",
        "Windows": "video_engine.dll",
        "Linux":   "libvideo_engine.so",
    }
    lib_name = name_map.get(_SYS, "libvideo_engine.so")

    candidates = [
        os.path.join(base, lib_name),
        os.path.join(base, "bin", lib_name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), lib_name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", lib_name),
        # macOS 旧パス互換
        os.path.join(base, "libvideo_engine.dylib"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return os.path.join(base, lib_name)   # 存在しなくても返す（後でエラー表示）


class VideoEngine:
    """
    libvideo_engine の Python ラッパー。
    ライブラリが存在しない場合はダミー動作し、例外を発生させない。
    """

    def __init__(self, lib_path: Optional[str] = None) -> None:
        self.lib:    Optional[ctypes.CDLL] = None
        self.handle: Optional[ctypes.c_void_p] = None
        self._path = lib_path or _find_lib()
        self._load()

    # ── 初期化 ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self._path):
            print(f"⚠️  Engine library not found: {self._path}")
            return
        try:
            if _SYS == "Darwin":
                lib = ctypes.CDLL(self._path, mode=ctypes.RTLD_GLOBAL)
            else:
                lib = ctypes.CDLL(self._path)
            self._setup_signatures(lib)
            self.lib    = lib
            self.handle = lib.vose_create()
            print(f"✅  VideoEngine loaded: {self._path}")
        except Exception as exc:
            print(f"⚠️  Failed to load VideoEngine: {exc}")
            self.lib    = None
            self.handle = None

    def _setup_signatures(self, lib: ctypes.CDLL) -> None:
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

        lib.vose_save_frame.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_char_p,
        ]
        lib.vose_save_frame.restype  = ctypes.c_int

        # waveform: (handle, float*, buf_size, chunks) → int
        lib.vose_waveform.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.vose_waveform.restype = ctypes.c_int

        lib.vose_export_edl.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
        ]
        lib.vose_export_edl.restype = ctypes.c_int

        lib.vose_export_hw.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
        ]
        lib.vose_export_hw.restype = ctypes.c_int

        lib.vose_build_keyframe_index.argtypes = [ctypes.c_void_p]
        lib.vose_build_keyframe_index.restype  = ctypes.c_int

        lib.vose_nearest_keyframe.argtypes = [ctypes.c_void_p, ctypes.c_double]
        lib.vose_nearest_keyframe.restype  = ctypes.c_double

    # ── 公開 API ───────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.lib is not None and self.handle is not None

    def load_video(self, path: str) -> bool:
        if not self.available:
            return False
        return self.lib.vose_load(self.handle, path.encode("utf-8")) == 1  # type: ignore[union-attr]

    @property
    def duration(self) -> float:
        if self.available:
            return float(self.lib.vose_duration(self.handle))  # type: ignore[union-attr]
        return 0.0

    @property
    def width(self) -> int:
        if self.available:
            return int(self.lib.vose_width(self.handle))  # type: ignore[union-attr]
        return 0

    @property
    def height(self) -> int:
        if self.available:
            return int(self.lib.vose_height(self.handle))  # type: ignore[union-attr]
        return 0

    @property
    def fps(self) -> float:
        if self.available:
            return float(self.lib.vose_fps(self.handle))  # type: ignore[union-attr]
        return 0.0

    @property
    def has_audio(self) -> bool:
        if self.available:
            return self.lib.vose_has_audio(self.handle) == 1  # type: ignore[union-attr]
        return False

    def save_preview(self, time_sec: float, output_path: str) -> bool:
        if self.available:
            return self.lib.vose_save_frame(  # type: ignore[union-attr]
                self.handle, time_sec, output_path.encode("utf-8")
            ) == 1
        return False

    def extract_waveform(self, chunks: int = 512) -> List[float]:
        """peaks_max を chunks 個返す。失敗時は空リスト。"""
        if not self.available:
            return []
        buf = (ctypes.c_float * chunks)()
        n = self.lib.vose_waveform(self.handle, buf, chunks, chunks)  # type: ignore[union-attr]
        return list(buf[:max(0, n)])

    def export_edl(self, edl_json: str, out_path: str) -> bool:
        if self.available:
            return self.lib.vose_export_edl(  # type: ignore[union-attr]
                self.handle,
                edl_json.encode("utf-8"),
                out_path.encode("utf-8"),
            ) == 1
        return False

    def export_hw(self, edl_json: str, out_path: str, quality: int = 23) -> bool:
        """Apple VideoToolbox (macOS) または libx264 でエンコードしてエクスポート。"""
        if self.available:
            return self.lib.vose_export_hw(  # type: ignore[union-attr]
                self.handle,
                edl_json.encode("utf-8"),
                out_path.encode("utf-8"),
                quality,
            ) == 1
        return False

    def build_keyframe_index(self) -> int:
        if self.available:
            return int(self.lib.vose_build_keyframe_index(self.handle))  # type: ignore[union-attr]
        return 0

    def nearest_keyframe(self, time_sec: float) -> float:
        if self.available:
            return float(self.lib.vose_nearest_keyframe(self.handle, time_sec))  # type: ignore[union-attr]
        return time_sec

    def make_edl_json(
        self,
        clips: List[Tuple[float, float]],
        px_per_sec: float = 100.0,
    ) -> str:
        """
        (x_px, width_px) のリストから EDL JSON を生成する。
        px_per_sec を受け取ることでズーム連動に対応。
        内部座標が「秒」の場合は px_per_sec=1.0 を渡す。
        """
        import json
        entries = [
            {
                "in":      round(x / px_per_sec, 6),
                "out":     round((x + w) / px_per_sec, 6),
                "enabled": True,
            }
            for x, w in clips
            if w > 0
        ]
        return json.dumps(entries, ensure_ascii=False)

    def __del__(self) -> None:
        if self.available:
            try:
                self.lib.vose_destroy(self.handle)  # type: ignore[union-attr]
            except Exception:
                pass
            self.handle = None
