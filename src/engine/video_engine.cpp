// ╔══════════════════════════════════════════════════════════════════╗
// ║  VOSE Video Engine — video_engine.cpp                           ║
// ║  Phase 1: Core Integration (frame/waveform extraction)          ║
// ║  Phase 2: Logic Design    (keyframe index, EDL export)          ║
// ║  Phase 4: VO-SE Subtitles (ASS burn-in via avfilter)           ║
// ║  Phase 5: HW Optimization (Apple VideoToolbox encoder)          ║
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

// FFmpeg の具体的な定義を読み込む（これが足りなかった）
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
}

namespace vose {

// ─── ローカルユーティリティ ────────────────────────────────────────────

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

VideoEngine::VideoEngine() {
    VOSE_LOG("Engine created.");
}

VideoEngine::~VideoEngine() {
    releaseResources();
    VOSE_LOG("Engine destroyed.");
}

void VideoEngine::releaseResources() {
    if (swsCtx_)   { sws_freeContext(swsCtx_);   swsCtx_   = nullptr; }
    if (videoCtx_) { avcodec_free_context(&videoCtx_); }
    if (audioCtx_) { avcodec_free_context(&audioCtx_); }
    if (fmtCtx_)   { avformat_close_input(&fmtCtx_); }
    videoIdx_ = -1;
    audioIdx_ = -1;
    loaded_   = false;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 1: load()
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::load(const std::string& filepath) {
    releaseResources();
    filePath_ = filepath;
    int ret;

    // ① ファイルオープン
    ret = avformat_open_input(&fmtCtx_, filepath.c_str(), nullptr, nullptr);
    if (ret != 0) {
        VOSE_ERR("avformat_open_input: " << avErr(ret));
        return false;
    }

    // ② ストリーム情報の取得
    ret = avformat_find_stream_info(fmtCtx_, nullptr);
    if (ret < 0) {
        VOSE_ERR("avformat_find_stream_info: " << avErr(ret));
        return false;
    }

    // ③ ベストな映像ストリームを検索
    const AVCodec* videoDecoder = nullptr;
    videoIdx_ = av_find_best_stream(fmtCtx_, AVMEDIA_TYPE_VIDEO, -1, -1, &videoDecoder, 0);
    if (videoIdx_ < 0) {
        VOSE_ERR("映像ストリームが見つかりません");
        return false;
    }

    // ④ 映像コーデックを開く
    videoCtx_ = avcodec_alloc_context3(videoDecoder);
    if (!videoCtx_) {
        VOSE_ERR("avcodec_alloc_context3 (video) 失敗");
        return false;
    }
    avcodec_parameters_to_context(videoCtx_, fmtCtx_->streams[videoIdx_]->codecpar);
    ret = avcodec_open2(videoCtx_, videoDecoder, nullptr);
    if (ret < 0) {
        VOSE_ERR("avcodec_open2 (video): " << avErr(ret));
        return false;
    }

    // ⑤ 音声ストリームを検索（なくても続行）
    const AVCodec* audioDecoder = nullptr;
    audioIdx_ = av_find_best_stream(fmtCtx_, AVMEDIA_TYPE_AUDIO, -1, -1, &audioDecoder, 0);
    if (audioIdx_ >= 0 && audioDecoder) {
        audioCtx_ = avcodec_alloc_context3(audioDecoder);
        if (audioCtx_) {
            avcodec_parameters_to_context(audioCtx_, fmtCtx_->streams[audioIdx_]->codecpar);
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

// ─── アクセサ ──────────────────────────────────────────────────────

int    VideoEngine::width()    const { return loaded_ ? videoCtx_->width  : 0; }
int    VideoEngine::height()   const { return loaded_ ? videoCtx_->height : 0; }
double VideoEngine::fps()      const {
    if (!loaded_) return 0.0;
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
    return loaded_ ? avcodec_get_name(videoCtx_->codec_id) : "";
}
double VideoEngine::toSeconds(int64_t pts, AVRational tb) const {
    return static_cast<double>(pts) * av_q2d(tb);
}
void VideoEngine::reportProgress(double p, const std::string& stage) {
    if (progressCb_) progressCb_(std::min(p, 1.0), stage);
}

// ─── シーク + コーデックバッファフラッシュ ────────────────────────

bool VideoEngine::seekAndFlush(double timeSec) {
    AVStream* vs = fmtCtx_->streams[videoIdx_];
    int64_t ts   = static_cast<int64_t>(timeSec / av_q2d(vs->time_base));

    // まずストリーム単位でシーク
    int ret = av_seek_frame(fmtCtx_, videoIdx_, ts, AVSEEK_FLAG_BACKWARD);
    if (ret < 0) {
        // フォールバック: AV_TIME_BASE 単位でシーク
        int64_t ts_us = static_cast<int64_t>(timeSec * AV_TIME_BASE);
        ret = avformat_seek_file(fmtCtx_, -1, INT64_MIN, ts_us, INT64_MAX, 0);
    }

    // コーデック内部バッファをフラッシュ（これをしないと古いフレームが返る）
    avcodec_flush_buffers(videoCtx_);
    if (audioCtx_) avcodec_flush_buffers(audioCtx_);

    return ret >= 0;
}

// ─── SwsContext のキャッシュ生成 ───────────────────────────────────

SwsContext* VideoEngine::makeSwsCtx(int w, int h, AVPixelFormat srcFmt) {
    // 既存のコンテキストを解放して再生成（サイズや形式が変わる可能性）
    sws_freeContext(swsCtx_);
    swsCtx_ = sws_getContext(w, h, srcFmt,
                             w, h, AV_PIX_FMT_RGB24,
                             SWS_BILINEAR, nullptr, nullptr, nullptr);
    return swsCtx_;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 1: extractFrame() — フレームをメモリに取得
// ════════════════════════════════════════════════════════════════════

std::optional<FrameInfo> VideoEngine::extractFrame(double timeSec) {
    if (!loaded_) return std::nullopt;

    seekAndFlush(timeSec);

    AVPacket* pkt   = av_packet_alloc();
    AVFrame*  frame = av_frame_alloc();
    std::optional<FrameInfo> result;

    while (av_read_frame(fmtCtx_, pkt) >= 0) {
        if (pkt->stream_index != videoIdx_) {
            av_packet_unref(pkt);
            continue;
        }

        if (avcodec_send_packet(videoCtx_, pkt) == 0) {
            if (avcodec_receive_frame(videoCtx_, frame) == 0) {
                // RGB24 変換フレームを準備
                int w = frame->width, h = frame->height;
                AVFrame* rgb = av_frame_alloc();
                rgb->format  = AV_PIX_FMT_RGB24;
                rgb->width   = w;
                rgb->height  = h;
                av_frame_get_buffer(rgb, 1);

                SwsContext* sws = makeSwsCtx(w, h, (AVPixelFormat)frame->format);
                sws_scale(sws,
                          frame->data, frame->linesize, 0, h,
                          rgb->data,   rgb->linesize);

                FrameInfo fi;
                fi.width       = w;
                fi.height      = h;
                fi.is_keyframe = (frame->flags & AV_FRAME_FLAG_KEY) != 0;
                fi.pts_seconds = toSeconds(frame->pts, fmtCtx_->streams[videoIdx_]->time_base);

                // packed RGB24 コピー（linesize != width*3 の場合に対応）
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

// ─── saveFrame() — extractFrame() → PPMファイル保存 ─────────────

bool VideoEngine::saveFrame(double timeSec, const std::string& outPath) {
    auto fi = extractFrame(timeSec);
    if (!fi) {
        VOSE_ERR("フレーム取得失敗 at " << timeSec << "s");
        return false;
    }
    std::ofstream ofs(outPath, std::ios::binary);
    if (!ofs) {
        VOSE_ERR("出力ファイルを開けません: " << outPath);
        return false;
    }
    ofs << "P6\n" << fi->width << " " << fi->height << "\n255\n";
    ofs.write(reinterpret_cast<const char*>(fi->rgb_data.data()),
              static_cast<std::streamsize>(fi->rgb_data.size()));
    VOSE_LOG("フレーム保存: " << outPath << " (" << fi->pts_seconds << "s)");
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 1: extractWaveform() — 音声波形の生成
// ════════════════════════════════════════════════════════════════════

WaveformData VideoEngine::extractWaveform(int chunks) {
    WaveformData result;
    if (!loaded_ || audioIdx_ < 0) {
        VOSE_LOG("音声ストリームなし。波形生成をスキップ。");
        return result;
    }

    // SwrContext を設定: 任意フォーマット → mono / f32
    SwrContext* swr = swr_alloc();

    // 入力チャンネルレイアウトをコピー
    AVChannelLayout in_layout;
    av_channel_layout_copy(&in_layout, &audioCtx_->ch_layout);

    // 出力: モノラル
    AVChannelLayout out_layout = AV_CHANNEL_LAYOUT_MONO;

    av_opt_set_chlayout   (swr, "in_chlayout",    &in_layout,            0);
    av_opt_set_chlayout   (swr, "out_chlayout",   &out_layout,           0);
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

    // 音声ストリームの先頭へシーク
    avformat_seek_file(fmtCtx_, audioIdx_, 0, 0, 0, 0);
    avcodec_flush_buffers(audioCtx_);

    // 全サンプルを収集
    double dur = duration();
    std::vector<float> allSamples;
    allSamples.reserve(static_cast<size_t>(audioCtx_->sample_rate * dur) + 1024);

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
                    allSamples.insert(allSamples.end(), converted.begin(), converted.end());
                    av_frame_unref(frame);
                }
            }
        }
        av_packet_unref(pkt);

        // プログレス更新
        if (dur > 0.0 && audioCtx_->sample_rate > 0) {
            double prog = static_cast<double>(allSamples.size())
                        / (audioCtx_->sample_rate * dur);
            reportProgress(prog, "波形抽出中");
        }
    }

    av_frame_free(&frame);
    av_packet_free(&pkt);
    swr_free(&swr);

    if (allSamples.empty()) {
        VOSE_LOG("音声サンプルが0件でした。");
        return result;
    }

    // チャンク単位でピーク/RMSを計算
    result.sample_rate  = audioCtx_->sample_rate;
    result.channels     = 1;
    result.duration_sec = dur;
    result.chunks       = chunks;
    result.peaks_max.assign(chunks, 0.0f);
    result.peaks_min.assign(chunks, 0.0f);
    result.rms.assign(chunks,       0.0f);

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

    reportProgress(1.0, "波形生成完了");
    VOSE_LOG("波形生成: " << chunks << " chunks / " << allSamples.size() << " samples");
    return result;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 2: buildKeyframeIndex() — キーフレームインデックス構築
// ════════════════════════════════════════════════════════════════════

std::vector<KeyframeIndex> VideoEngine::buildKeyframeIndex() {
    if (!loaded_) return {};
    keyframeIdx_.clear();

    // 映像ストリーム先頭へシーク
    avformat_seek_file(fmtCtx_, videoIdx_, 0, 0, 0, AVSEEK_FLAG_BACKWARD);
    avcodec_flush_buffers(videoCtx_);

    AVStream* vs  = fmtCtx_->streams[videoIdx_];
    AVPacket* pkt = av_packet_alloc();
    double    dur = duration();

    VOSE_LOG("キーフレームインデックス構築開始...");

    while (av_read_frame(fmtCtx_, pkt) >= 0) {
        if (pkt->stream_index == videoIdx_ && (pkt->flags & AV_PKT_FLAG_KEY)) {
            KeyframeIndex kf;
            kf.pts_raw     = pkt->pts;
            kf.dts_raw     = pkt->dts;
            kf.pts_seconds = toSeconds(pkt->pts, vs->time_base);
            kf.file_pos    = avio_tell(fmtCtx_->pb);
            keyframeIdx_.push_back(kf);

            if (dur > 0.0)
                reportProgress(kf.pts_seconds / dur, "キーフレームインデックス");
        }
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);

    VOSE_LOG("キーフレーム数: " << keyframeIdx_.size());
    reportProgress(1.0, "インデックス完了");
    return keyframeIdx_;
}

// ─── findNearestKeyframe() ────────────────────────────────────────

double VideoEngine::findNearestKeyframe(double timeSec) const {
    if (keyframeIdx_.empty()) return timeSec;

    // timeSec 以下の最後のキーフレームを二分探索
    auto it = std::upper_bound(
        keyframeIdx_.begin(), keyframeIdx_.end(), timeSec,
        [](double t, const KeyframeIndex& kf) { return t < kf.pts_seconds; });

    if (it == keyframeIdx_.begin())
        return keyframeIdx_.front().pts_seconds;
    --it;
    return it->pts_seconds;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 2: exportFromEDL() — ストリームコピーによる非破壊エクスポート
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::exportFromEDL(const EDL& edl, const std::string& outPath) {
    if (!loaded_) return false;

    auto entries = edl.getEnabledEntries();
    if (entries.empty()) {
        VOSE_LOG("EDLに有効なエントリがありません。");
        return false;
    }

    // ── 出力フォーマットコンテキスト生成 ──
    AVFormatContext* outFmt = nullptr;
    int ret = avformat_alloc_output_context2(&outFmt, nullptr, nullptr, outPath.c_str());
    if (ret < 0 || !outFmt) {
        VOSE_ERR("出力コンテキスト生成失敗: " << avErr(ret));
        return false;
    }

    // ── 入力ストリームを出力にコピー（映像 + 音声のみ） ──
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
        out->time_base           = in->time_base;
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

    // ── 各セグメントをパケット単位でコピー ──
    // 出力PTSの連続性を保つためにオフセットを追跡する
    std::vector<int64_t> ptsOffsets(outIdx, 0);
    std::vector<int64_t> firstPts(outIdx, AV_NOPTS_VALUE);
    std::vector<bool>    firstPktSeen(outIdx, false);

    for (size_t ei = 0; ei < entries.size(); ei++) {
        const auto& entry = entries[ei];
        reportProgress(static_cast<double>(ei) / entries.size(), "エクスポート中");

        // キーフレームにアライメントしてシーク
        double seekTarget = !keyframeIdx_.empty()
                          ? findNearestKeyframe(entry.in_point)
                          : entry.in_point;
        seekAndFlush(seekTarget);
        std::fill(firstPktSeen.begin(), firstPktSeen.end(), false);
        std::fill(firstPts.begin(),     firstPts.end(),     AV_NOPTS_VALUE);

        AVPacket* pkt = av_packet_alloc();
        while (av_read_frame(fmtCtx_, pkt) >= 0) {
            int si = pkt->stream_index;
            if (si >= static_cast<int>(streamMap.size()) || streamMap[si] < 0) {
                av_packet_unref(pkt);
                continue;
            }

            AVStream* inStream  = fmtCtx_->streams[si];
            int       oi        = streamMap[si];
            AVStream* outStream = outFmt->streams[oi];

            // PTS を秒換算して区間外をスキップ/終了
            double pktSec = AV_NOPTS_VALUE != pkt->pts
                          ? toSeconds(pkt->pts, inStream->time_base)
                          : 0.0;

            if (pktSec < entry.in_point - 0.002) {
                av_packet_unref(pkt);
                continue;
            }
            if (pktSec >= entry.out_point) {
                // 映像ストリームのみ終了判定
                if (si == videoIdx_) {
                    av_packet_unref(pkt);
                    break;
                }
                av_packet_unref(pkt);
                continue;
            }

            // 各ストリームの最初のPTSを記録
            if (!firstPktSeen[oi] && pkt->pts != AV_NOPTS_VALUE) {
                firstPts[oi]     = av_rescale_q(pkt->pts, inStream->time_base, outStream->time_base);
                firstPktSeen[oi] = true;
            }

            // タイムスタンプのリスケール + 連続化
            AVPacket* outPkt = av_packet_clone(pkt);
            if (outPkt->pts != AV_NOPTS_VALUE) {
                int64_t scaled = av_rescale_q(outPkt->pts, inStream->time_base, outStream->time_base);
                outPkt->pts    = scaled - firstPts[oi] + ptsOffsets[oi];
            }
            if (outPkt->dts != AV_NOPTS_VALUE) {
                int64_t scaled = av_rescale_q(outPkt->dts, inStream->time_base, outStream->time_base);
                outPkt->dts    = scaled - firstPts[oi] + ptsOffsets[oi];
            }
            if (outPkt->duration > 0) {
                outPkt->duration = av_rescale_q(outPkt->duration,
                                                inStream->time_base, outStream->time_base);
            }
            outPkt->pos          = -1;
            outPkt->stream_index = oi;

            av_interleaved_write_frame(outFmt, outPkt);
            av_packet_free(&outPkt);
            av_packet_unref(pkt);
        }
        av_packet_free(&pkt);

        // PTSオフセットをセグメント長分だけ進める
        for (int oi2 = 0; oi2 < outIdx; oi2++) {
            AVStream* outs = outFmt->streams[oi2];
            int64_t segLen = static_cast<int64_t>(
                entry.duration() / av_q2d(outs->time_base));
            ptsOffsets[oi2] += segLen;
        }
    }

    av_write_trailer(outFmt);
    if (!(outFmt->oformat->flags & AVFMT_NOFILE))
        avio_closep(&outFmt->pb);
    avformat_free_context(outFmt);

    VOSE_LOG("EDLエクスポート完了: " << outPath);
    reportProgress(1.0, "完了");
    return true;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 4: subtitleTrackFromVOSE() — VO-SE JSONを字幕トラックへ
// ════════════════════════════════════════════════════════════════════

SubtitleTrack VideoEngine::subtitleTrackFromVOSE(const std::string& voseJsonPath) {
    // VO-SE 出力フォーマット例:
    // [
    //   {"start": 1.0, "end": 3.5, "text": "こんにちは", "x": 0.5, "y": 0.85},
    //   ...
    // ]
    SubtitleTrack track;
    std::ifstream f(voseJsonPath);
    if (!f.is_open()) {
        VOSE_ERR("VO-SE JSONを開けません: " << voseJsonPath);
        return track;
    }
    std::string content((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());

    // ミニマムJSONパーサ（nlohmann/jsonへの差し替え推奨）
    auto parseDouble = [&](const std::string& block, const std::string& key, double def) -> double {
        std::string needle = "\"" + key + "\"";
        size_t k = block.find(needle);
        if (k == std::string::npos) return def;
        size_t colon = block.find(':', k + needle.size());
        if (colon == std::string::npos) return def;
        try { return std::stod(block.c_str() + colon + 1); } catch (...) { return def; }
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

// ─── exportWithSubtitles() — ASSファイル生成 + バーンイン ─────────

bool VideoEngine::exportWithSubtitles(const EDL& edl,
                                       const SubtitleTrack& subs,
                                       const std::string& outPath) {
    if (!loaded_) return false;

    // ① ASSファイルを生成
    std::string assPath = outPath + ".tmp.ass";
    {
        std::ofstream ass(assPath);
        ass << "[Script Info]\n"
            << "ScriptType: v4.00+\n"
            << "PlayResX: " << width()  << "\n"
            << "PlayResY: " << height() << "\n"
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
            << "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n";

        auto toAssTime = [](double sec) -> std::string {
            int h   = static_cast<int>(sec) / 3600;
            int m   = (static_cast<int>(sec) % 3600) / 60;
            double s = std::fmod(sec, 60.0);
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%d:%02d:%05.2f", h, m, s);
            return buf;
        };

        for (const auto& sub : subs) {
            int px = static_cast<int>(sub.x * width());
            int py = static_cast<int>(sub.y * height());
            std::string styleName = (sub.style == "highlight") ? "Highlight" : "Default";

            // {\pos(x,y)} で位置を指定
            ass << "Dialogue: 0,"
                << toAssTime(sub.start_sec) << ","
                << toAssTime(sub.end_sec)   << ","
                << styleName                << ",,0,0,0,,"
                << "{\\pos(" << px << "," << py << ")}"
                << sub.text << "\n";
        }
    }

    VOSE_LOG("ASSファイル生成: " << assPath);

    // ② EDLに従ってエクスポート（ストリームコピー）し、その後
    //    ASSファイルをlibavfilterのsubtitlesフィルタでバーンインする実装を
    //    本番環境では AVFilterGraph を使って構築する。
    //    ここでは非破壊エクスポートを行い、ASSパスを返す。
    bool ok = exportFromEDL(edl, outPath);

    VOSE_LOG("字幕バーンイン用ASSファイル: " << assPath);
    VOSE_LOG("本番実装: avfilter subtitles=" << assPath << " で再エンコードが必要");
    return ok;
}

// ════════════════════════════════════════════════════════════════════
//  Phase 5: exportWithVideoToolbox() — Appleシリコン対応HWエンコード
// ════════════════════════════════════════════════════════════════════

bool VideoEngine::exportWithVideoToolbox(const EDL& edl,
                                          const std::string& outPath,
                                          int crfQuality,
                                          const std::string& preset) {
    if (!loaded_) return false;

#if !defined(__APPLE__)
    VOSE_LOG("VideoToolboxはmacOS専用です。libx264にフォールバックします。");
    // ソフトウェアエンコードで代替
#endif

    auto entries = edl.getEnabledEntries();
    if (entries.empty()) return false;

    // ── 出力コンテキスト生成 ──
    AVFormatContext* outFmt = nullptr;
    avformat_alloc_output_context2(&outFmt, nullptr, nullptr, outPath.c_str());
    if (!outFmt) { VOSE_ERR("出力コンテキスト生成失敗"); return false; }

    // ── VideoToolboxエンコーダを探す（フォールバックあり） ──
    const char* encoderNames[] = {
#if defined(__APPLE__)
        "hevc_videotoolbox",   // Apple M1/M2/M3: HEVC/H.265 HW
        "h264_videotoolbox",   // Apple: H.264 HW
#endif
        "libx265",             // ソフトウェアH.265
        "libx264",             // ソフトウェアH.264
        nullptr
    };
    const AVCodec* encoder = nullptr;
    for (int i = 0; encoderNames[i]; i++) {
        encoder = avcodec_find_encoder_by_name(encoderNames[i]);
        if (encoder) { VOSE_LOG("エンコーダ選択: " << encoderNames[i]); break; }
    }
    if (!encoder) { VOSE_ERR("利用可能なエンコーダが見つかりません"); avformat_free_context(outFmt); return false; }

    // ── エンコーダコンテキスト設定 ──
    AVCodecContext* encCtx = avcodec_alloc_context3(encoder);
    encCtx->width     = videoCtx_->width;
    encCtx->height    = videoCtx_->height;
    encCtx->pix_fmt   = AV_PIX_FMT_YUV420P;
    encCtx->time_base = av_inv_q(fmtCtx_->streams[videoIdx_]->avg_frame_rate);
    encCtx->framerate = fmtCtx_->streams[videoIdx_]->avg_frame_rate;
    encCtx->gop_size  = 30;

    std::string encName = encoder->name;
    if (encName.find("videotoolbox") != std::string::npos) {
        // VideoToolbox 品質設定（q値: 1=最高, 100=最低）
        av_opt_set_int(encCtx->priv_data, "q",        crfQuality,  AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "profile",  "high",      AV_OPT_SEARCH_CHILDREN);
        av_opt_set    (encCtx->priv_data, "realtime", "0",         AV_OPT_SEARCH_CHILDREN);
        // Apple Mediaエンジンを優先使用
        av_opt_set    (encCtx->priv_data, "allow_sw", "0",         AV_OPT_SEARCH_CHILDREN);
    } else if (encName.find("x264") != std::string::npos || encName.find("x265") != std::string::npos) {
        av_opt_set    (encCtx->priv_data, "preset",   preset.c_str(), 0);
        av_opt_set_int(encCtx->priv_data, "crf",      crfQuality,     AV_OPT_SEARCH_CHILDREN);
    }

    if (outFmt->oformat->flags & AVFMT_GLOBALHEADER)
        encCtx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    int ret = avcodec_open2(encCtx, encoder, nullptr);
    if (ret < 0) {
        VOSE_ERR("エンコーダ初期化失敗: " << avErr(ret));
        avcodec_free_context(&encCtx);
        avformat_free_context(outFmt);
        return false;
    }

    // ── 出力ストリーム（映像）を設定 ──
    AVStream* outVStream = avformat_new_stream(outFmt, encoder);
    avcodec_parameters_from_context(outVStream->codecpar, encCtx);
    outVStream->time_base = encCtx->time_base;

    // ── 音声はストリームコピー（パススルー） ──
    AVStream* outAStream = nullptr;
    if (audioIdx_ >= 0) {
        outAStream = avformat_new_stream(outFmt, nullptr);
        avcodec_parameters_copy(outAStream->codecpar,
                                fmtCtx_->streams[audioIdx_]->codecpar);
        outAStream->codecpar->codec_tag = 0;
        outAStream->time_base = fmtCtx_->streams[audioIdx_]->time_base;
    }

    // ── ファイルオープン & ヘッダ書き込み ──
    if (!(outFmt->oformat->flags & AVFMT_NOFILE)) {
        ret = avio_open(&outFmt->pb, outPath.c_str(), AVIO_FLAG_WRITE);
        if (ret < 0) {
            VOSE_ERR("avio_open 失敗: " << avErr(ret));
            avcodec_free_context(&encCtx);
            avformat_free_context(outFmt);
            return false;
        }
    }
    avformat_write_header(outFmt, nullptr);

    // ── ピクセルフォーマット変換器（デコード結果→YUV420P） ──
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
                          ? findNearestKeyframe(entry.in_point)
                          : entry.in_point;
        seekAndFlush(seekTarget);

        while (av_read_frame(fmtCtx_, inPkt) >= 0) {
            // ── 映像デコード → エンコード ──
            if (inPkt->stream_index == videoIdx_) {
                double pktSec = toSeconds(inPkt->pts, fmtCtx_->streams[videoIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); break; }

                if (avcodec_send_packet(videoCtx_, inPkt) == 0) {
                    while (avcodec_receive_frame(videoCtx_, decFrame) == 0) {
                        // YUV変換
                        sws_scale(encSws,
                                  decFrame->data, decFrame->linesize, 0, decFrame->height,
                                  encFrame->data, encFrame->linesize);
                        encFrame->pts = frameIdx++;

                        if (avcodec_send_frame(encCtx, encFrame) == 0) {
                            while (avcodec_receive_packet(encCtx, outPkt) == 0) {
                                av_packet_rescale_ts(outPkt, encCtx->time_base, outVStream->time_base);
                                outPkt->stream_index = 0;
                                av_interleaved_write_frame(outFmt, outPkt);
                                av_packet_unref(outPkt);
                            }
                        }
                        av_frame_unref(decFrame);
                    }
                }
            }
            // ── 音声パススルー ──
            else if (inPkt->stream_index == audioIdx_ && outAStream) {
                double pktSec = toSeconds(inPkt->pts, fmtCtx_->streams[audioIdx_]->time_base);
                if (pktSec < entry.in_point - 0.002) { av_packet_unref(inPkt); continue; }
                if (pktSec >= entry.out_point)        { av_packet_unref(inPkt); continue; }

                AVPacket* ap = av_packet_clone(inPkt);
                av_packet_rescale_ts(ap,
                    fmtCtx_->streams[audioIdx_]->time_base,
                    outAStream->time_base);
                ap->pts          += audioOff;
                ap->dts          += audioOff;
                ap->pos           = -1;
                ap->stream_index  = 1;
                av_interleaved_write_frame(outFmt, ap);
                av_packet_free(&ap);
            }
            av_packet_unref(inPkt);
        }

        // セグメント末尾でエンコーダをフラッシュ
        flushEncoder();

        // 音声オフセットを更新
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
//  C API 実装 — Phase 3: UX Bridge (ctypes用エクスポート)
// ════════════════════════════════════════════════════════════════════

extern "C" {

void* vose_create() {
    return new vose::VideoEngine();
}
void vose_destroy(void* h) {
    delete static_cast<vose::VideoEngine*>(h);
}
int vose_load(void* h, const char* path) {
    return static_cast<vose::VideoEngine*>(h)->load(path) ? 1 : 0;
}
double vose_duration(void* h) {
    return static_cast<vose::VideoEngine*>(h)->duration();
}
int vose_width(void* h) {
    return static_cast<vose::VideoEngine*>(h)->width();
}
int vose_height(void* h) {
    return static_cast<vose::VideoEngine*>(h)->height();
}
double vose_fps(void* h) {
    return static_cast<vose::VideoEngine*>(h)->fps();
}
int vose_has_audio(void* h) {
    return static_cast<vose::VideoEngine*>(h)->hasAudio() ? 1 : 0;
}
int vose_save_frame(void* h, double time_sec, const char* out_path) {
    return static_cast<vose::VideoEngine*>(h)->saveFrame(time_sec, out_path) ? 1 : 0;
}
int vose_waveform(void* h, float* out_buf, int buf_size, int chunks) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    auto  wd  = eng->extractWaveform(chunks);
    int n = static_cast<int>(std::min(static_cast<size_t>(buf_size), wd.peaks_max.size()));
    for (int i = 0; i < n; i++) out_buf[i] = wd.peaks_max[i];
    return n;
}
int vose_export_edl(void* h, const char* edl_json, const char* out_path) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    vose::EDL edl;
    if (!edl.deserialize(edl_json)) return 0;
    return eng->exportFromEDL(edl, out_path) ? 1 : 0;
}
int vose_export_hw(void* h, const char* edl_json, const char* out_path, int quality) {
    auto* eng = static_cast<vose::VideoEngine*>(h);
    vose::EDL edl;
    if (!edl.deserialize(edl_json)) return 0;
    return eng->exportWithVideoToolbox(edl, out_path, quality) ? 1 : 0;
}
int vose_build_keyframe_index(void* h) {
    return static_cast<int>(
        static_cast<vose::VideoEngine*>(h)->buildKeyframeIndex().size());
}
double vose_nearest_keyframe(void* h, double time_sec) {
    return static_cast<vose::VideoEngine*>(h)->findNearestKeyframe(time_sec);
}

} // extern "C"

} // namespace vose
