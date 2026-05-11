import ctypes
import os
from typing import Optional, List

# C++側で定義した構造体をPython側でも定義
class FrameInfo(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("pts_seconds", ctypes.c_double),
        ("is_keyframe", ctypes.c_bool),
        ("rgb_data_ptr", ctypes.POINTER(ctypes.c_uint8)), # 実データへのポインタ
        ("rgb_data_size", ctypes.c_size_t)
    ]

class VideoEngine:
    def __init__(self, lib_path: str = "./libvideo_engine.dylib"):
        # ライブラリのロード (macOSなら .dylib, Linuxなら .so)
        if not os.path.exists(lib_path):
            raise FileNotFoundError(f"Engine library not found at {lib_path}")
        
        self.lib = ctypes.CDLL(lib_path)
        
        # C関数の戻り値と引数の型定義
        self.lib.vose_create.restype = ctypes.c_void_p
        
        self.lib.vose_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.vose_load.restype = ctypes.c_int
        
        self.lib.vose_duration.argtypes = [ctypes.c_void_p]
        self.lib.vose_duration.restype = ctypes.c_double
        
        self.lib.vose_save_frame.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_char_p]
        self.lib.vose_save_frame.restype = ctypes.c_int

        # インスタンス生成
        self.handle = self.lib.vose_create()

    def load_video(self, path: str) -> bool:
        return self.lib.vose_load(self.handle, path.encode('utf-8')) == 1

    @property
    def duration(self) -> float:
        return self.lib.vose_duration(self.handle)

    def save_preview(self, time_sec: float, output_path: str) -> bool:
        return self.lib.vose_save_frame(self.handle, time_sec, output_path.encode('utf-8')) == 1

    def __del__(self):
        if hasattr(self, "lib") and self.handle:
            self.lib.vose_destroy(self.handle)
