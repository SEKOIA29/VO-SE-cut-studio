#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <cstdint>
#include <mutex>
#include "vose_core.h"
#pragma once
#include <stdint.h>
inline void register_all_embedded_voices() {}

// WORLDライブラリ
#include "world/synthesis.h"
#include "world/cheaptrick.h"
#include "world/d4c.h"
#include "world/audioio.h"
#include "world/constantnumbers.h"

// ============================================================
// データ構造
// ============================================================

struct EmbeddedVoice {
    std::vector<double> waveform;
    int fs;
};

// スレッドセーフなボイスDB
static std::map<std::string, EmbeddedVoice> g_voice_db;
static std::mutex g_voice_db_mutex;

// ============================================================
// SynthesisScratchPad
// ノートをまたいで再利用するスクラッチバッファ。
// spec_tmp もここに収め、内側ループでの vector 確保を完全排除。
// ============================================================

struct SynthesisScratchPad {
    // WORLD解析・合成用フラットバッファ
    std::vector<double> flat_spec;   // [max_f0_length * spec_bins]
    std::vector<double> flat_ap;     // [max_f0_length * spec_bins]

    // フォルマントシフト用の一時退避バッファ（1フレーム分）
    std::vector<double> spec_tmp;    // [spec_bins]

    // WORLDが要求するポインタ配列
    std::vector<double*> spec_ptrs;
    std::vector<double*> ap_ptrs;

    // 現在確保済みのサイズ
    int reserved_f0  = 0;
    int reserved_bins = 0;

    /**
     * 必要なサイズに拡張する（縮小はしない）。
     * spec_ptrs / ap_ptrs のポインタは flat_* の再確保後に必ず更新する。
     */
    void ensure(int f0_length, int spec_bins) {
        bool need_rebuild = false;

        if (f0_length > reserved_f0 || spec_bins > reserved_bins) {
            reserved_f0   = std::max(f0_length,  reserved_f0);
            reserved_bins = std::max(spec_bins, reserved_bins);

            flat_spec.resize(reserved_f0 * reserved_bins);
            flat_ap  .resize(reserved_f0 * reserved_bins);
            spec_tmp .resize(reserved_bins);

            spec_ptrs.resize(reserved_f0);
            ap_ptrs  .resize(reserved_f0);
            need_rebuild = true;
        }

        if (need_rebuild) {
            for (int i = 0; i < reserved_f0; ++i) {
                spec_ptrs[i] = &flat_spec[i * reserved_bins];
                ap_ptrs  [i] = &flat_ap  [i * reserved_bins];
            }
        }
    }
};

// スレッドローカルにすることでスレッドセーフを実現
// （グローバル静的だと複数スレッドから execute_render を呼べない）
static thread_local SynthesisScratchPad tl_scratch;

// ============================================================
// 定数
// ============================================================

static constexpr int    kFs          = 44100;
static constexpr double kFramePeriod = 5.0;           // ms
static constexpr double kInv32768    = 1.0 / 32768.0;

// ============================================================
// extern "C" API
// ============================================================

