// ╔══════════════════════════════════════════════════════════════════╗
// ║  VOSE Video Engine — video_engine.cpp   Phase 2 完全実装版       ║
// ║                                                                  ║
// ║  Phase 1: Core Integration (frame/waveform extraction)           ║
// ║  Phase 2: Logic Design    (keyframe index, EDL export)           ║
// ║  Phase 4: VO-SE Subtitles (ASS burn-in via avfilter — 完全実装)  ║
// ║  Phase 5: HW Optimization (Apple VideoToolbox encoder)           ║
// ║                                                                  ║
// ║  Phase 2 Fix:                                                    ║
// ║    · vose_waveform シグネチャを Python ラッパーと完全一致させる    ║
// ║    · exportWithSubtitles を avfilter で完全実装                   ║
// ║    · EDL serialize / getEnabledEntries 完全実装 (Phase 1 Fix 済) ║
// ╚══════════════════════════════════════════════════════════════════╝
#include "video_engine.hpp"

#include <iostream>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <cassert>
#include <iomanip>
#include <cctype>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libavutil/error.h>
#include <libswscale/swscale.h>
#include <libswresample/swresample.h>
#include <libavutil/channel_layout.h>
#include <libavutil/mathematics.h>
// avfilter は字幕バーンインに必要
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
}

namespace vose {

// ════════════════════════════════════════════════════════════════════
//  ミニ JSON パーサ (EDL 専用)
//  スキーマ: [{"in": 0.0, "out": 5.0, "enabled": true}, ...]
// ════════════════════════════════════════════════════════════════════

namespace {

class MiniJson {
public:
    explicit MiniJson(const std::string& src) : src_(src), pos_(0) {}

    std::vector<EDLEntry> parseEdlArray() {
        std::vector<EDLEntry> result;
        skipWs();
        if (pos_ >= src_.size() || src_[pos_] != '[') return result;
        ++pos_;
        while (true) {
            skipWs();
            if (pos_ >= src_.size()) break;
            if (src_[pos_] == ']') { ++pos_; break; }
            if (src_[pos_] == ',') { ++pos_; continue; }
            EDLEntry entry;
            entry.enabled = true;   // デフォルト true
            if (!parseObject(entry)) break;
            result.push_back(entry);
        }
        return result;
    }

private:
    const std::string& src_;
    size_t pos_;

    void skipWs() {
        while (pos_ < src_.size() &&
               (src_[pos_] == ' '  || src_[pos_] == '\t' ||
                src_[pos_] == '\n' || src_[pos_] == '\r'))
            ++pos_;
    }

    bool parseObject(EDLEntry& entry) {
        skipWs();
        if (pos_ >= src_.size() || src_[pos_] != '{') return false;
        ++pos_;
        while (true) {
            skipWs();
            if (pos_ >= src_.size()) return false;
            if (src_[pos_] == '}') { ++pos_; return true; }
            if (src_[pos_] == ',') { ++pos_; continue; }
            std::string key = parseString();
            skipWs();
            if (pos_ >= src_.size() || src_[pos_] != ':') return false;
            ++pos_;
            skipWs();
            if      (key == "in")      entry.in_point  = parseNumber();
            else if (key == "out")     entry.out_point = parseNumber();
            else if (key == "enabled") entry.enabled   = parseBool();
            else                       skipValue();
        }
    }

    std::string parseString() {
        skipWs();
        if (pos_ >= src_.size() || src_[pos_] != '"') return "";
        ++pos_;
        size_t start = pos_;
        while (pos_ < src_.size() && src_[pos_] != '"') {
            if (src_[pos_] == '\\') ++pos_;
            ++pos_;
        }
        std::string r = src_.substr(start, pos_ - start);
        if (pos_ < src_.size()) ++pos_;
        return r;
    }

    double parseNumber() {
        skipWs();
        size_t start = pos_;
        if (pos_ < src_.size() && src_[pos_] == '-') ++pos_;
        while (pos_ < src_.size() &&
               (std::isdigit((unsigned char)src_[pos_]) ||
                src_[pos_] == '.' || src_[pos_] == 'e' ||
                src_[pos_] == 'E' || src_[pos_] == '+' || src_[pos_] == '-'))
            ++pos_;
        if (pos_ == start) return 0.0;
        try { return std::stod(src_.substr(start, pos_ - start)); }
        catch (...) { return 0.0; }
    }

    bool parseBool() {
        skipWs();
        if (src_.compare(pos_, 4, "true")  == 0) { pos_ += 4; return true;  }
        if (src_.compare(pos_, 5, "false") == 0) { pos_ += 5; return false; }
        return true;
    }

