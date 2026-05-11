//vose_core.cpp

#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>    
#include <shared_mutex> // std::shared_mutex 用
#include <random>
#include <sstream>
#include <cstring>
#include <cstdint>
#include <future>
#include <thread>
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

namespace fs = std::filesystem;

// ============================================================
// FNV-1a ハッシュ
// ============================================================

static uint64_t fnv1a_hash(const std::string& str) {
    uint64_t hash = 0xcbf29ce484222325ULL;
    for (char c : str) {
        hash ^= static_cast<uint64_t>(c);
        hash *= 0x100000001b3ULL;
    }
    return hash;
}

static std::string generate_cache_hash(const std::string& wav_path) {
    try {
        fs::path p(wav_path);
        if (!fs::exists(p)) return "0000000000000000";
        auto last_time = static_cast<long long>(
            fs::last_write_time(p).time_since_epoch().count());
        auto file_size = static_cast<unsigned long long>(fs::file_size(p));
        std::string seed = p.string() + std::to_string(last_time)
                                      + std::to_string(file_size);
        uint64_t h = fnv1a_hash(seed);
        std::stringstream ss;
        ss << std::hex << std::setw(16) << std::setfill('0') << h;
        return ss.str();
    } catch (...) {
        return "error_hash";
    }
}

// ============================================================
// oto.ini DB
// ============================================================

static std::map<std::string, OtoEntry> g_oto_db;
static std::shared_mutex g_oto_db_mutex;

extern "C" void set_oto_data(const OtoEntry* entries, int count) {
    std::unique_lock<std::shared_mutex> lock(g_oto_db_mutex);
    g_oto_db.clear();
    if (!entries || count <= 0) return;
    for (int i = 0; i < count; ++i)
        g_oto_db[entries[i].alias] = entries[i];
}

// ============================================================
// データ構造
// ============================================================

struct EmbeddedVoice {
    std::string         path;
    std::vector<double> waveform;
    int                 fs;
};

static std::map<std::string, std::shared_ptr<const EmbeddedVoice>> g_voice_db;
static std::shared_mutex g_voice_db_mutex;

struct AnalysisCache {
    std::vector<double> f0;
    std::vector<double> time;
    int                 length    = 0;
    std::vector<double> flat_spec;
    std::vector<double> flat_ap;
    int                 spec_bins = 0;
};

static std::map<std::shared_ptr<const EmbeddedVoice>,
                std::shared_ptr<const AnalysisCache>> g_analysis_cache;
static std::shared_mutex g_analysis_cache_mutex;

// ============================================================
// NoteState / NotePrepass
// ============================================================

enum class NoteState : uint8_t { INVALID, NO_VOICE, RENDERABLE };

struct NotePrepass {
    NoteState                            state        = NoteState::INVALID;
    int64_t                              note_samples = 0;
    std::shared_ptr<const EmbeddedVoice> ev;
    std::shared_ptr<const EmbeddedVoice> prev_ev;
    const OtoEntry*                      oto          = nullptr;

    NotePrepass() = default;
    NotePrepass(NoteState s, int64_t ns,
                std::shared_ptr<const EmbeddedVoice> e,
                std::shared_ptr<const EmbeddedVoice> pe = nullptr,
                const OtoEntry* o = nullptr)
        : state(s), note_samples(ns), ev(std::move(e)),
          prev_ev(std::move(pe)), oto(o) {}
};

// ============================================================
// SynthesisScratchPad
// ============================================================

struct SynthesisScratchPad {
    std::vector<double>  flat_spec, flat_ap, spec_tmp;
    std::vector<double*> spec_ptrs, ap_ptrs;
    std::vector<double>  f0, time_axis;

    std::vector<double>  flat_spec_prev, flat_ap_prev;
    std::vector<double*> spec_ptrs_prev, ap_ptrs_prev;
    std::vector<double>  f0_prev, time_axis_prev;

    std::vector<double>  flat_mod_ap;
    std::vector<double*> mod_ap_ptrs;

    int reserved_f0 = 0, reserved_bins = 0;

