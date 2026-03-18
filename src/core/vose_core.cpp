#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <random>
#include <cstring>
#include <cstdint>
#include <mutex>
#include <shared_mutex>
#include <memory>
#define _USE_MATH_DEFINES
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include "vose_core.h"
#include "voice_data.h"

#include "world/synthesis.h"
#include "world/cheaptrick.h"
#include "world/d4c.h"
#include "world/harvest.h"
#include "world/audioio.h"
#include "world/constantnumbers.h"

// LOCK ORDER POLICY:
//  1) Acquire g_analysis_cache_mutex (shared or unique) first.
//  2) Then acquire g_voice_db_mutex (shared or unique).
//  Never acquire locks in the reverse order.


// ============================================================
// データ構造
// ============================================================

struct EmbeddedVoice {
    std::vector<double> waveform;
    int fs;
};

static std::map<std::string, std::shared_ptr<const EmbeddedVoice>> g_voice_db;
static std::shared_mutex g_voice_db_mutex;

// ============================================================
// AnalysisCache
//
// EmbeddedVoice 1音源につき1エントリ。
// Harvest / CheapTrick / D4C の結果をすべて保持する。
// 音源データは不変（load_embedded_resource 後に書き換えない前提）なので
// 解析結果もキャッシュ後は不変 → shared_lock での読み取りが安全。
// ============================================================

struct AnalysisCache {
    // F0 / タイムスタンプ（Harvest出力）
    std::vector<double> f0;
    std::vector<double> time;
    int                 length = 0;      // 有効フレーム数

    // スペクトル包絡・非周期性（CheapTrick / D4C 出力）
    // フラット配列 [frame * spec_bins] で保持
    std::vector<double> flat_spec;
    std::vector<double> flat_ap;
    int                 spec_bins = 0;
};

static std::map<std::shared_ptr<const EmbeddedVoice>, std::shared_ptr<const AnalysisCache>> g_analysis_cache;
static std::shared_mutex g_analysis_cache_mutex;
// ============================================================
// oto.ini 受け渡し
// ============================================================

extern "C" void set_voice_library(const char* voice_path);
extern "C" void set_oto_data(const OtoEntry* entries, int count);

// ============================================================
// NoteState / NotePrepass
// ============================================================

enum class NoteState : uint8_t {
    INVALID,
    NO_VOICE,
    RENDERABLE,
};

struct NotePrepass {
    NoteState                            state        = NoteState::INVALID;
    int64_t                              note_samples = 0;
    std::shared_ptr<const EmbeddedVoice> ev;
    std::shared_ptr<const EmbeddedVoice> prev_ev;

    NotePrepass() = default;
    NotePrepass(NoteState s, int64_t ns,
                std::shared_ptr<const EmbeddedVoice> e,
                std::shared_ptr<const EmbeddedVoice> pe = nullptr)
        : state(s), note_samples(ns), ev(std::move(e)), prev_ev(std::move(pe)) {}
};

// ============================================================
// SynthesisScratchPad
//
// キャッシュ導入後は「キャッシュから作業バッファへのコピー先」として使う。
// ポインタ配列（spec_ptrs / ap_ptrs）は作業バッファを指したまま維持する。
// prev 用の作業バッファも保持するが、get_or_analyze() がコピーまで面倒を見るので
// execute_render 側は "cur" / "prev" のどちらのバッファを使うかだけ意識すればよい。
// ============================================================

struct SynthesisScratchPad {
    // カレント音素用作業バッファ
    std::vector<double>  flat_spec;
    std::vector<double>  flat_ap;
    std::vector<double>  spec_tmp;
    std::vector<double*> spec_ptrs;
    std::vector<double*> ap_ptrs;
    std::vector<double>  f0;
    std::vector<double>  time;

    // 前音素用作業バッファ
    std::vector<double>  flat_spec_prev;
    std::vector<double>  flat_ap_prev;
    std::vector<double*> spec_ptrs_prev;
    std::vector<double*> ap_ptrs_prev;
    std::vector<double>  f0_prev;
    std::vector<double>  time_prev;

    std::vector<std::vector<double>> mod_ap_buffer;
    std::vector<double*> mod_ap_ptrs;

    int reserved_f0   = 0;
    int reserved_bins = 0;

