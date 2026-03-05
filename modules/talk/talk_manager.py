"""
vo_se_engine.py
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


# ══════════════════════════════════════════════════════════════
# 1. データクラス
# ══════════════════════════════════════════════════════════════

@dataclass
class AccentPhrase:
    """アクセント句の解析結果"""
    text: str
    mora_count: int
    accent_position: int
    f0_values: list[float] = field(default_factory=list)


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
            labels: list[str] = self._get_labels(text)
            self.last_analysis_status = True
            return "\n".join(labels)
        except Exception as e:
            self.last_analysis_status = False
            msg = f"Error during analysis: {e}\n{traceback.format_exc()}"
            print(msg)
            return msg

    def analyze_to_phonemes(self, text: str) -> list[str]:
        """
        テキストから音素列を抽出して返す。
        pyopenjtalk.g2p() を使用（バージョン間で最も安定した API）。

        例: "こんにちは" → ["k", "o", "N", "n", "i", "ch", "i", "w", "a"]
        """
        if not text:
            return []
        try:
            # kana=False で IPA 近似のローマ字音素列を取得
            phoneme_str: str = pyopenjtalk.g2p(text, kana=False)
            return [p for p in phoneme_str.split() if p]
        except Exception as e:
            print(f"[IntonationAnalyzer] g2p error: {e}")
            return []

    def analyze_to_accent_phrases(self, text: str) -> list[AccentPhrase]:
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

    def _get_labels(self, text: str) -> list[str]:
        """pyopenjtalk のバージョン差を吸収してラベルを取得する"""
        if hasattr(pyopenjtalk, "run_frontend"):
            features = pyopenjtalk.run_frontend(text)
        else:
            features = pyopenjtalk.extract_fullcontext(text)
        return pyopenjtalk.make_label(features)

    def _parse_labels(self, labels: list[str]) -> list[AccentPhrase]:
        """
        HTS フルコンテキストラベルからアクセント句・F0 を抽出する。
        ラベル形式の A: フィールド（アクセント型）と F0 推定値を利用。
        """
        phrases: list[AccentPhrase] = []
        current_moras: list[tuple[str, float]] = []
        accent_pos: int = 0
        prev_phrase_id: str = ""

        for label in labels:
            # 音素名（p3 フィールド）を取得
            parts = label.split("-")
            phoneme = parts[1] if len(parts) > 1 else "?"

            # アクセント句 ID（/E: フィールド）でグループ化
            phrase_id = self._extract_field(label, "/E:")

            if phrase_id != prev_phrase_id and current_moras:
                phrases.append(AccentPhrase(
                    text="".join(m[0] for m in current_moras),
                    mora_count=len(current_moras),
                    accent_position=accent_pos,
                    f0_values=[m[1] for m in current_moras],
                ))
                current_moras = []

            # A: フィールドからアクセント型を取得
            try:
                a_field = self._extract_field(label, "/A:")
                accent_pos = int(a_field.split("_")[0]) if a_field else 0
            except (ValueError, IndexError):
                accent_pos = 0

            # 簡易 F0 推定（実際は HMM から取るべきだが近似値として利用）
            f0 = 130.0 if accent_pos == 0 else 150.0 + accent_pos * 5.0

            if phoneme not in ("sil", "pau", "?"):
                current_moras.append((phoneme, f0))

            prev_phrase_id = phrase_id

        # 末尾の句を追加
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

def generate_accent_curve(phoneme: str, accent_pos: int = 0) -> list[float]:
    """
    音素とアクセント位置からピッチカーブを生成する。
    将来的には AccentPhrase.f0_values を直接使用することを推奨。
    """
    base_f0 = 150.0 + accent_pos * 5.0
    # 子音は無声（0Hz）、母音はピッチあり
    voiced = phoneme in list("aeiou") + ["N", "m", "n", "r", "w", "y", "v"]
    return [base_f0 if voiced else 0.0] * 50


def generate_talk_events(
    text: str,
    analyzer: IntonationAnalyzer,
) -> list[dict[str, Any]]:
    """
    テキストから VO-SE エンジン用トークイベントリストを生成する。

    Returns:
        List of dicts with keys:
            phoneme, pitch, gender, tension, breath,
            offset, consonant, cutoff, pre_utterance, overlap
    """
    phonemes = analyzer.analyze_to_phonemes(text)
    # アクセント句も取得してピッチ生成に活用
    accent_phrases = analyzer.analyze_to_accent_phrases(text)

    # 音素→アクセント位置マップ（簡易版：句単位で均等割り当て）
    accent_map: dict[int, int] = {}
    idx = 0
    for phrase in accent_phrases:
        for _ in range(phrase.mora_count):
            accent_map[idx] = phrase.accent_position
            idx += 1

    talk_notes: list[dict[str, Any]] = []
    for i, phoneme in enumerate(phonemes):
        accent_pos = accent_map.get(i, 0)
        pitch_curve = generate_accent_curve(phoneme, accent_pos)
        length = len(pitch_curve)

        talk_notes.append({
            "phoneme":       phoneme,
            "pitch":         pitch_curve,
            "gender":        [0.5] * length,
            "tension":       [0.5] * length,
            "breath":        [0.1] * length,   # 0.1 で自然な息感
            # UTAU パラメータ（デフォルト値）
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
    """
    VO-SE C++ エンジン用構造体。
    C++ 側の struct NoteEvent とメモリ配置を完全一致させること。
    """
    _fields_ = [
        ("wav_path",           ctypes.c_char_p),
        ("pitch_length",       ctypes.c_int),
        ("pitch_curve",        ctypes.POINTER(ctypes.c_double)),
        ("gender_curve",       ctypes.POINTER(ctypes.c_double)),
        ("tension_curve",      ctypes.POINTER(ctypes.c_double)),
        ("breath_curve",       ctypes.POINTER(ctypes.c_double)),
        # UTAU 互換パラメータ
        ("offset_ms",          ctypes.c_double),   # 原音の開始位置
        ("consonant_ms",       ctypes.c_double),   # 固定範囲（子音部）
        ("cutoff_ms",          ctypes.c_double),   # 右ブランク
        ("pre_utterance_ms",   ctypes.c_double),   # 先行発声
        ("overlap_ms",         ctypes.c_double),   # オーバーラップ
    ]


class VoseRendererBridge:
    """
    Python ↔ C++ DLL/dylib ブリッジ。
    GC 対策として配列参照を keep_alive に保持する。
    """

    def __init__(self, dll_path: str) -> None:
        try:
            # macOS は RTLD_GLOBAL でシンボルをグローバル公開
            if platform.system() == "Darwin":
                self.lib = ctypes.CDLL(dll_path, mode=ctypes.RTLD_GLOBAL)
            else:
                self.lib = ctypes.CDLL(dll_path)

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

    def render(self, notes_data: list[dict[str, Any]], output_path: str) -> bool:
        """
        Python データを NoteEvent 配列に変換して C++ レンダラーに渡す。

        Returns:
            True on success, False on failure.
        """
        if self.lib is None:
            print("❌ render() called but engine is not loaded.")
            return False

        note_count = len(notes_data)
        if note_count == 0:
            print("⚠️ render() called with empty notes_data.")
            return False

        NotesArray = NoteEvent * note_count
        c_notes = NotesArray()

        # ★ GC 対策：C 側にポインタを渡す間、Python 配列を生存させる
        keep_alive: list[Any] = []

        for i, data in enumerate(notes_data):
            p_arr = (ctypes.c_double * len(data["pitch"]))(*data["pitch"])
            g_arr = (ctypes.c_double * len(data["gender"]))(*data["gender"])
            t_arr = (ctypes.c_double * len(data["tension"]))(*data["tension"])
            b_arr = (ctypes.c_double * len(data["breath"]))(*data["breath"])
            keep_alive.extend([p_arr, g_arr, t_arr, b_arr])

            c_notes[i].wav_path         = data["phoneme"].encode("utf-8")
            c_notes[i].pitch_length     = len(data["pitch"])
            c_notes[i].pitch_curve      = p_arr
            c_notes[i].gender_curve     = g_arr
            c_notes[i].tension_curve    = t_arr
            c_notes[i].breath_curve     = b_arr
            c_notes[i].offset_ms        = data.get("offset",        0.0)
            c_notes[i].consonant_ms     = data.get("consonant",     0.0)
            c_notes[i].cutoff_ms        = data.get("cutoff",        0.0)
            c_notes[i].pre_utterance_ms = data.get("pre_utterance", 0.0)
            c_notes[i].overlap_ms       = data.get("overlap",       0.0)

        try:
            self.lib.execute_render(c_notes, note_count, output_path.encode("utf-8"))
            print(f"🎬 Render finished: {output_path}")
            return True
        except Exception as e:
            print(f"❌ execute_render error: {e}\n{traceback.format_exc()}")
            return False


# ══════════════════════════════════════════════════════════════
# 5. 音声合成マネージャー
# ══════════════════════════════════════════════════════════════

class TalkManager(QObject):
    """
    pyopenjtalk を使用した TTS マネージャー。
    htsvoice の切替・フォールバックを自動処理する。
    """

    def __init__(self) -> None:
        super().__init__()
        self.current_voice_path: str | None = None
        self.is_speaking: bool = False

    # ----------------------------------------------------------
    # ボイス設定
    # ----------------------------------------------------------

    def set_voice(self, htsvoice_path: str) -> bool:
        if htsvoice_path and os.path.exists(htsvoice_path):
            self.current_voice_path = htsvoice_path
            return True
        print(f"⚠️ Voice path not found: {htsvoice_path}")
        return False

    # ----------------------------------------------------------
    # 簡易スピーク（再生まで行う場合はここを拡張）
    # ----------------------------------------------------------

    def speak(self, text: str) -> None:
        if not text:
            return
        try:
            print(f"🗣 Speaking: {text}")
            # TODO: 再生処理を追加する場合はここに実装
        except Exception as e:
            print(f"Speech Error: {e}\n{traceback.format_exc()}")

    # ----------------------------------------------------------
    # WAV 合成
    # ----------------------------------------------------------

    def synthesize(
        self,
        text: str,
        output_path: str,
        speed: float = 1.0,
    ) -> tuple[bool, str]:
        """
        テキストを WAV に合成して output_path に保存する。

        Returns:
            (True, output_path) on success
            (False, error_message) on failure
        """
        if not text:
            return False, "テキストが空です。"

        try:
            # 出力先ディレクトリを確保
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            x: np.ndarray | None = None
            sr: int = 48000
            options: dict[str, Any] = {"speed": float(speed)}
            voice = self.current_voice_path or ""

            if voice and os.path.exists(voice):
                x, sr = self._tts_with_voice(text, voice, options)
            else:
                x, sr = self._tts_default(text, options)

            if x is None:
                return False, "音声データの生成に失敗しました。"

            # int16 変換して保存
            x_int16 = np.clip(np.asarray(x), -32768, 32767).astype(np.int16)
            sf.write(output_path, x_int16, sr)

            if os.path.exists(output_path):
                print(f"✅ Saved: {output_path}")
                return True, output_path

            return False, f"書き出し失敗: {output_path}"

        except Exception as e:
            msg = f"Critical synthesis error: {e}\n{traceback.format_exc()}"
            print(msg)
            return False, msg

    # ----------------------------------------------------------
    # 内部実装
    # ----------------------------------------------------------

    def _tts_with_voice(
        self,
        text: str,
        voice: str,
        options: dict[str, Any],
    ) -> tuple[np.ndarray | None, int]:
        """
        指定ボイスで TTS を試みる。
        htsvoice → font → デフォルトの順でフォールバック。
        """
        for key in ("htsvoice", "font"):
            try:
                result = pyopenjtalk.tts(text, **{**options, key: voice})
                if result is not None and len(result) >= 2:
                    return result[0], result[1]
            except (TypeError, Exception) as e:
                print(f"DEBUG: '{key}' kwarg failed: {e}")

        print("DEBUG: Falling back to default voice")
        return self._tts_default(text, options)

    @staticmethod
    def _tts_default(
        text: str,
        options: dict[str, Any],
    ) -> tuple[np.ndarray | None, int]:
        """デフォルトボイスで TTS を実行する"""
        result = pyopenjtalk.tts(text, **options)
        if result is not None and len(result) >= 2:
            return result[0], result[1]
        return None, 48000