    void ensure_spec(int f0_length, int spec_bins) {
        if (f0_length > reserved_f0 || spec_bins > reserved_bins) {
            reserved_f0   = std::max(f0_length,  reserved_f0);
            reserved_bins = std::max(spec_bins,  reserved_bins);
            const size_t total = static_cast<size_t>(reserved_f0) * reserved_bins;
            flat_spec     .resize(total); flat_ap      .resize(total);
            spec_tmp      .resize(reserved_bins);
            spec_ptrs     .resize(reserved_f0); ap_ptrs      .resize(reserved_f0);
            flat_spec_prev.resize(total); flat_ap_prev .resize(total);
            spec_ptrs_prev.resize(reserved_f0); ap_ptrs_prev .resize(reserved_f0);
            flat_mod_ap   .resize(total); mod_ap_ptrs  .resize(reserved_f0);
        }
        for (int i = 0; i < reserved_f0; ++i) {
            const size_t off  = static_cast<size_t>(i) * reserved_bins;
            spec_ptrs     [i] = &flat_spec     [off];
            ap_ptrs       [i] = &flat_ap       [off];
            spec_ptrs_prev[i] = &flat_spec_prev[off];
            ap_ptrs_prev  [i] = &flat_ap_prev  [off];
            mod_ap_ptrs   [i] = &flat_mod_ap   [off];
        }
    }

    void ensure_f0(int n) {
        if (n > static_cast<int>(f0.size())) {
            f0.resize(n); time_axis.resize(n);
        }
    }
    void ensure_f0_prev(int n) {
        if (n > static_cast<int>(f0_prev.size())) {
            f0_prev.resize(n); time_axis_prev.resize(n);
        }
    }
};

static thread_local SynthesisScratchPad tl_scratch;

// ============================================================
// 定数
// ============================================================

static constexpr int    kFs               = 44100;
static constexpr double kFramePeriod      = 5.0;   // ms
static constexpr double kInv32768         = 1.0 / 32768.0;
static constexpr int    kCrossfadeSamples = static_cast<int>(kFs * 0.030);
static constexpr int    kMaxPitchLength   = 120000;
static constexpr int    kTransitionFrames = static_cast<int>(60.0 / kFramePeriod);

static int64_t note_samples_safe(int p) {
    return (static_cast<int64_t>(p) - 1) * kFramePeriod / 1000.0 * kFs + 1;
}

// ============================================================
// find_voice_ref
// ============================================================

static std::shared_ptr<const EmbeddedVoice> find_voice_ref(const char* key)
{
    std::shared_lock<std::shared_mutex> lock(g_voice_db_mutex);
    auto it = g_voice_db.find(key ? key : "");
    return (it != g_voice_db.end()) ? it->second : nullptr;
}

// ============================================================
// ディスクキャッシュ
// ============================================================

static fs::path get_cache_dir() {
    fs::path p = "cache";
    if (!fs::exists(p)) fs::create_directories(p);
    return p;
}

static void save_cache(const fs::path& cache_path, const AnalysisCache& cache)
{
    FILE* fp = fopen(cache_path.string().c_str(), "wb");
    if (!fp) return;
    VoseCacheHeader header;
    header.magic     = 0x45534F56;
    header.length    = cache.length;
    header.spec_bins = cache.spec_bins;
    fwrite(&header, sizeof(header), 1, fp);
    fwrite(cache.f0.data(),        sizeof(double), cache.length, fp);
    fwrite(cache.time.data(),      sizeof(double), cache.length, fp);
    const size_t sc = static_cast<size_t>(cache.length) * cache.spec_bins;
    fwrite(cache.flat_spec.data(), sizeof(double), sc, fp);
    fwrite(cache.flat_ap.data(),   sizeof(double), sc, fp);
    fclose(fp);
}

static std::shared_ptr<AnalysisCache> load_cache(const fs::path& path)
{
    if (!fs::exists(path)) return nullptr;
    std::ifstream ifs(path, std::ios::binary);
    VoseCacheHeader header;
    ifs.read(reinterpret_cast<char*>(&header), sizeof(header));
    if (header.magic != 0x45534F56) return nullptr;
    auto cache = std::make_shared<AnalysisCache>();
    cache->length    = header.length;
    cache->spec_bins = header.spec_bins;
    cache->f0  .resize(cache->length);
    cache->time.resize(cache->length);
    const size_t sc = static_cast<size_t>(cache->length) * cache->spec_bins;
    cache->flat_spec.resize(sc);
    cache->flat_ap  .resize(sc);
    ifs.read(reinterpret_cast<char*>(cache->f0.data()),        sizeof(double)*cache->length);
    ifs.read(reinterpret_cast<char*>(cache->time.data()),      sizeof(double)*cache->length);
    ifs.read(reinterpret_cast<char*>(cache->flat_spec.data()), sizeof(double)*sc);
    ifs.read(reinterpret_cast<char*>(cache->flat_ap.data()),   sizeof(double)*sc);
    return cache;
}

// ============================================================
// build_analysis_cache
// ============================================================

