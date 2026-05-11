#include <iostream>
#include <string>
#include <memory>
#include <iomanip>

// FFmpeg C-APIのインクルード
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/timestamp.h>
}

namespace vose {

/**
 * 動画の解析・デコードを司るコアエンジン
 */
class VideoEngine {
private:
    AVFormatContext* formatContext = nullptr;
    AVCodecContext* codecContext = nullptr;
    int videoStreamIndex = -1;

public:
    VideoEngine() {
        std::cout << "[VideoEngine] Initializing aural core..." << std::endl;
    }

    ~VideoEngine() {
        releaseResources();
        std::cout << "[VideoEngine] resources released safely." << std::endl;
    }

    /**
     * 指定された動画ファイルを詳細にインスペクト（解析）する
     */
    bool inspect(const std::string& filepath) {
        std::cout << "\n--- Media Inspection: " << filepath << " ---" << std::endl;

        // 1. コンテナを開く
        if (avformat_open_input(&formatContext, filepath.c_str(), nullptr, nullptr) != 0) {
            std::cerr << "[Error] Could not open file: " << filepath << std::endl;
            return false;
        }

        // 2. ストリーム情報を取得
        if (avformat_find_stream_info(formatContext, nullptr) < 0) {
            std::cerr << "[Error] Could not retrieve stream info." << std::endl;
            return false;
        }

        // 3. 基本メタデータの出力
        if (formatContext->duration != AV_NOPTS_VALUE) {
            double secs = formatContext->duration / (double)AV_TIME_BASE;
            std::cout << "Duration   : " << std::fixed << std::setprecision(2) << secs << " seconds" << std::endl;
        }
        std::cout << "Format     : " << formatContext->iformat->long_name << std::endl;
        std::cout << "Streams    : " << formatContext->nb_streams << std::endl;

        // 4. 各ストリーム（映像・音声）の詳細を列挙
        for (unsigned int i = 0; i < formatContext->nb_streams; i++) {
            AVStream* stream = formatContext->streams[i];
            AVCodecParameters* codec_par = stream->codecpar;

            std::cout << "\n[Stream #" << i << "]" << std::endl;
            std::cout << "  Type     : " << av_get_media_type_string(codec_par->codec_type) << std::endl;
            std::cout << "  Codec    : " << avcodec_get_name(codec_par->codec_id) << std::endl;
            
            if (codec_par->codec_type == AVMEDIA_TYPE_VIDEO) {
                std::cout << "  Res      : " << codec_par->width << "x" << codec_par->height << std::endl;
                AVRational framerate = stream->avg_frame_rate;
                if (framerate.den > 0) {
                    std::cout << "  Framerate: " << static_cast<double>(framerate.num) / framerate.den << " fps" << std::endl;
                }
            } else if (codec_par->codec_type == AVMEDIA_TYPE_AUDIO) {
                std::cout << "  Channels : " << codec_par->ch_layout.nb_channels << std::endl;
                std::cout << "  SampleRt : " << codec_par->sample_rate << " Hz" << std::endl;
            }
        }
        std::cout << "-------------------------------------------\n" << std::endl;
        return true;
    }

private:
    void releaseResources() {
        if (codecContext) {
            avcodec_free_context(&codecContext);
            codecContext = nullptr;
        }
        if (formatContext) {
            avformat_close_input(&formatContext);
            formatContext = nullptr;
        }
    }
};

} // namespace vose

/**
 * メインエントリーポイント
 */
int main(int argc, char* argv[]) {
    std::cout << "=============================================" << std::endl;
    std::cout << "  VO-SE Cut Studio: Video Engine v1.0.0" << std::endl;
    std::cout << "  Powered by aural core technologies" << std::endl;
    std::cout << "=============================================" << std::endl;
    std::cout << "FFmpeg Version: " << av_version_info() << "\n" << std::endl;

    // 引数がない場合は使い方を表示
    if (argc < 2) {
        std::cout << "Usage: ./video_engine <video_path>" << std::endl;
        return 0;
    }

    // エンジンの実体化と実行
    vose::VideoEngine engine;
    if (!engine.inspect(argv[1])) {
        return 1;
    }

    std::cout << "Engine shutdown sequence completed." << std::endl;
    return 0;
}