    // f0_length × spec_bins 分の領域を確保し、ポインタ配列を必ず正しく設定する。
    //
    // 【設計メモ】
    // resize が容量不足時に realloc を行うと flat_spec 等の先頭アドレスが変わる。
    // そのため「realloc が起きたときだけポインタを更新する」実装では、
    //
    //   (A) ensure_spec(cur_len,  bins) → flat_spec realloc なし → ポインタ更新なし
    //   (B) copy_cache_to_scratch_cur() → flat_spec[0..] に書き込む
    //   (C) ensure_spec(prev_len, bins) → prev_len > cur_len なので realloc 発生
    //       → flat_spec の内容は vector が新領域へ移動するので壊れないが
    //          spec_ptrs[i] は旧アドレスを指したままダングリング化する
    //
    // という問題が生じる。これを防ぐため、ポインタ再設定を resize の後に
    // 無条件で実行する。reserved_f0 個の単純なポインタ代入なので
    // リビルドしないケースのオーバーヘッドは無視できる。
    void ensure_spec(int f0_length, int spec_bins) {
        if (f0_length > reserved_f0 || spec_bins > reserved_bins) {
            reserved_f0   = std::max(f0_length,  reserved_f0);
            reserved_bins = std::max(spec_bins,  reserved_bins);

            flat_spec     .resize(static_cast<size_t>(reserved_f0) * reserved_bins);
            flat_ap       .resize(static_cast<size_t>(reserved_f0) * reserved_bins);
            spec_tmp      .resize(reserved_bins);
            spec_ptrs     .resize(reserved_f0);
            ap_ptrs       .resize(reserved_f0);

            flat_spec_prev.resize(static_cast<size_t>(reserved_f0) * reserved_bins);
            flat_ap_prev  .resize(static_cast<size_t>(reserved_f0) * reserved_bins);
            spec_ptrs_prev.resize(reserved_f0);
            ap_ptrs_prev  .resize(reserved_f0);
            mod_ap_buffer.assign(reserved_f0, std::vector<double>(reserved_bins));
            mod_ap_ptrs.resize(reserved_f0);
        }
        // realloc の有無に関わらず常に再設定する（ダングリング防止）
        for (int i = 0; i < reserved_f0; ++i) {
            spec_ptrs     [i] = &flat_spec     [static_cast<size_t>(i) * reserved_bins];
            ap_ptrs       [i] = &flat_ap       [static_cast<size_t>(i) * reserved_bins];
            spec_ptrs_prev[i] = &flat_spec_prev[static_cast<size_t>(i) * reserved_bins];
            ap_ptrs_prev  [i] = &flat_ap_prev  [static_cast<size_t>(i) * reserved_bins];
            mod_ap_ptrs   [i] = mod_ap_buffer[i].data();
        }
    }

    void ensure_f0(int length) {
        if (length > static_cast<int>(f0.size())) {
            f0  .resize(length);
            time.resize(length);
        }
    }

    void ensure_f0_prev(int length) {
        if (length > static_cast<int>(f0_prev.size())) {
            f0_prev  .resize(length);
            time_prev.resize(length);
        }
    }
};

static thread_local SynthesisScratchPad tl_scratch;

// ============================================================
// 定数
// ============================================================

static constexpr int    kFs               = 44100;
static constexpr double kFramePeriod      = 5.0;
static constexpr double kInv32768         = 1.0 / 32768.0;
static constexpr int    kCrossfadeSamples = static_cast<int>(kFs * 0.030);
static constexpr int    kMaxPitchLength   = 120000;
static constexpr int    kTransitionFrames = static_cast<int>(60.0 / kFramePeriod);

static int64_t note_samples_safe(int pitch_length)
{
    return (static_cast<int64_t>(pitch_length) - 1)
           * kFramePeriod / 1000.0 * kFs + 1;
}

// ============================================================
// find_voice_ref
// ============================================================

static std::shared_ptr<const EmbeddedVoice> find_voice_ref(const char* key)
{
    std::shared_lock<std::shared_mutex> lock(g_voice_db_mutex);
    auto it = g_voice_db.find(key ? key : "");
    if (it == g_voice_db.end()) return nullptr;
    return it->second;
}