static std::shared_ptr<const AnalysisCache>
build_analysis_cache(const EmbeddedVoice& ev, int fft_size, int spec_bins)
{
    auto cache = std::make_shared<AnalysisCache>();
    cache->spec_bins = spec_bins;

    HarvestOption opt;
    InitializeHarvestOption(&opt);
    opt.frame_period = kFramePeriod;
    opt.f0_floor     = 50.0;
    opt.f0_ceil      = 800.0;

    const int wav_len     = static_cast<int>(ev.waveform.size());
    const int harvest_len = GetSamplesForHarvest(ev.fs, wav_len, kFramePeriod);
    cache->f0.resize(harvest_len);
    cache->time.resize(harvest_len);
    cache->length = harvest_len;

    Harvest(ev.waveform.data(), wav_len, ev.fs, &opt,
            cache->time.data(), cache->f0.data());

    // F0補完: 無声区間を前後の有声値で線形補間
    {
        std::vector<int> vi;
        vi.reserve(harvest_len);
        for (int i = 0; i < harvest_len; ++i)
            if (cache->f0[i] > 0.0) vi.push_back(i);

        if (!vi.empty()) {
            for (int i = 0; i < vi.front(); ++i)
                cache->f0[i] = cache->f0[vi.front()];
            for (int i = vi.back()+1; i < harvest_len; ++i)
                cache->f0[i] = cache->f0[vi.back()];
            for (int v = 0; v+1 < static_cast<int>(vi.size()); ++v) {
                const int ia = vi[v], ib = vi[v+1];
                if (ib-ia <= 1) continue;
                const double fa = cache->f0[ia], fb = cache->f0[ib];
                for (int i = ia+1; i < ib; ++i)
                    cache->f0[i] = fa + static_cast<double>(i-ia)/(ib-ia)*(fb-fa);
            }
        } else {
            std::fill(cache->f0.begin(), cache->f0.end(), 440.0);
        }
    }

    const size_t sc = static_cast<size_t>(harvest_len) * spec_bins;
    cache->flat_spec.resize(sc);
    cache->flat_ap  .resize(sc);

    std::vector<double*> sp(harvest_len), ap(harvest_len);
    for (int i = 0; i < harvest_len; ++i) {
        sp[i] = &cache->flat_spec[static_cast<size_t>(i)*spec_bins];
        ap[i] = &cache->flat_ap  [static_cast<size_t>(i)*spec_bins];
    }
    CheapTrick(ev.waveform.data(), wav_len, ev.fs,
               cache->time.data(), cache->f0.data(), harvest_len, nullptr, sp.data());
    D4C(ev.waveform.data(), wav_len, ev.fs,
        cache->time.data(), cache->f0.data(), harvest_len, fft_size, nullptr, ap.data());

    return cache;
}

// ============================================================
// get_or_analyze
// ============================================================

static std::shared_ptr<const AnalysisCache>
get_or_analyze(std::shared_ptr<const EmbeddedVoice> ev_sp, int fft_size, int spec_bins)
{
    {
        std::shared_lock<std::shared_mutex> rlock(g_analysis_cache_mutex);
        auto it = g_analysis_cache.find(ev_sp);
        if (it != g_analysis_cache.end()) return it->second;
    }

    const std::string h_str     = generate_cache_hash(ev_sp->path);
    const fs::path    cache_file = get_cache_dir() / (h_str + ".vsc");
    auto disk_cache = load_cache(cache_file);

    std::unique_lock<std::shared_mutex> wlock(g_analysis_cache_mutex);
    {
        auto it = g_analysis_cache.find(ev_sp);
        if (it != g_analysis_cache.end()) return it->second;
    }
    if (disk_cache) {
        g_analysis_cache[ev_sp] = disk_cache;
        return disk_cache;
    }

    auto cache = build_analysis_cache(*ev_sp, fft_size, spec_bins);
    g_analysis_cache[ev_sp] = cache;
    wlock.unlock();
    save_cache(cache_file, *cache);
    return cache;
}

// ============================================================
// UTAUタイムマッピング
// ============================================================

static double get_source_ms(const EmbeddedVoice& ev) {
    return static_cast<double>(ev.waveform.size()) / ev.fs * 1000.0;
}

