"""
talk_manager.py
VO-SE Cut Studio — コアエンジン統合モジュール
- IntonationAnalyzer : pyopenjtalk による音素・F0解析
- generate_talk_events: トークイベント生成
- NoteEvent           : C++ 構造体バインディング
- VoseRendererBridge  : DLL/dylib ブリッジ
- TalkManager         : 音声合成マネージャー
"""

from __future__ import annotations

import os
import ctypes
import platform
import traceback
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, cast, Union

import numpy as np
from numpy.typing import NDArray
import pyopenjtalk
import soundfile as sf
from PySide6.QtCore import QObject

# --- Pyright 対策: 型情報を持たない外部ライブラリを Any にキャストして警告を抑制 ---
_pyopenjtalk: Any = pyopenjtalk
_sf: Any = sf


if TYPE_CHECKING:
    # Pyright (CI) 用のモック定義：エンジンがなくても型を認識させる
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
        from .vo_se_engine import (
            IntonationAnalyzer,
            TalkManager,
            generate_talk_events
        )
    except (ImportError, AttributeError):
        # エンジンがない場合のフォールバック（開発環境やCIでのクラッシュ防止）
        class IntonationAnalyzer:
            pass

        class TalkManager:
            pass

        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []

__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]


# ══════════════════════════════════════════════════════════════
# 1. データクラス
# ══════════════════════════════════════════════════════════════

# Pyright の list[Unknown] エラーを防ぐための型付きファクトリ関数
def _default_float_list() -> List[float]:
    return []

@dataclass
class AccentPhrase:
    """アクセント句の解析結果"""
    text: str
    mora_count: int
    accent_position: int
    f0_values: List[float] = field(default_factory=_default_float_list)


# ══════════════════════════════════════════════════════════════
# 2. イントネーション解析
# ══════════════════════════════════════════════════════════════

class IntonationAnalyzer:
    """
    pyopenjtalk を使用したテキスト解析クラス。
    音素列・フルコンテキストラベル・アクセント句を返す。
    """

    def __init__(self) -> None:
        self.last_analysis_status: bool = False

    # ----------------------------------------------------------
    # 公開 API
    # ----------------------------------------------------------

    def analyze(self, text: str) -> str:
        """
        フルコンテキストラベルを改行区切り文字列で返す。
        GUI 表示やデバッグ用途向け。
        """
        if not text:
            return ""
        try:
            labels: List[str] = self._get_labels(text)
            self.last_analysis_status = True
            return "\n".join(labels)
        except Exception as e:
            self.last_analysis_status = False
            msg = f"Error during analysis: {e}\n{traceback.format_exc()}"
            print(msg)
            return msg

    def analyze_to_phonemes(self, text: str) -> List[str]:
        """
        テキストから音素列を抽出して返す。
        """
        if not text:
            return []
        try:
            # Pyright 対策: _pyopenjtalk 経由で呼び出し
            raw_phonemes = cast(str, _pyopenjtalk.g2p(text, kana=False))
            return [p for p in raw_phonemes.split() if p]
        except Exception as e:
            print(f"[IntonationAnalyzer] g2p error: {e}")
            return []

    def analyze_to_accent_phrases(self, text: str) -> List[AccentPhrase]:
        """
        アクセント句リストを返す（VO-SE ピッチ編集用）。
        """
        if not text:
            return []
        try:
            labels = self._get_labels(text)
            return self._parse_labels(labels)
        except Exception as e:
            print(f"[IntonationAnalyzer] accent parse error: {e}")
            return []

    # ----------------------------------------------------------
    # 内部実装
    # ----------------------------------------------------------

    def _get_labels(self, text: str) -> List[str]:
        """pyopenjtalk のバージョン差を吸収してラベルを取得する"""
        if hasattr(_pyopenjtalk, "run_frontend"):
            features = cast(List[Dict[str, Any]], _pyopenjtalk.run_frontend(text))
        else:
            features = cast(List[Dict[str, Any]], _pyopenjtalk.extract_fullcontext(text))
        
        return cast(List[str], _pyopenjtalk.make_label(features))

    def _parse_labels(self, labels: List[str]) -> List[AccentPhrase]:
        """
        HTS フルコンテキストラベルからアクセント句・F0 を抽出する。
        """
        phrases: List[AccentPhrase] = []
        current_moras: List[Tuple[str, float]] = []
        accent_pos: int = 0
        prev_phrase_id: str = ""

        for label in labels:
            parts = label.split("-")
            phoneme = parts[1] if len(parts) > 1 else "?"
            phrase_id = self._extract_field(label, "/E:")

            if phrase_id != prev_phrase_id and current_moras:
                phrases.append(AccentPhrase(
                    text="".join(m[0] for m in current_moras),
                    mora_count=len(current_moras),
                    accent_position=accent_pos,
                    f0_values=[m[1] for m in current_moras],
                ))
                current_moras = []

            try:
                a_field = self._extract_field(label, "/A:")
                accent_pos = int(a_field.split("_")[0]) if a_field else 0
            except (ValueError, IndexError):
                accent_pos = 0

            f0 = 130.0 if accent_pos == 0 else 150.0 + accent_pos * 5.0

            if phoneme not in ("sil", "pau", "?"):
                current_moras.append((phoneme, f0))

            prev_phrase_id = phrase_id

        if current_moras:
            phrases.append(AccentPhrase(
                text="".join(m[0] for m in current_moras),
                mora_count=len(current_moras),
                accent_position=accent_pos,
                f0_values=[m[1] for m in current_moras],
            ))

        return phrases

    @staticmethod
    def _extract_field(label: str, key: str) -> str:
        """HTS ラベルから特定フィールドの値を抽出する"""
        idx = label.find(key)
        if idx == -1:
            return ""
        start = idx + len(key)
        end = label.find("/", start)
        return label[start:end] if end != -1 else label[start:]


