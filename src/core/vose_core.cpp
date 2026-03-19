#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <random>
#include <sstream>
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
        auto last_time = static_cast<long long>(fs::last_write_time(p).time_since_epoch().count());
        auto file_size = static_cast<unsigned long long>(fs::file_size(p));
        std::string seed = p.string() + std::to_string(last_time) + std::to_string(file_size);
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
    for (int i = 0; i < count; ++i)
        g_oto_db[entries[i].alias] = entries[i];
}

// ============================================================
// データ構造
// ============================================================

struct EmbeddedVoice {
    std::string          path;
    std::vector<double>  waveform;
    int                  fs;
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
        : state(s), note_samples(ns), ev(std::move(e)), prev_ev(std::move(pe)), oto(o) {}
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
        if (n > static_cast<int>(f0.size())) { f0.resize(n); time_axis.resize(n); }
    }
    void ensure_f0_prev(int n) {
        if (n > static_cast<int>(f0_prev.size())) { f0_prev.resize(n); time_axis_prev.resize(n); }
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
// キャッシュディレクトリ
// ============================================================

static fs::path get_cache_dir() {
    fs::path p = "cache";
    if (!fs::exists(p)) fs::create_directories(p);
    return p;
}

// ============================================================
// ディスクキャッシュ読み書き
// [FIX-SAVE] 前方宣言を削除し、実装を使用箇所より前に配置。
//            シグネチャを (const fs::path&, const AnalysisCache&) に統一。
// ============================================================

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
    const size_t spec_count = static_cast<size_t>(cache.length) * cache.spec_bins;
    fwrite(cache.flat_spec.data(), sizeof(double), spec_count, fp);
    fwrite(cache.flat_ap.data(),   sizeof(double), spec_count, fp);
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
    const size_t spec_count = static_cast<size_t>(cache->length) * cache->spec_bins;
    cache->flat_spec.resize(spec_count);
    cache->flat_ap  .resize(spec_count);

    ifs.read(reinterpret_cast<char*>(cache->f0.data()),        sizeof(double) * cache->length);
    ifs.read(reinterpret_cast<char*>(cache->time.data()),      sizeof(double) * cache->length);
    ifs.read(reinterpret_cast<char*>(cache->flat_spec.data()), sizeof(double) * spec_count);
    ifs.read(reinterpret_cast<char*>(cache->flat_ap.data()),   sizeof(double) * spec_count);
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
            for (int i = 0; i < vi.front(); ++i) cache->f0[i] = cache->f0[vi.front()];
            for (int i = vi.back()+1; i < harvest_len; ++i) cache->f0[i] = cache->f0[vi.back()];
            for (int v = 0; v+1 < static_cast<int>(vi.size()); ++v) {
                const int ia = vi[v], ib = vi[v+1];
                if (ib-ia <= 1) continue;
                const double fa = cache->f0[ia], fb = cache->f0[ib];
                for (int i = ia+1; i < ib; ++i) {
                    cache->f0[i] = fa + static_cast<double>(i-ia)/(ib-ia) * (fb-fa);
                }
            }
        } else {
            std::fill(cache->f0.begin(), cache->f0.end(), 440.0);
        }
    }

    const size_t spec_count = static_cast<size_t>(harvest_len) * spec_bins;
    cache->flat_spec.resize(spec_count);
    cache->flat_ap  .resize(spec_count);

    std::vector<double*> sp(harvest_len), ap(harvest_len);
    for (int i = 0; i < harvest_len; ++i) {
        sp[i] = &cache->flat_spec[static_cast<size_t>(i) * spec_bins];
        ap[i] = &cache->flat_ap  [static_cast<size_t>(i) * spec_bins];
    }

    CheapTrick(ev.waveform.data(), wav_len, ev.fs,
               cache->time.data(), cache->f0.data(), harvest_len, nullptr, sp.data());
    D4C(ev.waveform.data(), wav_len, ev.fs,
        cache->time.data(), cache->f0.data(), harvest_len, fft_size, nullptr, ap.data());

    return cache;
}