// ============================================================
// build_analysis_cache
//
// キャッシュミス時にのみ呼ばれる。
// Harvest → F0補完 → CheapTrick → D4C を実行し AnalysisCache を生成する。
// 呼び出し元は書き込みロックを取得済みであること。
// ============================================================

static std::shared_ptr<const AnalysisCache>
build_analysis_cache(const EmbeddedVoice& ev, int fft_size, int spec_bins)
{
    auto cache = std::make_shared<AnalysisCache>();
    cache->spec_bins = spec_bins;

    // --- Harvest ---
    HarvestOption opt;
    InitializeHarvestOption(&opt);
    opt.frame_period = kFramePeriod;
    opt.f0_floor     = 50.0;
    opt.f0_ceil      = 800.0;

    const int wav_len     = static_cast<int>(ev.waveform.size());
    const int harvest_len = GetSamplesForHarvest(ev.fs, wav_len, kFramePeriod);

    cache->f0  .resize(harvest_len);
    cache->time.resize(harvest_len);
    cache->length = harvest_len;

    Harvest(ev.waveform.data(), wav_len, ev.fs, &opt,
            cache->time.data(), cache->f0.data());

    // --- F0補完: 無声区間を前後の有声値で線形補間 (FIX-④) ---
    {
        std::vector<int> voiced_idx;
        voiced_idx.reserve(harvest_len);
        for (int i = 0; i < harvest_len; ++i)
            if (cache->f0[i] > 0.0)
                voiced_idx.push_back(i);

        if (!voiced_idx.empty()) {
            for (int i = 0; i < voiced_idx.front(); ++i)
                cache->f0[i] = cache->f0[voiced_idx.front()];
            for (int i = voiced_idx.back() + 1; i < harvest_len; ++i)
                cache->f0[i] = cache->f0[voiced_idx.back()];
            for (int vi = 0; vi + 1 < static_cast<int>(voiced_idx.size()); ++vi) {
                const int    ia = voiced_idx[vi];
                const int    ib = voiced_idx[vi + 1];
                if (ib - ia <= 1) continue;
                const double fa = cache->f0[ia];
                const double fb = cache->f0[ib];
                for (int i = ia + 1; i < ib; ++i) {
                    const double t = static_cast<double>(i - ia) / (ib - ia);
                    cache->f0[i]  = fa + t * (fb - fa);
                }
            }
        } else {
            std::fill(cache->f0.begin(), cache->f0.end(), 440.0);
        }
    }

    // --- CheapTrick / D4C ---
    // spec_ptrs / ap_ptrs はキャッシュ内フラット配列を指すローカルポインタ配列
    cache->flat_spec.resize(static_cast<size_t>(harvest_len) * spec_bins);
    cache->flat_ap  .resize(static_cast<size_t>(harvest_len) * spec_bins);

    std::vector<double*> sp(harvest_len), ap(harvest_len);
    for (int i = 0; i < harvest_len; ++i) {
        sp[i] = &cache->flat_spec[static_cast<size_t>(i) * spec_bins];
        ap[i] = &cache->flat_ap  [static_cast<size_t>(i) * spec_bins];
    }

    CheapTrick(ev.waveform.data(), wav_len, ev.fs,
               cache->time.data(), cache->f0.data(),
               harvest_len, nullptr, sp.data());

    D4C(ev.waveform.data(), wav_len, ev.fs,
        cache->time.data(), cache->f0.data(),
        harvest_len, fft_size, nullptr, ap.data());

    return cache;
}

// ============================================================
// get_or_analyze
//
// 指定音源のキャッシュを返す。未キャッシュなら build_analysis_cache() を呼ぶ。
// スレッドセーフ（double-checked locking パターン）。
// ============================================================

static std::shared_ptr<const AnalysisCache>
get_or_analyze(std::shared_ptr<const EmbeddedVoice> ev_sp, int fft_size, int spec_bins)
{
    // --- まず共有ロックでキャッシュ確認 ---
    {
        std::shared_lock<std::shared_mutex> rlock(g_analysis_cache_mutex);
        auto it = g_analysis_cache.find(ev_sp);
        if (it != g_analysis_cache.end()) return it->second;
    }

    // --- キャッシュミス: 排他ロックを取り直して再確認してから生成 ---
    std::unique_lock<std::shared_mutex> wlock(g_analysis_cache_mutex);
    auto it = g_analysis_cache.find(ev_sp);
    if (it != g_analysis_cache.end()) return it->second;

    auto cache = build_analysis_cache(*ev_sp, fft_size, spec_bins);
    g_analysis_cache[ev_sp] = cache;
    return cache;
}