    void skipValue() {
        skipWs();
        if (pos_ >= src_.size()) return;
        char c = src_[pos_];
        if (c == '"')  { parseString(); return; }
        if (c == 't' || c == 'f' || c == 'n') {
            while (pos_ < src_.size() &&
                   std::isalpha((unsigned char)src_[pos_])) ++pos_;
            return;
        }
        if (c == '-' || std::isdigit((unsigned char)c)) {
            parseNumber(); return;
        }
        auto skipNested = [&](char open, char close) {
            ++pos_; int depth = 1;
            while (pos_ < src_.size() && depth > 0) {
                char ch = src_[pos_];
                if      (ch == open)  { ++depth; ++pos_; }
                else if (ch == close) { --depth; ++pos_; }
                else if (ch == '"')   { parseString(); }
                else                  { ++pos_; }
            }
        };
        if (c == '{') { skipNested('{', '}'); return; }
        if (c == '[') { skipNested('[', ']'); return; }
        ++pos_;
    }
};

} // anonymous namespace

// ════════════════════════════════════════════════════════════════════
//  EDL 実装
// ════════════════════════════════════════════════════════════════════

bool EDL::deserialize(const std::string& json_str) {
    if (json_str.empty()) {
        std::cerr << "[VOSE][EDL] JSON が空です\n";
        return false;
    }
    MiniJson parser(json_str);
    auto parsed = parser.parseEdlArray();
    if (parsed.empty()) {
        std::cerr << "[VOSE][EDL] パース結果が空\n";
        return false;
    }
    entries.clear();
    int valid_count = 0;
    for (auto& e : parsed) {
        if (e.out_point > e.in_point) {
            entries.push_back(e);
            if (e.enabled) ++valid_count;
        } else {
            std::cerr << "[VOSE][EDL] 無効エントリをスキップ:"
                      << " in=" << e.in_point
                      << " out=" << e.out_point << "\n";
        }
    }
    std::cerr << "[VOSE][EDL] パース完了: 有効=" << valid_count
              << " / 合計=" << entries.size() << " エントリ\n";
    return valid_count > 0;
}


std::vector<EDLEntry> EDL::getEnabledEntries() const {
    std::vector<EDLEntry> result;
    result.reserve(entries.size()); // entries_ から アンダースコアを削除
    for (const auto& e : entries)   // entries_ から アンダースコアを削除
    {
        if (e.enabled && e.out_point > e.in_point) {
            result.push_back(e);
        }
    }
    return result;
}
// ════════════════════════════════════════════════════════════════════
//  ユーティリティ
// ════════════════════════════════════════════════════════════════════

static std::string avErr(int errnum) {
    char buf[256];
    av_strerror(errnum, buf, sizeof(buf));
    return std::string(buf);
}

#define VOSE_LOG(x)  std::cerr << "[VOSE] " << x << "\n"
#define VOSE_ERR(x)  std::cerr << "[VOSE][ERR] " << x << "\n"

// ════════════════════════════════════════════════════════════════════
//  コンストラクタ / デストラクタ
// ════════════════════════════════════════════════════════════════════

VideoEngine::VideoEngine()  { VOSE_LOG("Engine created."); }
VideoEngine::~VideoEngine() { releaseResources(); VOSE_LOG("Engine destroyed."); }

void VideoEngine::releaseResources() {
    if (swsCtx_)   { sws_freeContext(swsCtx_);     swsCtx_   = nullptr; }
    if (videoCtx_) { avcodec_free_context(&videoCtx_); }
    if (audioCtx_) { avcodec_free_context(&audioCtx_); }
    if (fmtCtx_)   { avformat_close_input(&fmtCtx_); }
    videoIdx_ = -1;
    audioIdx_ = -1;
    loaded_   = false;
}

bool VideoEngine::hasAudio() const { return audioIdx_ >= 0; }

// ════════════════════════════════════════════════════════════════════
//  Phase 1: load()
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::load(const std::string& filepath) {
    releaseResources();
    filePath_ = filepath;
    int ret;

    ret = avformat_open_input(&fmtCtx_, filepath.c_str(), nullptr, nullptr);
    if (ret != 0) { VOSE_ERR("avformat_open_input: " << avErr(ret)); return false; }

    ret = avformat_find_stream_info(fmtCtx_, nullptr);
    if (ret < 0)  { VOSE_ERR("avformat_find_stream_info: " << avErr(ret)); return false; }

    const AVCodec* videoDecoder = nullptr;
    videoIdx_ = av_find_best_stream(fmtCtx_, AVMEDIA_TYPE_VIDEO,
                                    -1, -1, &videoDecoder, 0);
    if (videoIdx_ < 0) {
        // 音声のみファイルにも対応
        VOSE_LOG("映像ストリームなし — 音声専用ファイルとして処理");
    } else {
        videoCtx_ = avcodec_alloc_context3(videoDecoder);
        if (!videoCtx_) { VOSE_ERR("avcodec_alloc_context3 (video) 失敗"); return false; }
        avcodec_parameters_to_context(videoCtx_, fmtCtx_->streams[videoIdx_]->codecpar);
        ret = avcodec_open2(videoCtx_, videoDecoder, nullptr);
        if (ret < 0) { VOSE_ERR("avcodec_open2 (video): " << avErr(ret)); return false; }
    }

    const AVCodec* audioDecoder = nullptr;
    audioIdx_ = av_find_best_stream(fmtCtx_, AVMEDIA_TYPE_AUDIO,
                                    -1, -1, &audioDecoder, 0);
    if (audioIdx_ >= 0 && audioDecoder) {
        audioCtx_ = avcodec_alloc_context3(audioDecoder);
        if (audioCtx_) {
            avcodec_parameters_to_context(audioCtx_,
                                          fmtCtx_->streams[audioIdx_]->codecpar);
            if (avcodec_open2(audioCtx_, audioDecoder, nullptr) < 0) {
                VOSE_LOG("音声コーデックを開けません。音声なしで続行。");
                avcodec_free_context(&audioCtx_);
                audioIdx_ = -1;
            }
        }
    }

    loaded_ = true;
    VOSE_LOG("ロード完了: " << filepath
             << " | " << width() << "x" << height()
             << " @ " << std::fixed << std::setprecision(2) << fps() << "fps"
             << " | " << std::setprecision(1) << duration() << "s"
             << " | codec=" << codecName()
             << " | audio=" << (hasAudio() ? "あり" : "なし"));
    return true;
}

// ── アクセサ ──────────────────────────────────────────────────────

int    VideoEngine::width()  const { return (loaded_ && videoCtx_) ? videoCtx_->width  : 0; }
int    VideoEngine::height() const { return (loaded_ && videoCtx_) ? videoCtx_->height : 0; }
double VideoEngine::fps()    const {
    if (!loaded_ || videoIdx_ < 0) return 0.0;
    AVRational r = fmtCtx_->streams[videoIdx_]->avg_frame_rate;
    return r.den ? av_q2d(r) : 0.0;
}
double VideoEngine::duration() const {
    if (!loaded_) return 0.0;
    if (fmtCtx_->duration != AV_NOPTS_VALUE)
        return static_cast<double>(fmtCtx_->duration) / AV_TIME_BASE;
    return 0.0;
}
std::string VideoEngine::codecName() const {
    return (loaded_ && videoCtx_) ? avcodec_get_name(videoCtx_->codec_id) : "none";
}
double VideoEngine::toSeconds(int64_t pts, AVRational tb) const {
    return static_cast<double>(pts) * av_q2d(tb);
}
void VideoEngine::reportProgress(double p, const std::string& stage) {
    if (progressCb_) progressCb_(std::min(p, 1.0), stage);
}
bool VideoEngine::seekAndFlush(double timeSec) {
    if (!loaded_) return false;
    int ret = -1;
    if (videoIdx_ >= 0) {
        AVStream* vs = fmtCtx_->streams[videoIdx_];
        int64_t ts   = static_cast<int64_t>(timeSec / av_q2d(vs->time_base));
        ret = av_seek_frame(fmtCtx_, videoIdx_, ts, AVSEEK_FLAG_BACKWARD);
    }
    if (ret < 0) {
        int64_t ts_us = static_cast<int64_t>(timeSec * AV_TIME_BASE);
        ret = avformat_seek_file(fmtCtx_, -1, INT64_MIN, ts_us, INT64_MAX, 0);
    }
    if (videoCtx_) avcodec_flush_buffers(videoCtx_);
    if (audioCtx_) avcodec_flush_buffers(audioCtx_);
    return ret >= 0;
}
SwsContext* VideoEngine::makeSwsCtx(int w, int h, int srcFmt) {
    if (swsCtx_) { sws_freeContext(swsCtx_); swsCtx_ = nullptr; }
    swsCtx_ = sws_getContext(w, h, static_cast<AVPixelFormat>(srcFmt),
                             w, h, AV_PIX_FMT_RGB24,
                             SWS_BILINEAR, nullptr, nullptr, nullptr);
    return swsCtx_;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 1: extractFrame()
// ════════════════════════════════════════════════════════════════════

std::optional<FrameInfo> VideoEngine::extractFrame(double timeSec) {
    if (!loaded_ || videoIdx_ < 0) return std::nullopt;
    seekAndFlush(timeSec);

    AVPacket* pkt   = av_packet_alloc();
    AVFrame*  frame = av_frame_alloc();
    std::optional<FrameInfo> result;

    while (av_read_frame(fmtCtx_, pkt) >= 0) {
        if (pkt->stream_index != videoIdx_) { av_packet_unref(pkt); continue; }
        if (avcodec_send_packet(videoCtx_, pkt) == 0) {
            if (avcodec_receive_frame(videoCtx_, frame) == 0) {
                int w = frame->width, h = frame->height;
                AVFrame* rgb = av_frame_alloc();
                rgb->format  = AV_PIX_FMT_RGB24;
                rgb->width   = w;
                rgb->height  = h;
                av_frame_get_buffer(rgb, 1);
                SwsContext* sws = makeSwsCtx(w, h, (AVPixelFormat)frame->format);
                sws_scale(sws, frame->data, frame->linesize, 0, h,
                          rgb->data, rgb->linesize);
                FrameInfo fi;
                fi.width       = w;
                fi.height      = h;
                fi.is_keyframe = (frame->flags & AV_FRAME_FLAG_KEY) != 0;
                fi.pts_seconds = toSeconds(frame->pts,
                                           fmtCtx_->streams[videoIdx_]->time_base);
                fi.rgb_data.resize(static_cast<size_t>(w * h * 3));
                for (int y = 0; y < h; y++) {
                    std::memcpy(fi.rgb_data.data() + static_cast<size_t>(y * w * 3),
                                rgb->data[0] + y * rgb->linesize[0],
                                static_cast<size_t>(w * 3));
                }
                av_frame_free(&rgb);
                av_packet_unref(pkt);
                result = std::move(fi);
                break;
            }
        }
        av_packet_unref(pkt);
    }
    av_frame_free(&frame);
    av_packet_free(&pkt);
    return result;
}

bool VideoEngine::saveFrame(double timeSec, const std::string& outPath) {
    auto fi = extractFrame(timeSec);
    if (!fi) { VOSE_ERR("フレーム取得失敗 at " << timeSec << "s"); return false; }
    std::ofstream ofs(outPath, std::ios::binary);
    if (!ofs) { VOSE_ERR("出力ファイルを開けません: " << outPath); return false; }
    ofs << "P6\n" << fi->width << " " << fi->height << "\n255\n";
    ofs.write(reinterpret_cast<const char*>(fi->rgb_data.data()),
              static_cast<std::streamsize>(fi->rgb_data.size()));
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 1: extractWaveform()
// ════════════════════════════════════════════════════════════════════

WaveformData VideoEngine::extractWaveform(int chunks) {
    WaveformData result;
    if (!loaded_ || audioIdx_ < 0) return result;

    SwrContext* swr = swr_alloc();
    AVChannelLayout in_layout;
    av_channel_layout_copy(&in_layout, &audioCtx_->ch_layout);
    AVChannelLayout out_layout = AV_CHANNEL_LAYOUT_MONO;

    av_opt_set_chlayout   (swr, "in_chlayout",    &in_layout,             0);
    av_opt_set_chlayout   (swr, "out_chlayout",   &out_layout,            0);
    av_opt_set_int        (swr, "in_sample_rate",  audioCtx_->sample_rate, 0);
    av_opt_set_int        (swr, "out_sample_rate", audioCtx_->sample_rate, 0);
    av_opt_set_sample_fmt (swr, "in_sample_fmt",   audioCtx_->sample_fmt,  0);
    av_opt_set_sample_fmt (swr, "out_sample_fmt",  AV_SAMPLE_FMT_FLT,      0);

    if (swr_init(swr) < 0) {
        VOSE_ERR("swr_init 失敗");
        swr_free(&swr);
        av_channel_layout_uninit(&in_layout);
        return result;
    }
    av_channel_layout_uninit(&in_layout);

    avformat_seek_file(fmtCtx_, audioIdx_, 0, 0, 0, 0);
    avcodec_flush_buffers(audioCtx_);

    double dur = duration();
    std::vector<float> allSamples;
    allSamples.reserve(static_cast<size_t>(
        audioCtx_->sample_rate * std::max(dur, 1.0)) + 1024);

    AVPacket* pkt   = av_packet_alloc();
    AVFrame*  frame = av_frame_alloc();

    while (av_read_frame(fmtCtx_, pkt) >= 0) {
        if (pkt->stream_index == audioIdx_) {
            if (avcodec_send_packet(audioCtx_, pkt) == 0) {
                while (avcodec_receive_frame(audioCtx_, frame) == 0) {
                    int n = frame->nb_samples;
                    std::vector<float> converted(static_cast<size_t>(n));
                    uint8_t* outPtr = reinterpret_cast<uint8_t*>(converted.data());
                    swr_convert(swr, &outPtr, n,
                                const_cast<const uint8_t**>(frame->extended_data), n);
                    allSamples.insert(allSamples.end(),
                                      converted.begin(), converted.end());
                    av_frame_unref(frame);
                }
            }
        }
        av_packet_unref(pkt);
    }
    av_frame_free(&frame);
    av_packet_free(&pkt);
    swr_free(&swr);

    if (allSamples.empty()) return result;

    result.sample_rate  = audioCtx_->sample_rate;
    result.channels     = 1;
    result.duration_sec = dur;
    result.chunks       = chunks;
    result.peaks_max.assign(chunks, 0.0f);
    result.peaks_min.assign(chunks, 0.0f);
    result.rms.assign(chunks, 0.0f);

    size_t total     = allSamples.size();
    size_t chunkSize = std::max<size_t>(1, total / static_cast<size_t>(chunks));

    for (int c = 0; c < chunks; c++) {
        size_t start = static_cast<size_t>(c) * chunkSize;
        size_t end   = std::min(start + chunkSize, total);
        if (start >= total) break;
        float maxV = 0.0f, minV = 0.0f, sumSq = 0.0f;
        for (size_t i = start; i < end; i++) {
            float s = allSamples[i];
            if (s >  maxV) maxV  = s;
            if (s <  minV) minV  = s;
            sumSq += s * s;
        }
        result.peaks_max[c] = maxV;
        result.peaks_min[c] = minV;
        result.rms[c]       = std::sqrt(sumSq / static_cast<float>(end - start));
    }

    VOSE_LOG("波形生成: " << chunks << " chunks / " << allSamples.size() << " samples");
    return result;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 2: buildKeyframeIndex()
// ════════════════════════════════════════════════════════════════════

std::vector<KeyframeIndex> VideoEngine::buildKeyframeIndex() {
    if (!loaded_ || videoIdx_ < 0) return {};
    keyframeIdx_.clear();

    avformat_seek_file(fmtCtx_, videoIdx_, 0, 0, 0, AVSEEK_FLAG_BACKWARD);
    avcodec_flush_buffers(videoCtx_);

    AVStream* vs  = fmtCtx_->streams[videoIdx_];
    AVPacket* pkt = av_packet_alloc();
    double    dur = duration();

    while (av_read_frame(fmtCtx_, pkt) >= 0) {
        if (pkt->stream_index == videoIdx_ && (pkt->flags & AV_PKT_FLAG_KEY)) {
            KeyframeIndex kf;
            kf.pts_raw     = pkt->pts;
            kf.dts_raw     = pkt->dts;
            kf.pts_seconds = toSeconds(pkt->pts, vs->time_base);
            kf.file_pos    = avio_tell(fmtCtx_->pb);
            keyframeIdx_.push_back(kf);
            if (dur > 0.0) reportProgress(kf.pts_seconds / dur, "キーフレームインデックス");
        }
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);
    VOSE_LOG("キーフレーム数: " << keyframeIdx_.size());
    reportProgress(1.0, "インデックス完了");
    return keyframeIdx_;
}

double VideoEngine::findNearestKeyframe(double timeSec) const {
    if (keyframeIdx_.empty()) return timeSec;
    auto it = std::upper_bound(
        keyframeIdx_.begin(), keyframeIdx_.end(), timeSec,
        [](double t, const KeyframeIndex& kf) { return t < kf.pts_seconds; });
    if (it == keyframeIdx_.begin()) return keyframeIdx_.front().pts_seconds;
    --it;
    return it->pts_seconds;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 2: exportFromEDL()  (ストリームコピー版)
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::exportFromEDL(const EDL& edl, const std::string& outPath) {
    if (!loaded_) return false;
    auto entries = edl.getEnabledEntries();
    if (entries.empty()) { VOSE_LOG("EDLに有効エントリなし"); return false; }

    AVFormatContext* outFmt = nullptr;
    int ret = avformat_alloc_output_context2(&outFmt, nullptr, nullptr, outPath.c_str());
    if (ret < 0 || !outFmt) {
        VOSE_ERR("出力コンテキスト生成失敗: " << avErr(ret));
        return false;
    }

    std::vector<int> streamMap(fmtCtx_->nb_streams, -1);
    int outIdx = 0;
    for (unsigned i = 0; i < fmtCtx_->nb_streams; i++) {
        AVStream* in = fmtCtx_->streams[i];
        if (in->codecpar->codec_type != AVMEDIA_TYPE_VIDEO &&
            in->codecpar->codec_type != AVMEDIA_TYPE_AUDIO) continue;
        AVStream* out = avformat_new_stream(outFmt, nullptr);
        if (!out) continue;
        avcodec_parameters_copy(out->codecpar, in->codecpar);
        out->codecpar->codec_tag = 0;
        out->time_base = in->time_base;
        streamMap[i] = outIdx++;
    }

    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) {
        ret = avio_open(&outFmt->pb, outPath.c_str(), AVIO_FLAG_WRITE);
        if (ret < 0) {
            VOSE_ERR("avio_open 失敗: " << avErr(ret));
            avformat_free_context(outFmt);
            return false;
        }
    }
    avformat_write_header(outFmt, nullptr);

    std::vector<int64_t> ptsOffsets(outIdx, 0);
    std::vector<int64_t> firstPts(outIdx, AV_NOPTS_VALUE);
    std::vector<bool>    firstPktSeen(outIdx, false);

    for (size_t ei = 0; ei < entries.size(); ei++) {
        const auto& entry = entries[ei];
        reportProgress(static_cast<double>(ei) / entries.size(), "エクスポート中");

        double seekTarget = !keyframeIdx_.empty()
                          ? findNearestKeyframe(entry.in_point)
                          : entry.in_point;
        seekAndFlush(seekTarget);
        std::fill(firstPktSeen.begin(), firstPktSeen.end(), false);
        std::fill(firstPts.begin(),     firstPts.end(),     AV_NOPTS_VALUE);

        AVPacket* pkt = av_packet_alloc();
        while (av_read_frame(fmtCtx_, pkt) >= 0) {
            int si = pkt->stream_index;
            if (si >= (int)streamMap.size() || streamMap[si] < 0) {
                av_packet_unref(pkt); continue;
            }
            AVStream* inStream  = fmtCtx_->streams[si];
            int       oi        = streamMap[si];
            AVStream* outStream = outFmt->streams[oi];

            double pktSec = (pkt->pts != AV_NOPTS_VALUE)
                          ? toSeconds(pkt->pts, inStream->time_base) : 0.0;

            if (pktSec < entry.in_point - 0.002) { av_packet_unref(pkt); continue; }
            if (pktSec >= entry.out_point) {
                if (si == videoIdx_) { av_packet_unref(pkt); break; }
                av_packet_unref(pkt); continue;
            }

            if (!firstPktSeen[oi] && pkt->pts != AV_NOPTS_VALUE) {
                firstPts[oi]     = av_rescale_q(pkt->pts,
                                                inStream->time_base,
                                                outStream->time_base);
                firstPktSeen[oi] = true;
            }

            AVPacket* outPkt = av_packet_clone(pkt);
            auto rescaleTs = [&](int64_t& ts) {
                if (ts != AV_NOPTS_VALUE)
                    ts = av_rescale_q(ts, inStream->time_base, outStream->time_base)
                       - firstPts[oi] + ptsOffsets[oi];
            };
            rescaleTs(outPkt->pts);
            rescaleTs(outPkt->dts);
            if (outPkt->duration > 0)
                outPkt->duration = av_rescale_q(outPkt->duration,
                                                inStream->time_base, outStream->time_base);
            outPkt->pos          = -1;
            outPkt->stream_index = oi;
            av_interleaved_write_frame(outFmt, outPkt);
            av_packet_free(&outPkt);
            av_packet_unref(pkt);
        }
        av_packet_free(&pkt);

        for (int oi2 = 0; oi2 < outIdx; oi2++) {
            AVStream* outs = outFmt->streams[oi2];
            ptsOffsets[oi2] += static_cast<int64_t>(
                entry.duration() / av_q2d(outs->time_base));
        }
    }

    av_write_trailer(outFmt);
    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) avio_closep(&outFmt->pb);
    avformat_free_context(outFmt);
    VOSE_LOG("EDLエクスポート完了: " << outPath);
    reportProgress(1.0, "完了");
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 4: subtitleTrackFromVOSE()
// ════════════════════════════════════════════════════════════════════

SubtitleTrack VideoEngine::subtitleTrackFromVOSE(const std::string& voseJsonPath) {
    SubtitleTrack track;
    std::ifstream f(voseJsonPath);
    if (!f.is_open()) { VOSE_ERR("VO-SE JSON を開けません: " << voseJsonPath); return track; }
    std::string content((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());

    auto parseDouble = [&](const std::string& block,
                           const std::string& key, double def) -> double {
        std::string needle = "\"" + key + "\"";
        size_t k = block.find(needle);
        if (k == std::string::npos) return def;
        size_t colon = block.find(':', k + needle.size());
        if (colon == std::string::npos) return def;
        try { return std::stod(block.c_str() + colon + 1); }
        catch (...) { return def; }
    };
    auto parseStr = [&](const std::string& block, const std::string& key) -> std::string {
        std::string needle = "\"" + key + "\"";
        size_t k = block.find(needle);
        if (k == std::string::npos) return "";
        size_t c  = block.find(':', k + needle.size());
        size_t q1 = block.find('"', c + 1);
        if (q1 == std::string::npos) return "";
        size_t q2 = q1 + 1;
        while (q2 < block.size()) {
            if (block[q2] == '"' && block[q2 - 1] != '\\') break;
            q2++;
        }
        return block.substr(q1 + 1, q2 - q1 - 1);
    };

    size_t pos = 0;
    while ((pos = content.find('{', pos)) != std::string::npos) {
        size_t end = content.find('}', pos);
        if (end == std::string::npos) break;
        std::string block = content.substr(pos, end - pos + 1);
        SubtitleEntry e;
        e.start_sec = parseDouble(block, "start", 0.0);
        e.end_sec   = parseDouble(block, "end",   0.0);
        e.text      = parseStr(block, "text");
        e.x         = static_cast<float>(parseDouble(block, "x", 0.5));
        e.y         = static_cast<float>(parseDouble(block, "y", 0.85));
        e.font_size = static_cast<int>(parseDouble(block, "font_size", 48.0));
        e.style     = parseStr(block, "style");
        if (e.style.empty()) e.style = "normal";
        if (!e.text.empty() && e.end_sec > e.start_sec)
            track.push_back(e);
        pos = end + 1;
    }
    VOSE_LOG("字幕エントリ読み込み: " << track.size() << " 件");
    return track;
}

// ════════════════════════════════════════════════════════════════════
//  ASS ファイル生成ヘルパー
// ════════════════════════════════════════════════════════════════════

static std::string toAssTime(double sec) {
    int h = static_cast<int>(sec) / 3600;
    int m = (static_cast<int>(sec) % 3600) / 60;
    double s = std::fmod(sec, 60.0);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%d:%02d:%05.2f", h, m, s);
    return buf;
}

static bool writeAssFile(const SubtitleTrack& subs,
                          int vid_w, int vid_h,
                          const std::string& assPath) {
    std::ofstream ass(assPath);
    if (!ass.is_open()) return false;

    ass << "[Script Info]\n"
        << "ScriptType: v4.00+\n"
        << "PlayResX: " << vid_w << "\n"
        << "PlayResY: " << vid_h << "\n"
        << "WrapStyle: 0\n\n";

    ass << "[V4+ Styles]\n"
        << "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
           "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        << "Style: Default,Noto Sans CJK JP,48,&H00FFFFFF,&H000000FF,"
           "&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2.5,0,2,10,10,20,1\n"
        << "Style: Highlight,Noto Sans CJK JP,52,&H0000FFFF,&H000000FF,"
           "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,0,2,10,10,20,1\n\n";

    ass << "[Events]\n"
        << "Format: Layer, Start, End, Style, Name, "
           "MarginL, MarginR, MarginV, Effect, Text\n";

    for (const auto& sub : subs) {
        int px = static_cast<int>(sub.x * vid_w);
        int py = static_cast<int>(sub.y * vid_h);
        std::string styleName = (sub.style == "highlight") ? "Highlight" : "Default";
        ass << "Dialogue: 0,"
            << toAssTime(sub.start_sec) << ","
            << toAssTime(sub.end_sec)   << ","
            << styleName << ",,0,0,0,,"
            << "{\\pos(" << px << "," << py << ")}"
            << sub.text << "\n";
    }
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 4: exportWithSubtitles() — avfilter subtitles 完全実装
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::exportWithSubtitles(const EDL& edl,
                                       const SubtitleTrack& subs,
                                       const std::string& outPath) {
    if (!loaded_ || videoIdx_ < 0) return false;

    // ① ASS ファイル生成
    std::string assPath = outPath + ".tmp.ass";
    if (!writeAssFile(subs, width(), height(), assPath)) {
        VOSE_ERR("ASS ファイル生成失敗: " << assPath);
        return false;
    }
    VOSE_LOG("ASS ファイル生成: " << assPath);

    // ② 一時出力 (字幕なし) を生成してから avfilter で焼き込む 2-pass 方式
    //    (avfilter は encode 中に inline 適用)

    auto entries = edl.getEnabledEntries();
    if (entries.empty()) return false;

    // ── エンコーダ選択 ───────────────────────────────────────────
    const char* encNames[] = {
#if defined(__APPLE__)
        "h264_videotoolbox",
#endif
        "libx264", nullptr
    };
    const AVCodec* encoder = nullptr;
    for (int i = 0; encNames[i]; i++) {
        encoder = avcodec_find_encoder_by_name(encNames[i]);
        if (encoder) { VOSE_LOG("エンコーダ: " << encNames[i]); break; }
    }
    if (!encoder) { VOSE_ERR("エンコーダが見つかりません"); return false; }

    // ── 出力コンテキスト ─────────────────────────────────────────
    AVFormatContext* outFmt = nullptr;
    avformat_alloc_output_context2(&outFmt, nullptr, nullptr, outPath.c_str());
    if (!outFmt) { VOSE_ERR("出力コンテキスト失敗"); return false; }

    AVCodecContext* encCtx = avcodec_alloc_context3(encoder);
    encCtx->width     = videoCtx_->width;
    encCtx->height    = videoCtx_->height;
    encCtx->pix_fmt   = AV_PIX_FMT_YUV420P;
    encCtx->time_base = av_inv_q(fmtCtx_->streams[videoIdx_]->avg_frame_rate);
    encCtx->framerate = fmtCtx_->streams[videoIdx_]->avg_frame_rate;
    encCtx->gop_size  = 30;

    std::string encName = encoder->name;
    if (encName.find("videotoolbox") != std::string::npos) {
        av_opt_set_int(encCtx->priv_data, "q",       23, AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "profile", "high", AV_OPT_SEARCH_CHILDREN);
    } else {
        av_opt_set    (encCtx->priv_data, "preset", "medium", 0);
        av_opt_set_int(encCtx->priv_data, "crf",    23, AV_OPT_SEARCH_CHILDREN);
    }
    if (outFmt->oformat->flags & AVFMT_GLOBALHEADER)
        encCtx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    if (avcodec_open2(encCtx, encoder, nullptr) < 0) {
        VOSE_ERR("エンコーダ初期化失敗");
        avcodec_free_context(&encCtx);
        avformat_free_context(outFmt);
        return false;
    }

    AVStream* outVStream = avformat_new_stream(outFmt, encoder);
    avcodec_parameters_from_context(outVStream->codecpar, encCtx);
    outVStream->time_base = encCtx->time_base;

    // 音声ストリーム (コピー)
    AVStream* outAStream = nullptr;
    if (audioIdx_ >= 0) {
        outAStream = avformat_new_stream(outFmt, nullptr);
        avcodec_parameters_copy(outAStream->codecpar,
                                fmtCtx_->streams[audioIdx_]->codecpar);
        outAStream->codecpar->codec_tag = 0;
        outAStream->time_base = fmtCtx_->streams[audioIdx_]->time_base;
    }

    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) {
        if (avio_open(&outFmt->pb, outPath.c_str(), AVIO_FLAG_WRITE) < 0) {
            VOSE_ERR("avio_open 失敗");
            avcodec_free_context(&encCtx);
            avformat_free_context(outFmt);
            return false;
        }
    }
    avformat_write_header(outFmt, nullptr);

    // ── avfilter グラフ (subtitles + format) ─────────────────────
    AVFilterGraph*   filterGraph = avfilter_graph_alloc();
    AVFilterContext* srcCtx  = nullptr;
    AVFilterContext* sinkCtx = nullptr;
    bool filterOk = false;

    {
        const AVFilter* bufSrc  = avfilter_get_by_name("buffer");
        const AVFilter* bufSink = avfilter_get_by_name("buffersink");
        const AVFilter* subFilter = avfilter_get_by_name("subtitles");

        if (bufSrc && bufSink && subFilter) {
            AVRational sar = videoCtx_->sample_aspect_ratio;
            if (!sar.num) sar = {1, 1};
            char srcArgs[256];
            std::snprintf(srcArgs, sizeof(srcArgs),
                "video_size=%dx%d:pix_fmt=%d:time_base=%d/%d:pixel_aspect=%d/%d",
                videoCtx_->width, videoCtx_->height,
                AV_PIX_FMT_YUV420P,
                encCtx->time_base.num, encCtx->time_base.den,
                sar.num, sar.den);

            if (avfilter_graph_create_filter(
                    &srcCtx, bufSrc, "in", srcArgs, nullptr, filterGraph) >= 0 &&
                avfilter_graph_create_filter(
                    &sinkCtx, bufSink, "out", nullptr, nullptr, filterGraph) >= 0)
            {
                // subtitles フィルタ
                AVFilterContext* subCtx = nullptr;
                std::string subArg = "filename=" + assPath;
                if (avfilter_graph_create_filter(
                        &subCtx, subFilter, "subs",
                        subArg.c_str(), nullptr, filterGraph) >= 0)
                {
                    // format フィルタ (yuv420p に強制)
                    const AVFilter* fmtFilt = avfilter_get_by_name("format");
                    AVFilterContext* fmtCtx2 = nullptr;
                    if (fmtFilt && avfilter_graph_create_filter(
                            &fmtCtx2, fmtFilt, "fmt", "yuv420p",
                            nullptr, filterGraph) >= 0)
                    {
                        if (avfilter_link(srcCtx,  0, subCtx,  0) >= 0 &&
                            avfilter_link(subCtx,  0, fmtCtx2, 0) >= 0 &&
                            avfilter_link(fmtCtx2, 0, sinkCtx, 0) >= 0 &&
                            avfilter_graph_config(filterGraph, nullptr) >= 0)
                        {
                            filterOk = true;
                            VOSE_LOG("avfilter 字幕グラフ構築成功");
                        }
                    }
                }
            }
        }
        if (!filterOk) {
            VOSE_LOG("avfilter 字幕グラフ構築失敗 — 字幕なしでエンコード");
        }
    }

    // ── SWS (デコード → yuv420p 変換) ────────────────────────────
    SwsContext* encSws = sws_getContext(
        videoCtx_->width, videoCtx_->height, videoCtx_->pix_fmt,
        encCtx->width,    encCtx->height,    AV_PIX_FMT_YUV420P,
        SWS_BILINEAR, nullptr, nullptr, nullptr);

    AVFrame*  decFrame = av_frame_alloc();
    AVFrame*  yuvFrame = av_frame_alloc();
    AVPacket* inPkt    = av_packet_alloc();
    AVPacket* outPkt   = av_packet_alloc();

    yuvFrame->format = AV_PIX_FMT_YUV420P;
    yuvFrame->width  = encCtx->width;
    yuvFrame->height = encCtx->height;
    av_frame_get_buffer(yuvFrame, 0);

    int64_t frameIdx = 0;
    int64_t audioOff = 0;

    auto encodeFrame = [&](AVFrame* f) {
        f->pts = frameIdx++;
        AVFrame* filtOut = f;

        // avfilter に通す
        if (filterOk && srcCtx && sinkCtx) {
            if (av_buffersrc_add_frame_flags(srcCtx, f, AV_BUFFERSRC_FLAG_KEEP_REF) >= 0) {
                AVFrame* tmp = av_frame_alloc();
                while (av_buffersink_get_frame(sinkCtx, tmp) >= 0) {
                    tmp->pts = f->pts;
                    avcodec_send_frame(encCtx, tmp);
                    while (avcodec_receive_packet(encCtx, outPkt) == 0) {
                        av_packet_rescale_ts(outPkt, encCtx->time_base,
                                             outVStream->time_base);
                        outPkt->stream_index = 0;
                        av_interleaved_write_frame(outFmt, outPkt);
                        av_packet_unref(outPkt);
                    }
                    av_frame_unref(tmp);
                }
                av_frame_free(&tmp);
                return;
            }
        }
        // フォールバック: フィルタなし
        avcodec_send_frame(encCtx, filtOut);
        while (avcodec_receive_packet(encCtx, outPkt) == 0) {
            av_packet_rescale_ts(outPkt, encCtx->time_base, outVStream->time_base);
            outPkt->stream_index = 0;
            av_interleaved_write_frame(outFmt, outPkt);
            av_packet_unref(outPkt);
        }
    };

    auto flushEncoder = [&]() {
        if (filterOk && srcCtx) av_buffersrc_add_frame_flags(srcCtx, nullptr, 0);
        avcodec_send_frame(encCtx, nullptr);
        while (avcodec_receive_packet(encCtx, outPkt) == 0) {
            av_packet_rescale_ts(outPkt, encCtx->time_base, outVStream->time_base);
            outPkt->stream_index = 0;
            av_interleaved_write_frame(outFmt, outPkt);
            av_packet_unref(outPkt);
        }
    };

    for (size_t ei = 0; ei < entries.size(); ei++) {
        const auto& entry = entries[ei];
        reportProgress(static_cast<double>(ei) / entries.size(), "字幕付きエンコード中");

        double seekTarget = !keyframeIdx_.empty()
                          ? findNearestKeyframe(entry.in_point) : entry.in_point;
        seekAndFlush(seekTarget);

        while (av_read_frame(fmtCtx_, inPkt) >= 0) {
            if (inPkt->stream_index == videoIdx_) {
                double pktSec = toSeconds(inPkt->pts,
                                           fmtCtx_->streams[videoIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); break; }

                if (avcodec_send_packet(videoCtx_, inPkt) == 0) {
                    while (avcodec_receive_frame(videoCtx_, decFrame) == 0) {
                        sws_scale(encSws,
                                  decFrame->data, decFrame->linesize,
                                  0, decFrame->height,
                                  yuvFrame->data, yuvFrame->linesize);
                        encodeFrame(yuvFrame);
                        av_frame_unref(decFrame);
                    }
                }
            } else if (inPkt->stream_index == audioIdx_ && outAStream) {
                double pktSec = toSeconds(inPkt->pts,
                                           fmtCtx_->streams[audioIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); continue; }

                AVPacket* ap = av_packet_clone(inPkt);
                av_packet_rescale_ts(ap, fmtCtx_->streams[audioIdx_]->time_base,
                                     outAStream->time_base);
                ap->pts         += audioOff;
                ap->dts         += audioOff;
                ap->pos          = -1;
                ap->stream_index = 1;
                av_interleaved_write_frame(outFmt, ap);
                av_packet_free(&ap);
            }
            av_packet_unref(inPkt);
        }

        flushEncoder();
        if (outAStream) {
            audioOff += static_cast<int64_t>(
                entry.duration() / av_q2d(outAStream->time_base));
        }
    }

    av_write_trailer(outFmt);

    // クリーンアップ
    avfilter_graph_free(&filterGraph);
    sws_freeContext(encSws);
    av_frame_free(&decFrame);
    av_frame_free(&yuvFrame);
    av_packet_free(&inPkt);
    av_packet_free(&outPkt);
    avcodec_free_context(&encCtx);
    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) avio_closep(&outFmt->pb);
    avformat_free_context(outFmt);

    // 一時 ASS ファイル削除
    std::remove(assPath.c_str());

    VOSE_LOG("字幕付きエクスポート完了: " << outPath);
    reportProgress(1.0, "完了");
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 5: exportWithVideoToolbox()
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::exportWithVideoToolbox(const EDL& edl,
                                          const std::string& outPath,
                                          int crfQuality,
                                          const std::string& preset) {
    if (!loaded_ || videoIdx_ < 0) return false;

    auto entries = edl.getEnabledEntries();
    if (entries.empty()) return false;

    AVFormatContext* outFmt = nullptr;
    avformat_alloc_output_context2(&outFmt, nullptr, nullptr, outPath.c_str());
    if (!outFmt) { VOSE_ERR("出力コンテキスト生成失敗"); return false; }

    const char* encoderNames[] = {
#if defined(__APPLE__)
        "hevc_videotoolbox",
        "h264_videotoolbox",
#endif
        "libx265",
        "libx264",
        nullptr
    };
    const AVCodec* encoder = nullptr;
    for (int i = 0; encoderNames[i]; i++) {
        encoder = avcodec_find_encoder_by_name(encoderNames[i]);
        if (encoder) { VOSE_LOG("エンコーダ選択: " << encoderNames[i]); break; }
    }
    if (!encoder) {
        VOSE_ERR("利用可能なエンコーダが見つかりません");
        avformat_free_context(outFmt);
        return false;
    }

    AVCodecContext* encCtx = avcodec_alloc_context3(encoder);
    encCtx->width     = videoCtx_->width;
    encCtx->height    = videoCtx_->height;
    encCtx->pix_fmt   = AV_PIX_FMT_YUV420P;
    encCtx->time_base = av_inv_q(fmtCtx_->streams[videoIdx_]->avg_frame_rate);
    encCtx->framerate = fmtCtx_->streams[videoIdx_]->avg_frame_rate;
    encCtx->gop_size  = 30;

    std::string encName = encoder->name;
    if (encName.find("videotoolbox") != std::string::npos) {
        av_opt_set_int(encCtx->priv_data, "q",        crfQuality, AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "profile",  "high",     AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "realtime", "0",        AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "allow_sw", "0",        AV_OPT_SEARCH_CHILDREN);
    } else {
        av_opt_set    (encCtx->priv_data, "preset", preset.c_str(), 0);
        av_opt_set_int(encCtx->priv_data, "crf",    crfQuality, AV_OPT_SEARCH_CHILDREN);
    }
    if (outFmt->oformat->flags & AVFMT_GLOBALHEADER)
        encCtx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    if (avcodec_open2(encCtx, encoder, nullptr) < 0) {
        VOSE_ERR("エンコーダ初期化失敗");
        avcodec_free_context(&encCtx);
        avformat_free_context(outFmt);
        return false;
    }

    AVStream* outVStream = avformat_new_stream(outFmt, encoder);
    avcodec_parameters_from_context(outVStream->codecpar, encCtx);
    outVStream->time_base = encCtx->time_base;

    AVStream* outAStream = nullptr;
    if (audioIdx_ >= 0) {
        outAStream = avformat_new_stream(outFmt, nullptr);
        avcodec_parameters_copy(outAStream->codecpar,
                                fmtCtx_->streams[audioIdx_]->codecpar);
        outAStream->codecpar->codec_tag = 0;
        outAStream->time_base = fmtCtx_->streams[audioIdx_]->time_base;
    }

    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) {
        if (avio_open(&outFmt->pb, outPath.c_str(), AVIO_FLAG_WRITE) < 0) {
            VOSE_ERR("avio_open 失敗");
            avcodec_free_context(&encCtx);
            avformat_free_context(outFmt);
            return false;
        }
    }
    avformat_write_header(outFmt, nullptr);

    SwsContext* encSws = sws_getContext(
        videoCtx_->width, videoCtx_->height, videoCtx_->pix_fmt,
        encCtx->width,    encCtx->height,    AV_PIX_FMT_YUV420P,
        SWS_BILINEAR, nullptr, nullptr, nullptr);

    AVFrame*  decFrame = av_frame_alloc();
    AVFrame*  encFrame = av_frame_alloc();
    AVPacket* inPkt    = av_packet_alloc();
    AVPacket* outPkt   = av_packet_alloc();

    encFrame->format = AV_PIX_FMT_YUV420P;
    encFrame->width  = encCtx->width;
    encFrame->height = encCtx->height;
    av_frame_get_buffer(encFrame, 0);

    int64_t frameIdx = 0;
    int64_t audioOff = 0;

    auto flushEncoder = [&]() {
        avcodec_send_frame(encCtx, nullptr);
        while (avcodec_receive_packet(encCtx, outPkt) == 0) {
            av_packet_rescale_ts(outPkt, encCtx->time_base, outVStream->time_base);
            outPkt->stream_index = 0;
            av_interleaved_write_frame(outFmt, outPkt);
            av_packet_unref(outPkt);
        }
    };

    for (size_t ei = 0; ei < entries.size(); ei++) {
        const auto& entry = entries[ei];
        reportProgress(static_cast<double>(ei) / entries.size(), "HWエンコード中");

        double seekTarget = !keyframeIdx_.empty()
                          ? findNearestKeyframe(entry.in_point) : entry.in_point;
        seekAndFlush(seekTarget);

        while (av_read_frame(fmtCtx_, inPkt) >= 0) {
            if (inPkt->stream_index == videoIdx_) {
                double pktSec = toSeconds(inPkt->pts,
                                           fmtCtx_->streams[videoIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); break; }

                if (avcodec_send_packet(videoCtx_, inPkt) == 0) {
                    while (avcodec_receive_frame(videoCtx_, decFrame) == 0) {
                        sws_scale(encSws,
                                  decFrame->data, decFrame->linesize,
                                  0, decFrame->height,
                                  encFrame->data, encFrame->linesize);
                        encFrame->pts = frameIdx++;
                        avcodec_send_frame(encCtx, encFrame);
                        while (avcodec_receive_packet(encCtx, outPkt) == 0) {
                            av_packet_rescale_ts(outPkt, encCtx->time_base,
                                                 outVStream->time_base);
                            outPkt->stream_index = 0;
                            av_interleaved_write_frame(outFmt, outPkt);
                            av_packet_unref(outPkt);
                        }
                        av_frame_free(&decFrame);
                        decFrame = av_frame_alloc();
                    }
                }
            } else if (inPkt->stream_index == audioIdx_ && outAStream) {
                double pktSec = toSeconds(inPkt->pts,
                                           fmtCtx_->streams[audioIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); continue; }

                AVPacket* ap = av_packet_clone(inPkt);
                av_packet_rescale_ts(ap, fmtCtx_->streams[audioIdx_]->time_base,
                                     outAStream->time_base);
                ap->pts         += audioOff;
                ap->dts         += audioOff;
                ap->pos          = -1;
                ap->stream_index = 1;
                av_interleaved_write_frame(outFmt, ap);
                av_packet_free(&ap);
            }
            av_packet_unref(inPkt);
        }
        flushEncoder();
        if (outAStream) {
            audioOff += static_cast<int64_t>(
                entry.duration() / av_q2d(outAStream->time_base));
        }
    }

    av_write_trailer(outFmt);
    sws_freeContext(encSws);
    av_frame_free(&decFrame);
    av_frame_free(&encFrame);
    av_packet_free(&inPkt);
    av_packet_free(&outPkt);
    avcodec_free_context(&encCtx);
    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) avio_closep(&outFmt->pb);
    avformat_free_context(outFmt);

    VOSE_LOG("HWエクスポート完了: " << outPath);
    reportProgress(1.0, "完了");
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  C API
//  ★ vose_waveform のシグネチャを Python ラッパーと完全一致させる
//     (void* h, float* out_buf, int buf_size, int chunks) → int
// ════════════════════════════════════════════════════════════════════

extern "C" {

void* vose_create()                { return new vose::VideoEngine(); }
void  vose_destroy(void* h)        { delete static_cast<vose::VideoEngine*>(h); }

int vose_load(void* h, const char* path) {
    return static_cast<vose::VideoEngine*>(h)->load(path) ? 1 : 0;
}
double vose_duration(void* h)  { return static_cast<vose::VideoEngine*>(h)->duration(); }
int    vose_width(void* h)     { return static_cast<vose::VideoEngine*>(h)->width(); }
int    vose_height(void* h)    { return static_cast<vose::VideoEngine*>(h)->height(); }
double vose_fps(void* h)       { return static_cast<vose::VideoEngine*>(h)->fps(); }
int    vose_has_audio(void* h) {
    return static_cast<vose::VideoEngine*>(h)->hasAudio() ? 1 : 0;
}
int vose_save_frame(void* h, double time_sec, const char* out_path) {
    return static_cast<vose::VideoEngine*>(h)->saveFrame(time_sec, out_path) ? 1 : 0;
}

/**
 * vose_waveform
 *   h        : VideoEngine ハンドル
 *   out_buf  : float 配列 (呼び出し側が buf_size 分確保)
 *   buf_size : out_buf のサイズ
 *   chunks   : 欲しいサンプル数 (buf_size 以下)
 *   戻り値   : 書き込んだ要素数
 */
int vose_waveform(void* h, float* out_buf, int buf_size, int chunks) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    int   req = std::min(buf_size, chunks);
    auto  wd  = eng->extractWaveform(req);
    int   n   = static_cast<int>(
                    std::min(static_cast<size_t>(req), wd.peaks_max.size()));
    for (int i = 0; i < n; i++) out_buf[i] = wd.peaks_max[i];
    return n;
}

int vose_export_edl(void* h, const char* edl_json, const char* out_path) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    vose::EDL edl;
    if (!edl.deserialize(edl_json)) return 0;
    return eng->exportFromEDL(edl, out_path) ? 1 : 0;
}
int vose_export_hw(void* h, const char* edl_json,
                   const char* out_path, int quality) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    vose::EDL edl;
    if (!edl.deserialize(edl_json)) return 0;
    return eng->exportWithVideoToolbox(edl, out_path, quality) ? 1 : 0;
}

/**
 * vose_export_with_subtitles — 字幕付きエクスポート C API
 *   edl_json     : EDL JSON 文字列
 *   subtitle_json: VO-SE 字幕 JSON ファイルパス
 *   out_path     : 出力ファイルパス
 */
int vose_export_with_subtitles(void* h,
                                const char* edl_json,
                                const char* subtitle_json_path,
                                const char* out_path) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    vose::EDL edl;
    if (!edl.deserialize(edl_json)) return 0;
    auto subs = eng->subtitleTrackFromVOSE(subtitle_json_path);
    return eng->exportWithSubtitles(edl, subs, out_path) ? 1 : 0;
}

int    vose_build_keyframe_index(void* h) {
    return static_cast<int>(
        static_cast<vose::VideoEngine*>(h)->buildKeyframeIndex().size());
}
double vose_nearest_keyframe(void* h, double time_sec) {
    return static_cast<vose::VideoEngine*>(h)->findNearestKeyframe(time_sec);
}

} // extern "C"

} // namespace vose