# ══════════════════════════════════════════════════════════════
# 3. トークイベント生成
# ══════════════════════════════════════════════════════════════

def generate_accent_curve(phoneme: str, accent_pos: int = 0) -> List[float]:
    """音素とアクセント位置からピッチカーブを生成する"""
    base_f0 = 150.0 + accent_pos * 5.0
    voiced = phoneme in list("aeiou") + ["N", "m", "n", "r", "w", "y", "v"]
    return [base_f0 if voiced else 0.0] * 50


def generate_talk_events(
    text: str,
    analyzer: IntonationAnalyzer,
) -> List[Dict[str, Any]]:
    """VO-SE エンジン用トークイベントリストを生成する"""
    phonemes = analyzer.analyze_to_phonemes(text)
    accent_phrases = analyzer.analyze_to_accent_phrases(text)

    accent_map: Dict[int, int] = {}
    idx = 0
    for phrase in accent_phrases:
        for _ in range(phrase.mora_count):
            accent_map[idx] = phrase.accent_position
            idx += 1

    talk_notes: List[Dict[str, Any]] = []
    for i, phoneme in enumerate(phonemes):
        accent_pos = accent_map.get(i, 0)
        pitch_curve = generate_accent_curve(phoneme, accent_pos)
        length = len(pitch_curve)

        talk_notes.append({
            "phoneme":       phoneme,
            "pitch":         pitch_curve,
            "gender":        [0.5] * length,
            "tension":       [0.5] * length,
            "breath":        [0.1] * length,
            "offset":        0.0,
            "consonant":     0.0,
            "cutoff":        0.0,
            "pre_utterance": 0.0,
            "overlap":       0.0,
        })

    return talk_notes


# ══════════════════════════════════════════════════════════════
# 4. C++ 構造体バインディング
# ══════════════════════════════════════════════════════════════

class NoteEvent(ctypes.Structure):
    """VO-SE C++ エンジン用構造体"""
    _fields_ = [
        ("wav_path",           ctypes.c_char_p),
        ("pitch_length",       ctypes.c_int),
        ("pitch_curve",        ctypes.POINTER(ctypes.c_double)),
        ("gender_curve",       ctypes.POINTER(ctypes.c_double)),
        ("tension_curve",      ctypes.POINTER(ctypes.c_double)),
        ("breath_curve",       ctypes.POINTER(ctypes.c_double)),
        ("offset_ms",          ctypes.c_double),
        ("consonant_ms",       ctypes.c_double),
        ("cutoff_ms",          ctypes.c_double),
        ("pre_utterance_ms",   ctypes.c_double),
        ("overlap_ms",         ctypes.c_double),
    ]