extern "C" {

void init_official_engine() {
    register_all_embedded_voices();
}

/**
 * load_embedded_resource
 * int16_t の PCM データを double に変換してボイス DB へ登録する。
 */
DLLEXPORT void load_embedded_resource(const char*    phoneme,
                                      const int16_t* raw_data,
                                      int            sample_count)
{
    if (!phoneme || !raw_data || sample_count <= 0) return;

    EmbeddedVoice ev;
    ev.fs = kFs;
    ev.waveform.resize(sample_count);

    for (int i = 0; i < sample_count; ++i) {
        ev.waveform[i] = static_cast<double>(raw_data[i]) * kInv32768;
    }

    std::lock_guard<std::mutex> lock(g_voice_db_mutex);
    g_voice_db[phoneme] = std::move(ev);
}

/**
 * execute_render
 *
 * 改善点:
 *   1. fft_size / spec_bins をループ外で1回だけ計算
 *   2. SynthesisScratchPad を thread_local で管理 → スレッドセーフ
 *   3. spec_tmp も ScratchPad に含め、内側ループでの heap 確保を完全排除
 *   4. Gender シフト境界条件を修正（k0 >= spec_bins-1 のフレームも正しく処理）
 *   5. total_samples の計算を ensure() 前に完結させバッファ確保を一本化
 */
DLLEXPORT void execute_render(NoteEvent*  notes,
                              int         note_count,
                              const char* output_path)
{
    if (!notes || note_count <= 0 || !output_path) return;

    // ループ外で一度だけ FFT サイズを確定
    const int fft_size  = GetFFTSizeForCheapTrick(kFs, nullptr);
    const int spec_bins = fft_size / 2 + 1;

    // ---- パス1: 総サンプル数 & 最大 f0 長を先行計算 ----
    int     max_f0        = 0;
    int64_t total_samples = 0;
    for (int i = 0; i < note_count; ++i) {
        const int f0_len = notes[i].pitch_length;
        if (f0_len > max_f0) max_f0 = f0_len;
        total_samples += static_cast<int64_t>((f0_len - 1) * kFramePeriod / 1000.0 * kFs) + 1;
    }

    // スクラッチバッファを必要サイズに拡張（縮小しない）
    tl_scratch.ensure(max_f0, spec_bins);

    std::vector<double> full_song_buffer(total_samples, 0.0);
    int64_t current_offset = 0;

    // ---- パス2: ノートごとの合成 ----
    for (int i = 0; i < note_count; ++i) {
        NoteEvent& n = notes[i];

        // ボイスの存在確認
        const EmbeddedVoice* ev_ptr = nullptr;
        {
            std::lock_guard<std::mutex> lock(g_voice_db_mutex);
            auto it = g_voice_db.find(n.wav_path ? n.wav_path : "");
            if (it != g_voice_db.end()) ev_ptr = &it->second;
        }

        const int64_t note_samples =
            static_cast<int64_t>((n.pitch_length - 1) * kFramePeriod / 1000.0 * kFs) + 1;

        if (!ev_ptr) {
            // 音源が見つからない場合は無音でスキップ
            current_offset += note_samples;
            continue;
        }

        const EmbeddedVoice& ev = *ev_ptr;
        const int f0_len = n.pitch_length;

        // ---- タイムストレッチ軸の計算 ----
        std::vector<double> time_axis(f0_len);
        const double src_dur = static_cast<double>(ev.waveform.size()) / kFs;
        const double inv_f0_len_m1 = (f0_len > 1) ? (1.0 / (f0_len - 1)) : 0.0;
        for (int j = 0; j < f0_len; ++j) {
            time_axis[j] = static_cast<double>(j) * inv_f0_len_m1 * src_dur;
        }

        // ---- WORLD 解析（固定 F0 でスペクトル抽出） ----
        // スクラッチバッファへ直接書き込む
        std::vector<double> f0_analysis(f0_len, 150.0);

        CheapTrick(ev.waveform.data(),
                   static_cast<int>(ev.waveform.size()),
                   kFs,
                   time_axis.data(), f0_analysis.data(), f0_len,
                   nullptr,
                   tl_scratch.spec_ptrs.data());

        D4C(ev.waveform.data(),
            static_cast<int>(ev.waveform.size()),
            kFs,
            time_axis.data(), f0_analysis.data(), f0_len,
            fft_size,
            nullptr,
            tl_scratch.ap_ptrs.data());

        // ---- パラメータ・シェイピング ----
        double* const spec_tmp = tl_scratch.spec_tmp.data();  // heap 確保なし

        for (int j = 0; j < f0_len; ++j) {
            double* spec_row = tl_scratch.spec_ptrs[j];
            double* ap_row   = tl_scratch.ap_ptrs[j];

            const double shift   = (n.gender_curve[j]  - 0.5) * 0.4;
            const double tension = n.tension_curve[j];
            const double breath  = n.breath_curve[j];

            // --- Gender: フォルマントシフト ---
            // spec_tmp へ退避してからリサンプリング
            memcpy(spec_tmp, spec_row, sizeof(double) * spec_bins);

            for (int k = 0; k < spec_bins; ++k) {
                const double target_k = static_cast<double>(k) * (1.0 + shift);
                const int    k0       = static_cast<int>(target_k);

                if (k0 >= spec_bins - 1) {
                    // 境界外: 末端値でクランプ（v1/v2 の未処理バグを修正）
                    spec_row[k] = spec_tmp[spec_bins - 1];
                } else {
                    const double frac = target_k - k0;
                    spec_row[k] = (1.0 - frac) * spec_tmp[k0] + frac * spec_tmp[k0 + 1];
                }
            }

            // --- Tension / Breath ---
            const double inv_bins_m1 = 1.0 / (spec_bins - 1);
            for (int k = 0; k < spec_bins; ++k) {
                const double freq_w = static_cast<double>(k) * inv_bins_m1;
                spec_row[k] *= (1.0 + (tension - 0.5) * freq_w);
                ap_row[k]    = std::clamp(ap_row[k] + (breath * freq_w), 0.0, 1.0);
            }
        }

        // ---- 波形合成 ----
        if (current_offset + note_samples <= total_samples) {
            Synthesis(n.pitch_curve, f0_len,
                      tl_scratch.spec_ptrs.data(),
                      tl_scratch.ap_ptrs.data(),
                      fft_size, kFramePeriod, kFs,
                      static_cast<int>(note_samples),
                      &full_song_buffer[current_offset]);
        }
        current_offset += note_samples;
    }

    // ---- ファイル書き出し ----
    wavwrite(full_song_buffer.data(),
             static_cast<int>(full_song_buffer.size()),
             kFs, 16, output_path);
}

} // extern "C"