// ============================================================
// copy_cache_to_scratch
//
// AnalysisCache の内容を tl_scratch の cur バッファへコピーし、
// ポインタ配列（spec_ptrs / ap_ptrs）を再設定する。
// キャッシュは読み取り専用なので、変形処理（gender / tension 等）は
// コピー先の scratch バッファに対して行う。
// ============================================================

static void copy_cache_to_scratch_cur(const AnalysisCache& c)
{
    // 防御チェック: scratch が十分な容量を確保しているかを保証
    if (tl_scratch.reserved_f0 < c.length || tl_scratch.reserved_bins < c.spec_bins)
        tl_scratch.ensure_spec(c.length, c.spec_bins);
    
    const size_t total = static_cast<size_t>(c.length) * c.spec_bins;
    std::copy(c.flat_spec.begin(), c.flat_spec.begin() + total,
              tl_scratch.flat_spec.begin());
    std::copy(c.flat_ap  .begin(), c.flat_ap  .begin() + total,
              tl_scratch.flat_ap  .begin());
    tl_scratch.ensure_f0(c.length);
    std::copy(c.f0  .begin(), c.f0  .begin() + c.length, tl_scratch.f0  .begin());
    std::copy(c.time.begin(), c.time.begin() + c.length, tl_scratch.time.begin());
}

static void copy_cache_to_scratch_prev(const AnalysisCache& c)
{
    if (tl_scratch.reserved_f0 < c.length || tl_scratch.reserved_bins < c.spec_bins)
        tl_scratch.ensure_spec(c.length, c.spec_bins);
    const size_t total = static_cast<size_t>(c.length) * c.spec_bins;
    std::copy(c.flat_spec.begin(), c.flat_spec.begin() + total,
              tl_scratch.flat_spec_prev.begin());
    std::copy(c.flat_ap  .begin(), c.flat_ap  .begin() + total,
              tl_scratch.flat_ap_prev  .begin());
    tl_scratch.ensure_f0_prev(c.length);
    std::copy(c.f0  .begin(), c.f0  .begin() + c.length, tl_scratch.f0_prev  .begin());
    std::copy(c.time.begin(), c.time.begin() + c.length, tl_scratch.time_prev.begin());
}

// ============================================================
// resample_curve
// ============================================================

static inline double resample_curve(const double* curve, int src_len,
                                     int dst_idx, int dst_len)
{
    if (!curve || src_len <= 0 || dst_len <= 0) return 0.0;
    if (dst_idx < 0) return curve[0];

    if (src_len == 1) return curve[0];
    const double t     = static_cast<double>(dst_idx) / std::max(dst_len - 1, 1);
    const double src_f = t * (src_len - 1);
    const int    j0    = static_cast<int>(src_f);
    const int    j1    = std::min(j0 + 1, src_len - 1);
    const double frac  = src_f - j0;
    return (1.0 - frac) * curve[j0] + frac * curve[j1];
}

// ============================================================
// apply_crossfade
// ============================================================

static void apply_crossfade(std::vector<double>& dst, int64_t dst_size,
                             const std::vector<double>& src, int64_t src_size,
                             int64_t offset, int xfade_len)
{
    if (offset >= dst_size) return;

    const int safe_xfade = static_cast<int>(
        std::min<int64_t>(xfade_len,
            std::min(src_size, dst_size - offset)));

    for (int s = 0; s < safe_xfade; ++s) {
        const double t       = static_cast<double>(s) / safe_xfade;
        const double fade_in = 0.5 * (1.0 - std::cos(M_PI * t));
        const int64_t di     = offset + s;
        if (di >= dst_size) break;
        dst[di] = dst[di] * (1.0 - fade_in) + src[s] * fade_in;
    }

    const int64_t body_end = std::min(offset + src_size, dst_size);
    for (int64_t s = offset + safe_xfade; s < body_end; ++s)
        dst[s] = src[s - offset];
}