class VoseRendererBridge:
    """Python ↔ C++ DLL/dylib ブリッジ"""

    def __init__(self, dll_path: str) -> None:
        self.lib: Optional[ctypes.CDLL] = None
        try:
            if platform.system() == "Darwin":
                self.lib = ctypes.CDLL(dll_path, mode=ctypes.RTLD_GLOBAL)
            else:
                self.lib = ctypes.CDLL(dll_path)

            if self.lib:
                self.lib.init_official_engine.argtypes = []
                self.lib.init_official_engine.restype = None

                self.lib.execute_render.argtypes = [
                    ctypes.POINTER(NoteEvent),
                    ctypes.c_int,
                    ctypes.c_char_p,
                ]
                self.lib.execute_render.restype = None

                self.lib.init_official_engine()
                print(f"✅ VO-SE Engine Initialized: {dll_path}")

        except Exception as e:
            print(f"❌ Engine Load Error: {e}\n{traceback.format_exc()}")
            self.lib = None

    def render(self, notes_data: List[Dict[str, Any]], output_path: str) -> bool:
        """NoteEvent 配列に変換して C++ レンダラーに渡す"""
        if self.lib is None:
            return False

        note_count = len(notes_data)
        if note_count == 0:
            return False

        NotesArray = NoteEvent * note_count
        c_notes = NotesArray()
        keep_alive: List[Any] = []

        for i, data in enumerate(notes_data):
            p_arr = (ctypes.c_double * len(data["pitch"]))(*data["pitch"])
            g_arr = (ctypes.c_double * len(data["gender"]))(*data["gender"])
            t_arr = (ctypes.c_double * len(data["tension"]))(*data["tension"])
            b_arr = (ctypes.c_double * len(data["breath"]))(*data["breath"])
            keep_alive.extend([p_arr, g_arr, t_arr, b_arr])

            c_notes[i].wav_path         = cast(str, data["phoneme"]).encode("utf-8")
            c_notes[i].pitch_length     = len(data["pitch"])
            c_notes[i].pitch_curve      = p_arr
            c_notes[i].gender_curve     = g_arr
            c_notes[i].tension_curve    = t_arr
            c_notes[i].breath_curve     = b_arr
            c_notes[i].offset_ms        = float(data.get("offset", 0.0))
            c_notes[i].consonant_ms     = float(data.get("consonant", 0.0))
            c_notes[i].cutoff_ms        = float(data.get("cutoff", 0.0))
            c_notes[i].pre_utterance_ms = float(data.get("pre_utterance", 0.0))
            c_notes[i].overlap_ms       = float(data.get("overlap", 0.0))

        try:
            self.lib.execute_render(c_notes, note_count, output_path.encode("utf-8"))
            return True
        except Exception as e:
            print(f"❌ execute_render error: {e}")
            return False


# ══════════════════════════════════════════════════════════════
# 5. 音声合成マネージャー
# ══════════════════════════════════════════════════════════════

class TalkManager(QObject):
    """pyopenjtalk を使用した TTS マネージャー"""

    def __init__(self) -> None:
        super().__init__()
        self.current_voice_path: Optional[str] = None
        self.is_speaking: bool = False

    def set_voice(self, htsvoice_path: str) -> bool:
        if htsvoice_path and os.path.exists(htsvoice_path):
            self.current_voice_path = htsvoice_path
            return True
        return False

    def synthesize(
        self,
        text: str,
        output_path: str,
        speed: float = 1.0,
    ) -> Tuple[bool, str]:
        """テキストを WAV に合成して保存する"""
        if not text:
            return False, "テキストが空です。"

        try:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            x: Optional[NDArray[Any]] = None
            sr: int = 48000
            options: Dict[str, Any] = {"speed": float(speed)}
            voice = self.current_voice_path or ""

            if voice and os.path.exists(voice):
                x, sr = self._tts_with_voice(text, voice, options)
            else:
                x, sr = self._tts_default(text, options)

            if x is None:
                return False, "音声データの生成に失敗しました。"
            
            if len(x) == 0:
                return False, "生成された音声が空です。"

            x_int16 = np.clip(np.asarray(x), -32768, 32767).astype(np.int16)
            
            # Pyright 対策: _sf 経由で呼び出し
            _sf.write(output_path, x_int16, sr)

            return True, output_path

        except Exception as e:
            return False, str(e)

    def _tts_with_voice(
        self,
        text: str,
        voice: str,
        options: Dict[str, Any],
    ) -> Tuple[Optional[NDArray[Any]], int]:
        """指定ボイスで TTS を試みる"""
        for key in ("htsvoice", "font"):
            try:
                tts_args = {**options, key: voice}
                # Pyright の Condition always True を防ぐため Any で受ける
                result: Any = _pyopenjtalk.tts(text, **tts_args)
                
                if result is not None:
                    res_tuple = cast(Tuple[NDArray[Any], int], result)
                    return res_tuple[0], res_tuple[1]
            except Exception:
                continue

        return self._tts_default(text, options)

    @staticmethod
    def _tts_default(
        text: str,
        options: Dict[str, Any],
    ) -> Tuple[Optional[NDArray[Any]], int]:
        """デフォルトボイスで TTS を実行する"""
        result: Any = _pyopenjtalk.tts(text, **options)
        
        if result is not None:
            res_tuple = cast(Tuple[NDArray[Any], int], result)
            return res_tuple[0], res_tuple[1]
            
        return None, 48000
