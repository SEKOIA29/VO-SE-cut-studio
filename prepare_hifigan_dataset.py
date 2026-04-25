"""
VO-SE Pro - HiFi-GAN 学習用前処理パイプライン
==============================================
Doc3 (pack_all_voices.py) と設計思想を統一しつつ、
HiFi-GANの学習に必要なメルスペクトログラムを生成する。

対応データセット:
  - PJS (Professional Japanese Singing voice corpus)
  - JVS-MuSiC

出力:
  - wavs/          : 44100Hz / モノラル / 16bit に正規化済みWAV
  - mels/          : メルスペクトログラム (.npy)
  - train.txt      : 学習用ファイルリスト
  - val.txt        : 検証用ファイルリスト
  - config.json    : HiFi-GAN設定ファイル（そのままKaggleで使える）

使い方:
  python prepare_hifigan_dataset.py \
      --input_dir  /path/to/PJS \
      --output_dir ./hifigan_dataset \
      --val_ratio  0.05
"""

import os
import sys
import json
import wave
import argparse
import numpy as np
from pathlib import Path
from math import gcd


# ============================================================
# メルスペクトログラム設定
# HiFi-GAN公式と同じ値に揃えることで
# 公開済みノートブックをそのまま流用できる
# ============================================================

MEL_CONFIG = {
    "sampling_rate"   : 22050,   # HiFi-GAN公式デフォルト
                                  # PJSは44100Hz → ここでダウンサンプル
    "n_fft"           : 1024,
    "hop_size"        : 256,
    "win_size"        : 1024,
    "n_mel_channels"  : 80,
    "mel_fmin"        : 0.0,
    "mel_fmax"        : 8000.0,
    "segment_size"    : 8192,    # 学習時の切り出しサンプル数
    "num_workers"     : 4,
}

# ============================================================
# WAV読み込み・正規化
# Doc3のリサンプリングロジックを流用
# ============================================================

def load_wav_as_float(wav_path: str, target_sr: int) -> tuple[np.ndarray, int]:
    """
    WAVをfloat64で読み込み、モノラル化・リサンプリングして返す。
    戻り値: (波形配列 [-1.0, 1.0], サンプリングレート)
    """
    with wave.open(wav_path, "rb") as f:
        sr        = f.getframerate()
        n_ch      = f.getnchannels()
        n_frames  = f.getnframes()
        raw       = f.readframes(n_frames)

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    # ステレオ→モノラル（Doc3と同じ処理）
    if n_ch == 2:
        data = data.reshape(-1, 2).mean(axis=1)

    # リサンプリング（Doc3のresample_polyを流用）
    if sr != target_sr:
        from scipy.signal import resample_poly
        g    = gcd(sr, target_sr)
        data = resample_poly(data, target_sr // g, sr // g)
        data = np.clip(data, -32768, 32767)
        sr   = target_sr

    # [-1.0, 1.0] に正規化
    wave_float = data / 32768.0
    return wave_float.astype(np.float32), sr


# ============================================================
# メルスペクトログラム生成
# librosaを使用（Kaggle環境にプリインストール済み）
# ============================================================

def compute_mel(wave: np.ndarray, cfg: dict) -> np.ndarray:
    """
    float32波形からメルスペクトログラムを生成する。
    shape: (n_mel_channels, T)
    """
    import librosa

    mel = librosa.feature.melspectrogram(
        y         = wave,
        sr        = cfg["sampling_rate"],
        n_fft     = cfg["n_fft"],
        hop_length= cfg["hop_size"],
        win_length= cfg["win_size"],
        n_mels    = cfg["n_mel_channels"],
        fmin      = cfg["mel_fmin"],
        fmax      = cfg["mel_fmax"],
        power     = 1.0,   # amplitude spectrogram（HiFi-GAN標準）
    )

    # log圧縮（HiFi-GANの標準前処理）
    log_mel = np.log(np.clip(mel, a_min=1e-5, a_max=None))
    return log_mel.astype(np.float32)


# ============================================================
# WAVスキャン
# Doc3の glob パターンと統一
# ============================================================

def scan_wav_files(input_dir: str) -> list[str]:
    """
    サブフォルダを含む全WAVを再帰スキャン。
    Doc3の search_path = "**/*.wav" と同じ。
    """
    wav_files = sorted(Path(input_dir).rglob("*.wav"))
    if not wav_files:
        print(f"Warning: No wav files found in {input_dir}")
    return [str(p) for p in wav_files]


# ============================================================
# 正規化済みWAVの書き出し
# ============================================================

def save_wav_16bit(path: str, wave: np.ndarray, sr: int) -> None:
    """float32波形を16bit WAVで保存する"""
    pcm = (wave * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)   # 16bit
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())