// ============================================================
// apply_gender_shift  (FIX-①: 対数周波数軸シフト)
// ============================================================

static void apply_gender_shift(double* sr, int spec_bins, double gender,
                                double* tmp)
{
    if (!sr || !tmp || spec_bins <= 0) return;
    if (std::abs(gender - 0.5) < 1e-4) return;

    const double shift_ratio = std::exp((gender - 0.5) * 0.4 * std::log(2.0));

    constexpr double kFloor = 1e-12;
    for (int k = 0; k < spec_bins; ++k)
        tmp[k] = std::log(std::max(sr[k], kFloor));

    for (int k = 0; k < spec_bins; ++k) {
        const double src_k = static_cast<double>(k) / shift_ratio;
        const int    k0    = static_cast<int>(src_k);
        if (k0 >= spec_bins - 1) {
            sr[k] = std::exp(tmp[spec_bins - 1]);
        } else {
            const double frac = src_k - k0;
            sr[k] = std::exp((1.0 - frac) * tmp[k0] + frac * tmp[k0 + 1]);
        }
    }
}

// ============================================================
// apply_tension_breath  (FIX-②: 非線形シグモイド + 加重ミックス)
// ============================================================

static void apply_tension_breath(double* sr, double* ar, int spec_bins,
                                  double tension, double breath)
{
    if (!sr || !ar || spec_bins <= 1) return;
    const double inv = 1.0 / (spec_bins - 1);

    for (int k = 0; k < spec_bins; ++k) {
        const double fw = static_cast<double>(k) * inv;

        if (std::abs(tension - 0.5) > 1e-4) {
            const double sigmoid_k  = 8.0;
            const double pivot      = 0.35;
            const double weight     = 1.0 / (1.0 + std::exp(-sigmoid_k * (fw - pivot)));
            const double gain_db    = (tension - 0.5) * 12.0 * weight;
            const double clipped_db = 6.0 * std::tanh(gain_db / 6.0);
            sr[k] *= std::pow(10.0, clipped_db / 20.0);
        }

        if (std::abs(breath - 0.5) > 1e-4) {
            const double bw     = std::pow(fw, 0.7);
            const double amount = (breath - 0.5) * bw;
            if (amount >= 0.0)
                ar[k] = ar[k] + amount * (1.0 - ar[k]);
            else
                ar[k] = ar[k] + amount * ar[k];
            ar[k] = std::clamp(ar[k], 0.0, 1.0);
        }
    }
}

// ============================================================
// blend_transition_spectra  (FIX-③: 音素境界のスペクトルブレンド)
// ============================================================

static void blend_transition_spectra(
    double** spec_cur,  double** ap_cur,  int cur_len,
    double** spec_prev, double** ap_prev, int prev_len,
    int spec_bins, int transition_frames)

{
    if (!spec_cur || !spec_prev || !ap_cur || !ap_prev) return;
    if (spec_bins <= 0 || cur_len <= 0 || prev_len <= 0) return;
    const int blend_frames = std::min(transition_frames,
                                       std::min(cur_len, prev_len));
    for (int j = 0; j < blend_frames; ++j) {
        const double t      = static_cast<double>(j) / blend_frames;
        const double w_prev = 0.5 * (1.0 - std::cos(M_PI * (1.0 - t)));
        const double w_cur  = 1.0 - w_prev;

        constexpr double kFloor = 1e-12;
        const int prev_j = prev_len - blend_frames + j;

        double* sc = spec_cur [j];
        double* sp = spec_prev[std::max(0, prev_j)];
        double* ac = ap_cur   [j];
        double* ap = ap_prev  [std::max(0, prev_j)];

        for (int k = 0; k < spec_bins; ++k) {
            const double log_c = std::log(std::max(sc[k], kFloor));
            const double log_p = std::log(std::max(sp[k], kFloor));
            sc[k] = std::exp(w_cur * log_c + w_prev * log_p);
            ac[k] = std::clamp(w_cur * ac[k] + w_prev * ap[k], 0.0, 1.0);
        }
    }
}

// ============================================================
// extern "C" API
// ============================================================

