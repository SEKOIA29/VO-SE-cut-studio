#ifndef VOSE_CORE_H
#define VOSE_CORE_H

#ifdef _WIN32
    #define DLLEXPORT __declspec(dllexport)
#else
    #define DLLEXPORT __attribute__((visibility("default")))
#endif

#include <stdint.h>

#include <cstdint> 

// ディスクキャッシュの先頭に書き込むヘッダ情報
struct VoseCacheHeader {
    uint32_t magic;     // 'VOSE' (0x45534F56) かどうかを確認するマジックナンバー
    int length;         // フレーム数
    int spec_bins;      // 周波数ビン数
};

// --- GUI（Python）とやり取りするための構造体 ---
// 64bit/32bit環境でサイズが変わらないよう、アライメントを厳密に制御します

struct OtoEntry {
    const char* filename;
    double cutoff;
    char   alias[64];
    char   wav_path[512];
    double offset;       // ms: 左ブランク
    double consonant;    // ms: 子音固定
    double blank;        // ms: 右ブランク（負なら末尾からの距離）
    double preutterance; // ms: 先行発声
    double overlap;      // ms: オーバーラップ
};

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

struct OtoEntry; // 前方宣言


extern "C" {
    // 1. 音源をメモリにパッキングする（内蔵音源化の必須関数）
    DLLEXPORT void load_embedded_resource(const char* phoneme, const int16_t* raw_data, int sample_count);

    // 2. レンダリング実行関数
    DLLEXPORT void execute_render(NoteEvent* notes, int note_count, const char* output_path, int mode_flag);
    
    // 3. エンジン管理
    DLLEXPORT float get_engine_version(void);
    DLLEXPORT void clear_engine_cache(void);
}

#endif // VOSE_CORE_H
