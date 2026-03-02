import wave
import numpy as np
import glob
import os
from typing import List, Tuple


def pack_all_voices() -> None:
    # 1. パスの決定
    base_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../")
    )
    output_path = os.path.join(base_dir, "src/core/voice_data.h")
    # CI環境とローカルの両方に対応するため、相対パスを調整
    search_path = os.path.join(base_dir, "assets/official_voices/**/*.wav")

    # 出力先フォルダ(src/core)がなければ作成
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 2. WAVファイルのリストアップ
    wav_files = glob.glob(search_path, recursive=True)

    print(f"Target Output: {output_path}")
    print(f"Searching in: {search_path}")

    # --- 重要：ファイルがない場合でも「空のヘッダー」を書き出す ---
    if not wav_files:
        print("Warning: No wav files found. Creating dummy header to satisfy build.")
        with open(output_path, 'w', encoding='utf-8') as h:
            h.write("#pragma once\n")
            h.write("#include <stdint.h>\n\n")
            h.write("// Dummy function for CI build without assets\n")
            h.write("inline void register_all_embedded_voices() {}\n")
        return

    voice_entries: List[Tuple[str, str]] = []

    with open(output_path, 'w', encoding='utf-8') as h:
        h.write("#pragma once\n#include <stdint.h>\n\n")
        h.write("// C++側の関数を呼び出すための宣言\n")
        h.write(
            'extern "C" void load_embedded_resource('
            'const char* phoneme, '
            'const int16_t* raw_data, '
            'int sample_count);\n\n'
        )

        for wav_path in wav_files:
            parts = os.path.normpath(wav_path).split(os.sep)
            folder_name = parts[-2] if len(parts) > 2 else ""
            file_base = os.path.splitext(parts[-1])[0]

            if folder_name != "official_voices":
                entry_name: str = f"{folder_name}_{file_base}"
            else:
                entry_name: str = file_base

            safe_id: str = "".join(f"{ord(c):04x}" for c in entry_name)
            var_name: str = f"OFFICIAL_VOICE_{safe_id}"

            try:
                with wave.open(wav_path, 'rb') as f:
                    frames = f.readframes(f.getnframes())
                    data = np.frombuffer(frames, dtype=np.int16)

                    h.write(f"// Source: {wav_path}\n")
                    h.write(f"const int16_t {var_name}[] = {{\n    ")
                    for i, val in enumerate(data):
                        h.write(f"{val},")
                        if (i + 1) % 15 == 0:
                            h.write("\n    ")
                    h.write("\n};\n")
                    h.write(f"const int {var_name}_LEN = {len(data)};\n\n")
                    voice_entries.append((entry_name, var_name))
            except Exception as e:
                print(f"Error skipping {wav_path}: {e}")

        h.write("inline void register_all_embedded_voices() {\n")
        for ent_name, v_name in voice_entries:
            h.write(f'    load_embedded_resource("{ent_name}", {v_name}, {v_name}_LEN);\n')
        h.write("}\n")

    print(f"Success: Packed {len(voice_entries)} voices.")


if __name__ == "__main__":
    pack_all_voices()