extern "C" {

void init_official_engine() { register_all_embedded_voices(); }

static void load_embedded_resource_impl(const char* phoneme,
                                        const int16_t* raw_data, int sample_count,
                                        int sample_rate)
{
    if (!phoneme || !raw_data || sample_count <= 0) return;

    // ロック外でデータ構築（重い処理をロック前に済ませる）
    auto ev = std::make_shared<EmbeddedVoice>();
    ev->fs = sample_rate;

    // 一旦入力サンプルレートで格納（double）
    std::vector<double> tmp;
    tmp.resize(sample_count);
    for (int i = 0; i < sample_count; ++i)
        tmp[i] = static_cast<double>(raw_data[i]) * kInv32768;

     // 内部標準 fs にリサンプルして保存（簡易線形リサンプラ）     
    if (ev->fs != kFs) {       
    
        const double ratio = static_cast<double>(kFs) / ev->fs;
        const size_t out_len = static_cast<size_t>(std::max<int64_t>(1, static_cast<int64_t>(std::floor(sample_count * ratio))));
        ev->waveform.resize(out_len);
        for (size_t i = 0; i < out_len; ++i) {
            const double src_pos = static_cast<double>(i) / ratio;
            const size_t i0 = static_cast<size_t>(std::floor(src_pos));
            const size_t i1 = std::min(i0 + 1, tmp.size() - 1);
            const double frac = src_pos - i0;
            ev->waveform[i] = (1.0 - frac) * tmp[i0] + frac * tmp[i1];
        }
        ev->fs = kFs;
    } else {
        ev->waveform.swap(tmp);
    }

    // [FIX-ATOMIC] キャッシュ削除と音源更新を両ロック保持中にアトミックに実行。
    std::unique_lock<std::shared_mutex> clock(g_analysis_cache_mutex); // 
    std::unique_lock<std::shared_mutex> wlock(g_voice_db_mutex);       // 後

    auto old_it = g_voice_db.find(phoneme);
    if (old_it != g_voice_db.end()) {
        auto old_sp = old_it->second;
        auto cache_it = g_analysis_cache.find(old_sp);
        if (cache_it != g_analysis_cache.end())
            g_analysis_cache.erase(cache_it);
    }
 
    g_voice_db[phoneme] = std::move(ev);               // 音源を差し替え
    // 両ロックがここでスコープアウト → アトミックに解放
}
// ヘッダ互換の C シンボル（既存の呼び出しを壊さないためのラッパー）
extern "C" DLLEXPORT void load_embedded_resource(const char* phoneme,
                                                 const int16_t* raw_data, int sample_count)
{
    // 既存の呼び出しはサンプルレート情報を渡さないため、既定値として kFs を使う
    load_embedded_resource_impl(phoneme, raw_data, sample_count, kFs);
}


}

// ============================================================
// VOSE_Synthesis
// VO-SE独自の励起信号注入 + ポストエフェクト付き合成エンジン
// ============================================================
static void VOSE_Synthesis(
    const double* f0, int f0_length,
    double** spectrogram, double** aperiodicity,
    int fft_size, double frame_period, int fs,
    int y_length, double* y)
{
    const int spec_bins = fft_size / 2 + 1;

    // --- 1. Aperiodicity の変調（ジッター・ブレス） ---
    // tl_scratch の mod_ap_ptrs を使用
    tl_scratch.ensure_spec(f0_length, spec_bins);
    double** mod_ap = tl_scratch.mod_ap_ptrs.data();

    static thread_local std::mt19937 rng(42);
    std::uniform_real_distribution<double> dist(-0.02, 0.02);

    for (int i = 0; i < f0_length; ++i) {
        double* ap_dst = mod_ap[i];
        double* ap_src = aperiodicity[i];

        double delta_f0 = 0.0;
        if (i > 0 && i < f0_length - 1)
            delta_f0 = std::abs(f0[i+1] - f0[i-1]) * 0.5;

        double vibrato_breath = std::min(0.15, delta_f0 * 0.003);

        for (int k = 0; k < spec_bins; ++k) {
            double freq = static_cast<double>(k) * fs / fft_size;
            double current_ap = ap_src[k];

            if (freq > 2000.0) {
                double jitter = dist(rng);
                current_ap += vibrato_breath + jitter;
            }

            ap_dst[k] = std::clamp(current_ap, 0.0, 1.0);
        }
    }

    // --- 2. WORLD Synthesis（変調済み AP を使用） ---
    Synthesis(f0, f0_length,
              spectrogram, mod_ap,
              fft_size, frame_period, fs,
              y_length, y);

    // --- 3. 高域エキサイター（ポストエフェクト） ---
    double alpha = 0.85;
    double prev_x = 0.0;
    double prev_y_hp = 0.0;

    for (int i = 0; i < y_length; ++i) {
        double x = y[i];
        double hp = x - prev_x + alpha * prev_y_hp;
        prev_x = x;
        prev_y_hp = hp;

        y[i] = x + hp * 0.05;   // 高域を 5% 加算
    }
}