static double map_time(double t_out_ms, const OtoEntry& oto,
                        double source_wav_len_ms, double note_duration_ms)
{
    const double offset     = oto.offset;
    const double fixed      = oto.consonant;
    const double cutoff_pos = (oto.cutoff < 0)
                              ? source_wav_len_ms + oto.cutoff : oto.cutoff;
    const double source_stretch = cutoff_pos - (offset + fixed);
    const double output_stretch = note_duration_ms - fixed;
    if (t_out_ms < fixed) return t_out_ms + offset;
    const double ratio = source_stretch / std::max(1.0, output_stretch);
    return (t_out_ms - fixed) * ratio + (offset + fixed);
}

// ============================================================
// copy_cache_to_scratch
// ============================================================

static void copy_cache_to_scratch_cur(const AnalysisCache& c)
{
    tl_scratch.ensure_spec(c.length, c.spec_bins);
    const size_t total = static_cast<size_t>(c.length) * c.spec_bins;
    std::copy(c.flat_spec.begin(), c.flat_spec.begin()+total, tl_scratch.flat_spec.begin());
    std::copy(c.flat_ap  .begin(), c.flat_ap  .begin()+total, tl_scratch.flat_ap  .begin());
    tl_scratch.ensure_f0(c.length);
    std::copy(c.f0  .begin(), c.f0  .begin()+c.length, tl_scratch.f0       .begin());
    std::copy(c.time.begin(), c.time.begin()+c.length, tl_scratch.time_axis.begin());
}

static void copy_cache_to_scratch_prev(const AnalysisCache& c)
{
    tl_scratch.ensure_spec(c.length, c.spec_bins);
    const size_t total = static_cast<size_t>(c.length) * c.spec_bins;
    std::copy(c.flat_spec.begin(), c.flat_spec.begin()+total, tl_scratch.flat_spec_prev.begin());
    std::copy(c.flat_ap  .begin(), c.flat_ap  .begin()+total, tl_scratch.flat_ap_prev  .begin());
    tl_scratch.ensure_f0_prev(c.length);
    std::copy(c.f0  .begin(), c.f0  .begin()+c.length, tl_scratch.f0_prev       .begin());
    std::copy(c.time.begin(), c.time.begin()+c.length, tl_scratch.time_axis_prev.begin());
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
    const double t     = static_cast<double>(dst_idx) / std::max(dst_len-1, 1);
    const double src_f = t * (src_len-1);
    const int    j0    = static_cast<int>(src_f);
    const int    j1    = std::min(j0+1, src_len-1);
    return (1.0-(src_f-j0))*curve[j0] + (src_f-j0)*curve[j1];
}

// ============================================================
// apply_crossfade
// ============================================================

static void apply_crossfade(std::vector<double>& dst, int64_t dst_size,
                             const std::vector<double>& src, int64_t src_size,
                             int64_t offset, int xfade_len)
{
    if (offset < 0 || offset >= dst_size) return;
    const int safe_xfade = static_cast<int>(
        std::min<int64_t>(xfade_len, std::min(src_size, dst_size-offset)));
    for (int s = 0; s < safe_xfade; ++s) {
        const double t       = static_cast<double>(s) / safe_xfade;
        const double fade_in = 0.5*(1.0-std::cos(M_PI*t));
        const int64_t di     = offset + s;
        if (di >= dst_size) break;
        dst[di] = dst[di]*(1.0-fade_in) + src[s]*fade_in;
    }
    const int64_t body_end = std::min(offset+src_size, dst_size);
    for (int64_t s = offset+safe_xfade; s < body_end; ++s)
        dst[s] = src[s-offset];
}

// ============================================================
// apply_gender_shift
// ============================================================

static void apply_gender_shift(double* sr, int spec_bins, double gender, double* tmp)
{
    if (!sr || !tmp || spec_bins <= 0) return;
    if (std::abs(gender-0.5) < 1e-4) return;
    const double shift_ratio = std::exp((gender-0.5)*0.4*std::log(2.0));
    constexpr double kFloor = 1e-12;
    for (int k = 0; k < spec_bins; ++k) tmp[k] = std::log(std::max(sr[k], kFloor));
    for (int k = 0; k < spec_bins; ++k) {
        const double src_k = static_cast<double>(k) / shift_ratio;
        const int    k0    = static_cast<int>(src_k);
        if (k0 >= spec_bins-1) { sr[k] = std::exp(tmp[spec_bins-1]); }
        else {
            const double frac = src_k - k0;
            sr[k] = std::exp((1.0-frac)*tmp[k0] + frac*tmp[k0+1]);
        }
    }
}

// ============================================================
// apply_tension_breath
// ============================================================

