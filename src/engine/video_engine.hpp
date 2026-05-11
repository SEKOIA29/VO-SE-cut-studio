#ifndef VOSE_VIDEO_ENGINE_HPP
#define VOSE_VIDEO_ENGINE_HPP

#include <string>
#include <vector>
#include <optional>
#include <functional>

// FFmpeg forward declarations
struct AVFormatContext;
struct AVCodecContext;
struct SwsContext;
struct AVRational;

enum AVPixelFormat : int;

namespace vose {

/**
 * フレーム情報構造体
 */
struct FrameInfo {
    int width;
    int height;
    double pts_seconds;
    bool is_keyframe;
    std::vector<uint8_t> rgb_data; // Packed RGB24
};

/**
 * 音声波形データ構造体
 */
struct WaveformData {
    int sample_rate;
    int channels;
    double duration_sec;
    int chunks;
    std::vector<float> peaks_max;
    std::vector<float> peaks_min;
    std::vector<float> rms;
};

/**
 * キーフレームインデックス
 */
struct KeyframeIndex {
    int64_t pts_raw;
    int64_t dts_raw;
    double pts_seconds;
    int64_t file_pos;
};

/**
 * 字幕エントリ（VO-SE連携用）
 */
struct SubtitleEntry {
    double start_sec;
    double end_sec;
    std::string text;
    float x, y;
    int font_size;
    std::string style;
};
using SubtitleTrack = std::vector<SubtitleEntry>;

/**
 * EDL (Edit Decision List) エントリ
 */
struct EDLEntry {
    double in_point;
    double out_point;
    bool enabled = true;
    double duration() const { return out_point - in_point; }
};

class EDL {
public:
    std::vector<EDLEntry> entries;
    bool deserialize(const std::string& json_str) {
        // 実装は video_engine.cpp 側
        return true; 
    }
    std::vector<EDLEntry> getEnabledEntries() const {
        std::vector<EDLEntry> enabled;
        for (const auto& e : entries) if (e.enabled) enabled.push_back(e);
        return enabled;
    }
};

/**
 * VO-SE Video Engine Core
 */
class VideoEngine {
public:
    VideoEngine();
    ~VideoEngine();

    // 基本操作
    bool load(const std::string& filepath);
    void releaseResources();

    // アクセサ
    int width() const;
    int height() const;
    double fps() const;
    double duration() const;
    std::string codecName() const;
    bool hasAudio() const { return audioIdx_ >= 0; }
    bool isLoaded() const { return loaded_; }

    // Phase 1 & 2: 抽出・インデックス
    std::optional<FrameInfo> extractFrame(double timeSec);
    bool saveFrame(double timeSec, const std::string& outPath);
    WaveformData extractWaveform(int chunks = 1000);
    std::vector<KeyframeIndex> buildKeyframeIndex();
    double findNearestKeyframe(double timeSec) const;

    // Phase 2 & 5: エクスポート
    bool exportFromEDL(const EDL& edl, const std::string& outPath);
    bool exportWithVideoToolbox(const EDL& edl, const std::string& outPath, int crfQuality = 20, const std::string& preset = "medium");

    // Phase 4: 字幕
    SubtitleTrack subtitleTrackFromVOSE(const std::string& voseJsonPath);
    bool exportWithSubtitles(const EDL& edl, const SubtitleTrack& subs, const std::string& outPath);

    // プログレスコールバック
    void setProgressCallback(std::function<void(double, std::string)> cb) { progressCb_ = cb; }

private:
    std::string filePath_;
    bool loaded_ = false;

    // FFmpeg コンテキスト
    AVFormatContext* fmtCtx_ = nullptr;
    AVCodecContext* videoCtx_ = nullptr;
    AVCodecContext* audioCtx_ = nullptr;
    SwsContext* swsCtx_ = nullptr;

    int videoIdx_ = -1;
    int audioIdx_ = -1;

    std::vector<KeyframeIndex> keyframeIdx_;
    std::function<void(double, std::string)> progressCb_;

    // 内部ユーティリティ
    bool seekAndFlush(double timeSec);
    SwsContext* makeSwsCtx(int w, int h, AVPixelFormat srcFmt); // srcFmtはAVPixelFormatだが前方宣言のためint
    double toSeconds(int64_t pts, AVRational tb) const;
    void reportProgress(double p, const std::string& stage);
};

} // namespace vose

#endif // VOSE_VIDEO_ENGINE_HPP