// ============================================================
// extern "C" API (execute_render の完全な置き換え)
// ============================================================

// 統合版 execute_render
extern "C" {
DLLEXPORT void execute_render(NoteEvent* notes, int note_count, const char* output_path)
{
    if (!notes || note_count <= 0 || !output_path) return;

    // スレッド安全のためのロック（最初の実装に合わせる）
    std::unique_lock<std::shared_mutex> clock(g_analysis_cache_mutex);
    std::unique_lock<std::shared_mutex> wlock(g_voice_db_mutex);

    const int fft_size  = GetFFTSizeForCheapTrick(kFs, nullptr);
    const int spec_bins = fft_size / 2 + 1;

    // ----------------------------------------------------------------
    // パス1: NotePrepass 構築
    // ----------------------------------------------------------------
    std::vector<NotePrepass> prepass(note_count);

    int     max_harvest_len = 0;
    int64_t total_samples   = 0;
    int     xfade_count     = 0;
    bool    prev_renderable = false;
    std::shared_ptr<const EmbeddedVoice> last_ev;

    for (int i = 0; i < note_count; ++i) {
        const int pitch_len = notes[i].pitch_length;

        if (pitch_len <= 0 || pitch_len > kMaxPitchLength) {
            std::fprintf(stderr,
                "[vose_core] note[%d] pitch_length=%d out of range (1..%d), skipping.\n",
                i, pitch_len, kMaxPitchLength);
            prepass[i]      = NotePrepass(NoteState::INVALID, 0, nullptr);
            prev_renderable = false;
            last_ev         = nullptr;
            continue;
        }

        const int64_t ns = note_samples_safe(pitch_len);
        auto ev = find_voice_ref(notes[i].wav_path);

        if (ev) {
            prepass[i] = NotePrepass(NoteState::RENDERABLE, ns, ev,
                                     prev_renderable ? last_ev : nullptr);
            if (prev_renderable) ++xfade_count;
            prev_renderable = true;
            last_ev = ev;

            const int wav_len     = static_cast<int>(ev->waveform.size());
            const int harvest_len = GetSamplesForHarvest(ev->fs, wav_len, kFramePeriod);
            if (harvest_len > max_harvest_len) max_harvest_len = harvest_len;
        } else {
            prepass[i]      = NotePrepass(NoteState::NO_VOICE, ns, nullptr);
            prev_renderable = false;
            last_ev         = nullptr;
        }

        total_samples += ns;
    }

    total_samples -= static_cast<int64_t>(kCrossfadeSamples) * xfade_count;
    if (total_samples <= 0) return;

    tl_scratch.ensure_spec(max_harvest_len, spec_bins);
    std::vector<double> full_song_buffer(total_samples, 0.0);
    std::vector<double> note_buf;

    static constexpr double kDefaultPitch   = 440.0;
    static constexpr double kDefaultGender  = 0.5;
    static constexpr double kDefaultTension = 0.5;
    static constexpr double kDefaultBreath  = 0.5;

    int64_t current_offset     = 0;
    bool    last_note_rendered = false;

    // ----------------------------------------------------------------
    // パス2: ノートごとの合成（VOSE_Synthesis + 高域エキサイター）
    // ----------------------------------------------------------------
    for (int idx = 0; idx < note_count; ++idx) {
        const NotePrepass& pp = prepass[idx];

        switch (pp.state) {
        case NoteState::INVALID:
            last_note_rendered = false;
            continue;

        case NoteState::NO_VOICE:
            last_note_rendered = false;
            current_offset    += pp.note_samples;
            continue;

        case NoteState::RENDERABLE:
            break;
        }

        NoteEvent& n               = notes[idx];
        const int64_t note_samples = pp.note_samples;
        const int     f0_len       = n.pitch_length;

        // --- キャッシュ取得（ミス時のみ Harvest / CheapTrick / D4C を実行） ---
        auto cache_cur = get_or_analyze(pp.ev, fft_size, spec_bins);

        tl_scratch.ensure_spec(cache_cur->length, spec_bins);
        copy_cache_to_scratch_cur(*cache_cur);

        const int harvest_len = cache_cur->length;

        // --- 前音素ブレンド（存在する場合） ---
        if (pp.prev_ev) {
            auto cache_prev = get_or_analyze(pp.prev_ev, fft_size, spec_bins);
            tl_scratch.ensure_spec(
                std::max(harvest_len, cache_prev->length), spec_bins);
            copy_cache_to_scratch_prev(*cache_prev);

            blend_transition_spectra(
                tl_scratch.spec_ptrs.data(), tl_scratch.ap_ptrs.data(), harvest_len,
                tl_scratch.spec_ptrs_prev.data(), tl_scratch.ap_ptrs_prev.data(),
                cache_prev->length, spec_bins, kTransitionFrames);
        }

        // --- ピッチカーブ適用 ---
        for (int j = 0; j < harvest_len; ++j) {
            tl_scratch.f0[j] = n.pitch_curve
                ? resample_curve(n.pitch_curve, f0_len, j, harvest_len)
                : kDefaultPitch;
        }

        // --- ジェンダー / テンション / ブレス適用 ---
        double* const spec_tmp = tl_scratch.spec_tmp.data();
        for (int j = 0; j < harvest_len; ++j) {
            double* sr = tl_scratch.spec_ptrs[j];
            double* ar = tl_scratch.ap_ptrs[j];

            const double gender  = n.gender_curve
                ? resample_curve(n.gender_curve,  f0_len, j, harvest_len) : kDefaultGender;
            const double tension = n.tension_curve
                ? resample_curve(n.tension_curve, f0_len, j, harvest_len) : kDefaultTension;
            const double breath  = n.breath_curve
                ? resample_curve(n.breath_curve,  f0_len, j, harvest_len) : kDefaultBreath;

            apply_gender_shift   (sr, spec_bins, gender, spec_tmp);
            apply_tension_breath (sr, ar, spec_bins, tension, breath);
        }

        // --- VOSE_Synthesis 呼び出し（VO-SE独自合成器） ---
        note_buf.assign(static_cast<size_t>(note_samples), 0.0);
        VOSE_Synthesis(tl_scratch.f0.data(), harvest_len,
                       tl_scratch.spec_ptrs.data(), tl_scratch.ap_ptrs.data(),
                       fft_size, kFramePeriod, pp.ev->fs,
                       static_cast<int>(note_samples), note_buf.data());

        // --- ポストエフェクト: 高域エキサイター（修正版） ---
        // パラメータ
        const double exciter_alpha = 0.85;
        const double exciter_gain  = 0.05; // 高域を戻す割合
        double prev_x = 0.0;
        double prev_y_hp = 0.0;

        // note_buf の長さは note_samples（境界チェック済み）
        for (int s = 0; s < static_cast<int>(note_buf.size()); ++s) {
            double y_sample = note_buf[s];
            double hp = y_sample - prev_x + exciter_alpha * prev_y_hp;
            prev_x = y_sample;
            prev_y_hp = hp;
            // 元信号に高域成分を少し加える
            note_buf[s] = y_sample + (hp * exciter_gain);
        }

        // --- クロスフェード書き込み ---
        const bool    do_xfade     = last_note_rendered;
        const int64_t write_offset = do_xfade ? current_offset - kCrossfadeSamples : current_offset;
        const int xfade = do_xfade ? kCrossfadeSamples : 0;

        apply_crossfade(full_song_buffer, total_samples,
                        note_buf, note_samples, write_offset, xfade);

        current_offset += do_xfade ? note_samples - kCrossfadeSamples : note_samples;
        last_note_rendered = true;
    }

    // --- wav 出力 ---
    wavwrite(
        full_song_buffer.data(),
        static_cast<int>(full_song_buffer.size()),
        kFs, 16, output_path);
}
} // extern "C"