static void apply_tension_breath(double* sr, double* ar, int spec_bins,
                                  double tension, double breath)
{
    if (!sr || !ar || spec_bins <= 1) return;
    const double inv = 1.0 / (spec_bins-1);
    for (int k = 0; k < spec_bins; ++k) {
        const double fw = static_cast<double>(k) * inv;
        if (std::abs(tension-0.5) > 1e-4) {
            const double weight     = 1.0/(1.0+std::exp(-8.0*(fw-0.35)));
            const double gain_db    = (tension-0.5)*12.0*weight;
            const double clipped_db = 6.0*std::tanh(gain_db/6.0);
            sr[k] *= std::pow(10.0, clipped_db/20.0);
        }
        if (std::abs(breath-0.5) > 1e-4) {
            const double bw     = std::pow(fw, 0.7);
            const double amount = (breath-0.5)*bw;
            ar[k] = amount >= 0.0
                ? ar[k] + amount*(1.0-ar[k])
                : ar[k] + amount*ar[k];
            ar[k] = std::clamp(ar[k], 0.0, 1.0);
        }
    }
}

// ============================================================
// blend_transition_spectra
// ============================================================

static void blend_transition_spectra(
    double** spec_cur, double** ap_cur, int cur_len,
    double** spec_prev, double** ap_prev, int prev_len,
    int spec_bins, int transition_frames)
{
    if (!spec_cur || !spec_prev || !ap_cur || !ap_prev) return;
    if (spec_bins <= 0 || cur_len <= 0 || prev_len <= 0) return;
    const int blend = std::min(transition_frames, std::min(cur_len, prev_len));
    for (int j = 0; j < blend; ++j) {
        const double t      = static_cast<double>(j) / blend;
        const double w_prev = 0.5*(1.0-std::cos(M_PI*(1.0-t)));
        const double w_cur  = 1.0 - w_prev;
        const int    prev_j = prev_len - blend + j;
        constexpr double kFloor = 1e-12;
        double* sc = spec_cur [j];
        double* sp = spec_prev[std::max(0, prev_j)];
        double* ac = ap_cur   [j];
        double* ap = ap_prev  [std::max(0, prev_j)];
        for (int k = 0; k < spec_bins; ++k) {
            sc[k] = std::exp(w_cur *std::log(std::max(sc[k],kFloor))
                           + w_prev*std::log(std::max(sp[k],kFloor)));
            ac[k] = std::clamp(w_cur*ac[k] + w_prev*ap[k], 0.0, 1.0);
        }
    }
}

// ============================================================
// apply_vibrato
//
// ノート後半50%からビブラートを自然に立ち上げる。
// フェードイン: raised cosine で 0→1
// 波形: sin（6Hz・±15cent）
// 15cent = 目標Hz × (2^(15/1200) - 1) ≈ 目標Hz × 0.00868
//
// AuralAIEngineの _apply_pseudo_ai と同じ発想だが、
// C++側でフレーム単位に適用することで遅延ゼロ・Python依存なし。
// ============================================================

static void apply_vibrato(double* f0, int f0_length, double frame_period_ms)
{
    if (!f0 || f0_length <= 0) return;

    // ビブラートが始まるフレーム（後半50%から）
    const int vib_start = f0_length / 2;
    const int vib_len   = f0_length - vib_start;
    if (vib_len <= 0) return;

    constexpr double kVibFreqHz  = 6.0;         // 6Hz
    constexpr double kVibDepth   = 0.00868;      // 約15cent
    const double     frame_sec   = frame_period_ms / 1000.0;

    for (int j = vib_start; j < f0_length; ++j) {
        // フェードイン: 後半の最初の25%で0→1に立ち上げる
        const double fade_progress =
            static_cast<double>(j - vib_start) / std::max(vib_len - 1, 1);
        const double fade_in = std::min(fade_progress * 4.0, 1.0); // 25%で飽和

        const double t_sec = static_cast<double>(j) * frame_sec;
        const double vib   = std::sin(2.0 * M_PI * kVibFreqHz * t_sec)
                             * kVibDepth * f0[j] * fade_in;
        f0[j] += vib;
        // F0が負にならないようにクランプ
        if (f0[j] < 50.0) f0[j] = 50.0;
    }
}

// ============================================================
// [NEW ③] smooth_f0_gaussian
//
// F0配列にガウシアンカーネルを畳み込んで音符境界の急変を緩和する。
// カーネル幅: 5フレーム（= 25ms @ 5ms/frame）
// 端点は折り返しパディングで処理する（ゼロパディングより自然）。
//
// 処理コスト: f0_length × 5 の乗算のみ → 無視できる
// ============================================================