// ============================================================
// get_or_analyze  (double-checked locking + ディスクキャッシュ)
// ============================================================

static std::shared_ptr<const AnalysisCache>
get_or_analyze(std::shared_ptr<const EmbeddedVoice> ev_sp, int fft_size, int spec_bins)
{
    // 1. メモリキャッシュ確認（shared_lock）
    {
        std::shared_lock<std::shared_mutex> rlock(g_analysis_cache_mutex);
        auto it = g_analysis_cache.find(ev_sp);
        if (it != g_analysis_cache.end()) return it->second;
    }

    // 2. ディスクキャッシュ確認（ロック外）
    const std::string h_str    = generate_cache_hash(ev_sp->path);
    const fs::path    cache_file = get_cache_dir() / (h_str + ".vsc");
    auto disk_cache = load_cache(cache_file);

    // 3. 排他ロックで確定
    std::unique_lock<std::shared_mutex> wlock(g_analysis_cache_mutex);
    {
        auto it = g_analysis_cache.find(ev_sp);
        if (it != g_analysis_cache.end()) return it->second;
    }

    if (disk_cache) {
        g_analysis_cache[ev_sp] = disk_cache;
        return disk_cache;
    }

    // 4. 解析実行 → メモリ登録 → ロック解放 → ディスク保存
    auto cache = build_analysis_cache(*ev_sp, fft_size, spec_bins);
    g_analysis_cache[ev_sp] = cache;
    wlock.unlock();                      // [FIX-IO] ロック外でファイルI/O
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
                              ? source_wav_len_ms + oto.cutoff
                              : oto.cutoff;
    const double source_stretch = cutoff_pos - (offset + fixed);
    const double output_stretch = note_duration_ms - fixed;

    if (t_out_ms < fixed)
        return t_out_ms + offset;

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
    const double frac  = src_f - j0;
    return (1.0-frac)*curve[j0] + frac*curve[j1];
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
        std::min<int64_t>(xfade_len, std::min(src_size, dst_size-offset)));
    for (int s = 0; s < safe_xfade; ++s) {
        const double t       = static_cast<double>(s) / safe_xfade;
        const double fade_in = 0.5 * (1.0 - std::cos(M_PI*t));
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
    const int blend_frames = std::min(transition_frames, std::min(cur_len, prev_len));
    for (int j = 0; j < blend_frames; ++j) {
        const double t      = static_cast<double>(j) / blend_frames;
        const double w_prev = 0.5*(1.0-std::cos(M_PI*(1.0-t)));
        const double w_cur  = 1.0 - w_prev;
        const int    prev_j = prev_len - blend_frames + j;
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

    Synthesis(f0, f0_length, spectrogram, mod_ap, fft_size, frame_period, fs, y_length, y);

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

    // ロック順序: analysis_cache → voice_db（get_or_analyze と同じ順序）
    std::unique_lock<std::shared_mutex> clock(g_analysis_cache_mutex);
    std::unique_lock<std::shared_mutex> wlock(g_voice_db_mutex);

    auto old_it = g_voice_db.find(phoneme);
    if (old_it != g_voice_db.end())
        g_analysis_cache.erase(old_it->second);

    g_voice_db[phoneme] = std::move(ev);
}

DLLEXPORT void execute_render(NoteEvent* notes, int note_count, const char* output_path)
{
    if (!notes || note_count <= 0 || !output_path) return;

    const int fft_size  = GetFFTSizeForCheapTrick(kFs, nullptr);
    const int spec_bins = fft_size / 2 + 1;

    // ----------------------------------------------------------------
    // パス1: NotePrepass 構築
    // [FIX-BRACE] forループを正しく閉じる
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
            prepass[i]      = NotePrepass(NoteState::INVALID, 0, nullptr);
            prev_renderable = false;
            last_ev         = nullptr;
            continue;
        }

        const int64_t ns = note_samples_safe(pitch_len);
        auto ev = find_voice_ref(notes[i].wav_path);

        // oto検索（パス1で1回だけ実行）
        const OtoEntry* found_oto = nullptr;
        {
            std::shared_lock<std::shared_mutex> lock(g_oto_db_mutex);
            auto oto_it = g_oto_db.find(notes[i].wav_path);
            if (oto_it != g_oto_db.end())
                found_oto = &oto_it->second;
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
    } // [FIX-BRACE] ← パス1forループの正しい閉じ括弧

    total_samples -= static_cast<int64_t>(kCrossfadeSamples) * xfade_count;
    if (total_samples <= 0) return;

    tl_scratch.ensure_spec(max_harvest_len, spec_bins);
    std::vector<double> full_song_buffer(total_samples, 0.0);
    std::vector<double> note_buf;

    int64_t current_offset     = 0;
    bool    last_note_rendered = false;

    // ----------------------------------------------------------------
    // パス2: 合成
    // ----------------------------------------------------------------

    static const OtoEntry kDefaultOto = {};

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
        }

        for (int j = 0; j < output_frames; ++j) {
            const double t_out_ms = j * kFramePeriod;
            const double t_src_ms = map_time(t_out_ms, current_oto, src_ms, note_ms);
            const int src_frame   = std::clamp(
                static_cast<int>(t_src_ms / kFramePeriod), 0, cache_cur->length-1);

            double* sr = tl_scratch.spec_ptrs[j];
            double* ar = tl_scratch.ap_ptrs  [j];
            std::copy_n(&cache_cur->flat_spec[static_cast<size_t>(src_frame)*spec_bins], spec_bins, sr);
            std::copy_n(&cache_cur->flat_ap  [static_cast<size_t>(src_frame)*spec_bins], spec_bins, ar);

            tl_scratch.f0[j] = n.pitch_curve
                ? resample_curve(n.pitch_curve, n.pitch_length, j, output_frames) : 440.0;
            const double gender  = n.gender_curve
                ? resample_curve(n.gender_curve,  n.pitch_length, j, output_frames) : 0.5;
            const double tension = n.tension_curve
                ? resample_curve(n.tension_curve, n.pitch_length, j, output_frames) : 0.5;
            const double breath  = n.breath_curve
                ? resample_curve(n.breath_curve,  n.pitch_length, j, output_frames) : 0.5;

            apply_gender_shift  (sr, spec_bins, gender, tl_scratch.spec_tmp.data());
            apply_tension_breath(sr, ar, spec_bins, tension, breath);
        }

        note_buf.assign(static_cast<size_t>(note_samples), 0.0);
        VOSE_Synthesis(tl_scratch.f0.data(), output_frames,
                       tl_scratch.spec_ptrs.data(), tl_scratch.ap_ptrs.data(),
                       fft_size, kFramePeriod, pp.ev->fs,
                       static_cast<int>(note_samples), note_buf.data());

        const int64_t pre_samples  = static_cast<int64_t>(current_oto.preutterance * kFs / 1000.0);
        const int64_t base_offset  = last_note_rendered
                                     ? current_offset - kCrossfadeSamples
                                     : current_offset;
        const int64_t write_offset = std::max<int64_t>(0, base_offset - pre_samples);
        const int     xfade        = last_note_rendered ? kCrossfadeSamples : 0;

        apply_crossfade(full_song_buffer, total_samples,
                        note_buf, note_samples, write_offset, xfade);

        current_offset += last_note_rendered
                          ? note_samples - kCrossfadeSamples
                          : note_samples;
        last_note_rendered = true;
    }

    wavwrite(full_song_buffer.data(),
             static_cast<int>(full_song_buffer.size()),
             kFs, 16, output_path);
}

} // extern "C"
