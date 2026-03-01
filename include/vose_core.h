#ifndef VOSE_CORE_H
#define VOSE_CORE_H

#ifdef _WIN32
    #define DLLEXPORT __declspec(dllexport)
#else
    #define DLLEXPORT __attribute__((visibility("default")))
#endif

#include <stdint.h>

// --- GUI（Python）とやり取りするための構造体 ---
// 64bit/32bit環境でサイズが変わらないよう、アライメントを厳密に制御します
#pragma pack(push, 8) 
struct NoteEvent {
    const char* wav_path;      // 音源キー（音素名）
    double* pitch_curve;       // 周波数(Hz) ※WORLDに合わせdoubleへ
    int pitch_length;          // 配列の長さ
    
    // 追加パラメータ（精度維持のためdouble）
    double* gender_curve;      
    double* tension_curve;     
    double* breath_curve;      
};
#pragma pack(pop)

extern "C" {
    // 1. 音源をメモリにパッキングする（内蔵音源化の必須関数）
    DLLEXPORT void load_embedded_resource(const char* phoneme, const int16_t* raw_data, int sample_count);

    // 2. レンダリング実行関数
    DLLEXPORT void execute_render(NoteEvent* notes, int note_count, const char* output_path);
    
    // 3. エンジン管理
    DLLEXPORT float get_engine_version(void);
    DLLEXPORT void clear_engine_cache(void);
}

#endif // VOSE_CORE_H