static void smooth_f0_gaussian(double* f0, int f0_length)
{
    if (!f0 || f0_length <= 0) return;

    // sigma=1.0 の5点ガウシアンカーネル（正規化済み）
    static constexpr double kKernel[5] = {
        0.06136, 0.24477, 0.38774, 0.24477, 0.06136
    };
    static constexpr int kRadius = 2; // カーネル半径

    std::vector<double> tmp(f0_length);
    for (int i = 0; i < f0_length; ++i) {
        double sum = 0.0;
        for (int k = -kRadius; k <= kRadius; ++k) {
            // 折り返しパディング: 端点を反射させる
            int idx = i + k;
            if (idx < 0)           idx = -idx;
            if (idx >= f0_length)  idx = 2*(f0_length-1) - idx;
            sum += f0[idx] * kKernel[k + kRadius];
        }
        tmp[i] = sum;
    }
    std::copy(tmp.begin(), tmp.end(), f0);
}

// ============================================================
// VOSE_Synthesis
// ============================================================

static void VOSE_Synthesis(
    const double* f0, int f0_length,
    double** spectrogram, double** aperiodicity,
    int fft_size, double frame_period, int fs,
    int y_length, double* y)
{
    const int spec_bins = fft_size / 2 + 1;
    tl_scratch.ensure_spec(f0_length, spec_bins);
    double** mod_ap = tl_scratch.mod_ap_ptrs.data();

    static thread_local std::mt19937 rng(42);
    std::uniform_real_distribution<double> dist(-0.02, 0.02);

    for (int i = 0; i < f0_length; ++i) {
        double* ap_dst = mod_ap[i];
        double* ap_src = aperiodicity[i];
        double delta_f0 = 0.0;
        if (i > 0 && i < f0_length-1)
            delta_f0 = std::abs(f0[i+1]-f0[i-1])*0.5;
        const double vibrato_breath = std::min(0.15, delta_f0*0.003);
        for (int k = 0; k < spec_bins; ++k) {
            double current_ap = ap_src[k];
            const double freq = static_cast<double>(k)*fs/fft_size;
            if (freq > 2000.0) current_ap += vibrato_breath + dist(rng);
            ap_dst[k] = std::clamp(current_ap, 0.0, 1.0);
        }
    }

    Synthesis(f0, f0_length, spectrogram, mod_ap,
              fft_size, frame_period, fs, y_length, y);

    double prev_x = 0.0, prev_y_hp = 0.0;
    for (int i = 0; i < y_length; ++i) {
        double hp = y[i] - prev_x + 0.85*prev_y_hp;
        prev_x = y[i];
        prev_y_hp = hp;
        y[i] += hp*0.05;
    }
}

// ============================================================
// extern "C" API
// ============================================================