# ============================================================
# HiFi-GAN設定ファイル生成
# Kaggleのノートブックにそのまま貼れる形式
# ============================================================

def generate_hifigan_config(output_dir: str, cfg: dict) -> None:
    """
    HiFi-GAN V1 公式と互換性のある config.json を生成する。
    Kaggle上でそのまま使えるようにフルセットで書き出す。
    """
    hifigan_config = {
        # --- オーディオ設定（メルと統一）---
        "resblock"                   : "1",
        "num_gpus"                   : 0,
        "batch_size"                 : 16,
        "learning_rate"              : 0.0002,
        "adam_b1"                    : 0.8,
        "adam_b2"                    : 0.99,
        "lr_decay"                   : 0.999,
        "seed"                       : 1234,

        "upsample_rates"             : [8, 8, 2, 2],
        "upsample_kernel_sizes"      : [16, 16, 4, 4],
        "upsample_initial_channel"   : 128,
        "resblock_kernel_sizes"      : [3, 7, 11],
        "resblock_dilation_sizes"    : [[1,3,5],[1,3,5],[1,3,5]],

        "segment_size"               : cfg["segment_size"],
        "num_mels"                   : cfg["n_mel_channels"],
        "num_freq"                   : cfg["n_fft"] // 2 + 1,
        "n_fft"                      : cfg["n_fft"],
        "hop_size"                   : cfg["hop_size"],
        "win_size"                   : cfg["win_size"],
        "sampling_rate"              : cfg["sampling_rate"],
        "fmin"                       : cfg["mel_fmin"],
        "fmax"                       : cfg["mel_fmax"],
        "fmax_for_loss"              : None,

        "num_workers"                : cfg["num_workers"],

        # --- 学習ステップ設定 ---
        # Kaggle T4(16GB)でのfine-tuning推奨値
        # PJSは約30曲・数千ファイルなので
        # 50000〜100000 stepで十分収束する
        "training_epochs"            : 3100,
        "stdout_interval"            : 5,
        "checkpoint_interval"        : 5000,
        "summary_interval"           : 100,
        "validation_interval"        : 1000,
    }

    config_path = os.path.join(output_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(hifigan_config, f, indent=4, ensure_ascii=False)

    print(f"Generated: {config_path}")


# ============================================================
# メインパイプライン
# ============================================================

def prepare_dataset(input_dir: str, output_dir: str, val_ratio: float) -> None:
    """
    メインの前処理パイプライン。
    1. WAVスキャン
    2. リサンプリング・正規化
    3. メルスペクトログラム生成
    4. train/val分割
    5. HiFi-GAN設定ファイル生成
    """

    # --- 出力ディレクトリの準備 ---
    wav_out_dir = os.path.join(output_dir, "wavs")
    mel_out_dir = os.path.join(output_dir, "mels")
    os.makedirs(wav_out_dir, exist_ok=True)
    os.makedirs(mel_out_dir, exist_ok=True)

    target_sr = MEL_CONFIG["sampling_rate"]

    # --- WAVスキャン ---
    wav_files = scan_wav_files(input_dir)
    if not wav_files:
        print("Error: No WAV files found. Exiting.")
        sys.exit(1)

    print(f"Found {len(wav_files)} WAV files.")
    print(f"Target sample rate: {target_sr} Hz")
    print(f"Output directory  : {output_dir}")
    print("-" * 50)

    # --- ファイル単位の処理 ---
    processed    = []
    skipped      = []
    min_duration = MEL_CONFIG["segment_size"] / target_sr  # 最低必要秒数

    for i, wav_path in enumerate(wav_files):

        # Doc3と同じ命名規則：フォルダ名_ファイル名
        parts       = os.path.normpath(wav_path).split(os.sep)
        folder_name = parts[-2] if len(parts) > 2 else "root"
        file_base   = os.path.splitext(parts[-1])[0]
        entry_id    = f"{folder_name}_{file_base}"

        try:
            # 1. 読み込み・リサンプリング
            wav_float, sr = load_wav_as_float(wav_path, target_sr)

            # segment_size より短いファイルはスキップ
            # （HiFi-GANの学習時にクラッシュする原因になる）
            duration = len(wav_float) / sr
            if duration < min_duration:
                skipped.append(wav_path)
                print(f"  Skip (too short {duration:.2f}s): {entry_id}")
                continue

            # 2. 正規化済みWAV書き出し
            wav_out_path = os.path.join(wav_out_dir, f"{entry_id}.wav")
            save_wav_16bit(wav_out_path, wav_float, sr)

            # 3. メルスペクトログラム生成・保存
            mel          = compute_mel(wav_float, MEL_CONFIG)
            mel_out_path = os.path.join(mel_out_dir, f"{entry_id}.npy")
            np.save(mel_out_path, mel)

            processed.append(entry_id)

            if (i + 1) % 50 == 0 or (i + 1) == len(wav_files):
                print(f"  [{i+1}/{len(wav_files)}] {entry_id} "
                      f"| duration: {duration:.2f}s "
                      f"| mel shape: {mel.shape}")

        except Exception as e:
            skipped.append(wav_path)
            print(f"  Error skipping {wav_path}: {e}")

    if not processed:
        print("Error: No files were successfully processed.")
        sys.exit(1)

    # --- train / val 分割 ---
    # シャッフルして末尾をval用に確保
    rng = np.random.default_rng(seed=42)
    indices = rng.permutation(len(processed)).tolist()

    n_val    = max(1, int(len(processed) * val_ratio))
    val_ids  = [processed[i] for i in indices[:n_val]]
    train_ids= [processed[i] for i in indices[n_val:]]

    def write_list(filepath: str, ids: list[str]) -> None:
        with open(filepath, "w", encoding="utf-8") as f:
            for entry_id in ids:
                # HiFi-GANが期待するフォーマット:
                # wavs/entry_id.wav|mels/entry_id.npy
                f.write(f"wavs/{entry_id}.wav|mels/{entry_id}.npy\n")

    write_list(os.path.join(output_dir, "train.txt"), train_ids)
    write_list(os.path.join(output_dir, "val.txt"),   val_ids)

    # --- HiFi-GAN設定ファイル生成 ---
    generate_hifigan_config(output_dir, MEL_CONFIG)

    # --- サマリー ---
    print("\n" + "=" * 50)
    print("前処理完了")
    print(f"  処理成功 : {len(processed)} ファイル")
    print(f"  スキップ : {len(skipped)} ファイル")
    print(f"  学習用   : {len(train_ids)} ファイル  → train.txt")
    print(f"  検証用   : {len(val_ids)}  ファイル  → val.txt")
    print(f"  設定     : config.json")
    print("=" * 50)
    print("\n次のステップ:")
    print("  1. output_dir ごと Google Drive にアップロード")
    print("  2. Kaggle で HiFi-GAN fine-tuning ノートブックを実行")
    print("  3. チェックポイントを ONNX に変換")


# ============================================================
# エントリーポイント
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="VO-SE Pro: HiFi-GAN学習用データセット前処理"
    )
    p.add_argument(
        "--input_dir",
        required=True,
        help="PJS または JVS-MuSiC のルートディレクトリ",
    )
    p.add_argument(
        "--output_dir",
        default="./hifigan_dataset",
        help="出力先ディレクトリ（デフォルト: ./hifigan_dataset）",
    )
    p.add_argument(
        "--val_ratio",
        type=float,
        default=0.05,
        help="検証データの割合（デフォルト: 0.05 = 5%%）",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    prepare_dataset(
        input_dir  = args.input_dir,
        output_dir = args.output_dir,
        val_ratio  = args.val_ratio,
    )
