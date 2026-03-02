import wave
import numpy as np
import glob
import os

def pack_all_voices():
    output_path = "src/voice_data.h"
    # サブフォルダまで全スキャンする設定 (**/*.wav)
    search_path = "assets/official_voices/**/*.wav"
    wav_files = glob.glob(search_path, recursive=True)

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
    output_path = os.path.join(base_dir, "src/voice_data.h")
    search_path = os.path.join(base_dir, "assets/official_voices/**/*.wav")
    
    # --- 修正ポイント2: 出力先フォルダ(src)がなければ自動で作る ---
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # あとのスキャン処理はそのまま
    wav_files = glob.glob(search_path, recursive=True)
    
    # デバッグ用にパスを表示（Actionsのログで見れる）
    print(f"Target Output: {output_path}")
    print(f"Searching in: {search_path}")
    
    if not wav_files:
        print("Warning: No wav files found. Check your assets path!")
    
    with open(output_path, 'w', encoding='utf-8') as h:
        h.write("#pragma once\n#include <stdint.h>\n\n")

        h.write("// C++側の関数を呼び出すための宣言\n")
        h.write('extern "C" void load_embedded_resource(const char* phoneme, const int16_t* raw_data, int sample_count);\n\n')
        
        voice_entries = []
        
        for wav_path in wav_files:
            # パスを分解して「フォルダ名」と「ファイル名」を取得
            # 例: assets/official_voices/kanase/あ.wav -> folder="kanase", file="あ"
            parts = os.path.normpath(wav_path).split(os.sep)
            folder_name = parts[-2] if len(parts) > 2 else ""
            file_base = os.path.splitext(parts[-1])[0]
            
            # 登録名を「フォルダ名_ファイル名」にする（名前の衝突を防ぐ）
            # もしフォルダが official_voices 直下ならファイル名のみ
            entry_name = f"{folder_name}_{file_base}" if folder_name != "official_voices" else file_base
            
            # C++変数名として安全な16進数IDを作成
            safe_id = "".join(f"{ord(c):04x}" for c in entry_name)
            var_name = f"OFFICIAL_VOICE_{safe_id}"
            
            try:
                with wave.open(wav_path, 'rb') as f:
                    frames = f.readframes(f.getnframes())
                    data = np.frombuffer(frames, dtype=np.int16)
                    
                    h.write(f"// Source: {wav_path} (ID: {entry_name})\n")
                    h.write(f"const int16_t {var_name}[] = {{\n    ")
                    
                    # データを15個ずつ改行して書き出し
                    for i, val in enumerate(data):
                        h.write(f"{val},")
                        if (i + 1) % 15 == 0:
                            h.write("\n    ")
                    
                    h.write("\n}};\n")
                    h.write(f"const int {var_name}_LEN = {len(data)};\n\n")
                    
                    voice_entries.append((entry_name, var_name))
            except Exception as e:
                print(f"Error skipping {wav_path}: {e}")

        # 一括登録関数を自動生成
        h.write("inline void register_all_embedded_voices() {\n")
        for entry_name, var_name in voice_entries:
            h.write(f'    load_embedded_resource("{entry_name}", {var_name}, {var_name}_LEN);\n')
        h.write("}\n")

    print(f"Success: Packed {len(wav_files)} voices from {search_path}")

if __name__ == "__main__":
    pack_all_voices()