extern "C" {

void init_official_engine() { register_all_embedded_voices(); }

DLLEXPORT void load_embedded_resource(const char* phoneme,
                                      const int16_t* raw_data, int sample_count)
{
    if (!phoneme || !raw_data || sample_count <= 0) return;

    auto ev = std::make_shared<EmbeddedVoice>();
    ev->fs = kFs;
    ev->waveform.resize(sample_count);
    for (int i = 0; i < sample_count; ++i)
        ev->waveform[i] = static_cast<double>(raw_data[i]) * kInv32768;

    std::unique_lock<std::shared_mutex> clock(g_analysis_cache_mutex);
    std::unique_lock<std::shared_mutex> wlock(g_voice_db_mutex);
    auto old_it = g_voice_db.find(phoneme);
    if (old_it != g_voice_db.end())
        g_analysis_cache.erase(old_it->second);
    g_voice_db[phoneme] = std::move(ev);
}

// ============================================================
// execute_render  (並列合成版)
//
// 並列化の設計:
//   パス2を「合成フェーズ」と「書き込みフェーズ」に分離する。
//
//   [合成フェーズ・並列]
//     各ノートの note_buf を std::async で独立して合成する。
//     ノード間の依存関係（current_offset, full_song_buffer）には
//     一切触れないので安全に並列化できる。
//     tl_scratch は thread_local なのでスレッドごとに独立している。
//
//   [書き込みフェーズ・順次]
//     future.get() で合成完了を待ち、apply_crossfade でシングルスレッドで書き込む。
//     full_song_buffer への書き込みはここだけなのでデータ競合なし。
//
// スレッド数:
//   std::thread::hardware_concurrency() を上限とするが、
//   音源の解析（get_or_analyze）は g_analysis_cache_mutex を取るため
//   キャッシュミス時だけ直列化される。通常はキャッシュヒットするので問題なし。
// ============================================================
 

DLLEXPORT void execute_render(NoteEvent* notes, int note_count, const char* output_path, int mode_flag)
{
    if (!notes || note_count <= 0 || !output_path) return;

    // ================================================================
    // Pro版（Studio Master）の判定とパラメータ設定
    // ================================================================
    bool is_pro = (mode_flag == 1);
    
    // Pro版は 32bit float (または32bit PCM)、無料版は 16bit CD音質
    int out_bit_depth = is_pro ? 32 : 16;
    
    // ※将来的に96kHz出力を行う場合は、ここの out_fs を切り替えて、
    // 最後の wavwrite 前にリサンプリング処理を挟みます。
    int out_fs = kFs; 

    const int fft_size  = GetFFTSizeForCheapTrick(kFs, nullptr);
    const int spec_bins = fft_size / 2 + 1;

    // ----------------------------------------------------------------
    // パス1: NotePrepass 構築（変更なし）
    // ----------------------------------------------------------------
    std::vector<NotePrepass> prepass(note_count);
    int     max_harvest_len  = 0;
    int64_t total_samples    = 0;
    int     xfade_count      = 0;
    bool    prev_renderable  = false;
    double  max_preutterance = 0.0;
    std::shared_ptr<const EmbeddedVoice> last_ev;

    for (int i = 0; i < note_count; ++i) {
        const int pitch_len = notes[i].pitch_length;
        if (pitch_len <= 0 || pitch_len > kMaxPitchLength) {
            prepass[i]      = NotePrepass(NoteState::INVALID, 0, nullptr);
            prev_renderable = false;
            last_ev         = nullptr;
            continue;
        }

        const int64_t ns = note_samples_safe(pitch_len);
        auto ev = find_voice_ref(notes[i].wav_path);

        const OtoEntry* found_oto = nullptr;
        {
            std::shared_lock<std::shared_mutex> lock(g_oto_db_mutex);
            auto oto_it = g_oto_db.find(notes[i].wav_path);
            if (oto_it != g_oto_db.end()) {
                found_oto = &oto_it->second;
                max_preutterance = std::max(max_preutterance,
                                            found_oto->preutterance);
            }
        }

        if (ev) {
            prepass[i] = NotePrepass(NoteState::RENDERABLE, ns, ev,
                                     prev_renderable ? last_ev : nullptr,
                                     found_oto);
            if (prev_renderable) ++xfade_count;
            prev_renderable = true;
            last_ev         = ev;
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

    const int64_t pre_buffer_samples =
        static_cast<int64_t>(max_preutterance * kFs / 1000.0);
    const int64_t buffer_total = total_samples + pre_buffer_samples;

    tl_scratch.ensure_spec(max_harvest_len, spec_bins);
    std::vector<double> full_song_buffer(buffer_total, 0.0);

    static const OtoEntry kDefaultOto = {};

    // ----------------------------------------------------------------
    // パス2-A: 各ノートの note_buf を並列合成
    // ----------------------------------------------------------------
    const int max_threads = static_cast<int>(
        std::max(1u, std::thread::hardware_concurrency()));

    std::vector<std::vector<double>> note_bufs(note_count);

    auto synthesize_note = [&](int idx) {
        const NotePrepass& pp = prepass[idx];
        if (pp.state != NoteState::RENDERABLE) return;

        NoteEvent& n               = notes[idx];
        const int64_t note_samples = pp.note_samples;
        const double  note_ms      = static_cast<double>(note_samples) / kFs * 1000.0;
        const double  src_ms       = get_source_ms(*pp.ev);
        const int     output_frames = static_cast<int>(note_ms / kFramePeriod);
        const OtoEntry& current_oto = pp.oto ? *pp.oto : kDefaultOto;

        auto cache_cur = get_or_analyze(pp.ev, fft_size, spec_bins);

        tl_scratch.ensure_f0(output_frames);
        tl_scratch.ensure_spec(output_frames, spec_bins);

        if (pp.prev_ev) {
            auto cache_prev = get_or_analyze(pp.prev_ev, fft_size, spec_bins);
            copy_cache_to_scratch_prev(*cache_prev);
            blend_transition_spectra(
                tl_scratch.spec_ptrs.data(), tl_scratch.ap_ptrs.data(), output_frames,
                tl_scratch.spec_ptrs_prev.data(), tl_scratch.ap_ptrs_prev.data(),
                cache_prev->length, spec_bins, kTransitionFrames);
        }

        for (int j = 0; j < output_frames; ++j) {
            const double t_out_ms = j * kFramePeriod;
            const double t_src_ms = map_time(t_out_ms, current_oto, src_ms, note_ms);
            const int src_frame   = std::clamp(
                static_cast<int>(t_src_ms / kFramePeriod), 0, cache_cur->length-1);

            double* sr = tl_scratch.spec_ptrs[j];
            double* ar = tl_scratch.ap_ptrs  [j];
            std::copy_n(&cache_cur->flat_spec[static_cast<size_t>(src_frame)*spec_bins],
                        spec_bins, sr);
            std::copy_n(&cache_cur->flat_ap  [static_cast<size_t>(src_frame)*spec_bins],
                        spec_bins, ar);

            tl_scratch.f0[j] = n.pitch_curve
                ? resample_curve(n.pitch_curve, n.pitch_length, j, output_frames)
                : 440.0;
            const double gender  = n.gender_curve
                ? resample_curve(n.gender_curve,  n.pitch_length, j, output_frames) : 0.5;
            const double tension = n.tension_curve
                ? resample_curve(n.tension_curve, n.pitch_length, j, output_frames) : 0.5;
            const double breath  = n.breath_curve
                ? resample_curve(n.breath_curve,  n.pitch_length, j, output_frames) : 0.5;

            apply_gender_shift  (sr, spec_bins, gender, tl_scratch.spec_tmp.data());
            apply_tension_breath(sr, ar, spec_bins, tension, breath);
        }

        smooth_f0_gaussian(tl_scratch.f0.data(), output_frames);
        apply_vibrato(tl_scratch.f0.data(), output_frames, kFramePeriod);

        note_bufs[idx].assign(static_cast<size_t>(note_samples), 0.0);
        
        // --- 【Pro機能拡張ポイント】 ---
        // 将来的には、ここで is_pro フラグを使って、WORLD のより重いが高音質な
        // アルゴリズムに分岐させたり、kFramePeriod を短くして時間解像度を上げる
        // などの処理が可能です。
        VOSE_Synthesis(tl_scratch.f0.data(), output_frames,
                       tl_scratch.spec_ptrs.data(), tl_scratch.ap_ptrs.data(),
                       fft_size, kFramePeriod, pp.ev->fs,
                       static_cast<int>(note_samples), note_bufs[idx].data());
    };

    {
        std::vector<std::future<void>> futures;
        futures.reserve(max_threads);

        for (int i = 0; i < note_count; ++i) {
            if (prepass[i].state != NoteState::RENDERABLE) continue;

            futures.push_back(std::async(std::launch::async,
                                         synthesize_note, i));

            if (static_cast<int>(futures.size()) >= max_threads) {
                for (auto& f : futures) f.get();
                futures.clear();
            }
        }

        for (auto& f : futures) f.get();
    }

    // ----------------------------------------------------------------
    // パス2-B: 書き込みフェーズ
    // ----------------------------------------------------------------
    int64_t current_offset     = pre_buffer_samples;
    bool    last_note_rendered = false;

    for (int idx = 0; idx < note_count; ++idx) {
        const NotePrepass& pp = prepass[idx];

        switch (pp.state) {
        case NoteState::INVALID:
        case NoteState::NO_VOICE:
            last_note_rendered = false;
            if (pp.state == NoteState::NO_VOICE) current_offset += pp.note_samples;
            continue;
        case NoteState::RENDERABLE:
            break;
        }

        const int64_t note_samples = pp.note_samples;
        const OtoEntry& current_oto = pp.oto ? *pp.oto : kDefaultOto;

        const int64_t pre_samples  =
            static_cast<int64_t>(current_oto.preutterance * kFs / 1000.0);
        const int64_t base_offset  = last_note_rendered
                                     ? current_offset - kCrossfadeSamples
                                     : current_offset;
        const int64_t write_offset = std::max<int64_t>(0, base_offset - pre_samples);
        const int     xfade        = last_note_rendered ? kCrossfadeSamples : 0;

        apply_crossfade(full_song_buffer, buffer_total,
                        note_bufs[idx], note_samples, write_offset, xfade);

        current_offset += last_note_rendered
                          ? note_samples - kCrossfadeSamples
                          : note_samples;
        last_note_rendered = true;
    }

    // ----------------------------------------------------------------
    // 【有料化の要】Pro版は 32bit出力、Free版は 16bit出力
    // ----------------------------------------------------------------
    wavwrite(full_song_buffer.data() + pre_buffer_samples,
             static_cast<int>(total_samples),
             out_fs, out_bit_depth, output_path);
}
 
} // extern "C"
