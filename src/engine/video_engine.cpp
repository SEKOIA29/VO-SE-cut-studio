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

class VideoEngine {
private:
    AVFormatContext* formatContext = nullptr;
    AVCodecContext* codecContext = nullptr;
    int videoStreamIndex = -1;

public:
    VideoEngine() {
        // コンストラクタでの初期化処理
        std::cout << "[VideoEngine] Instance created." << std::endl;
    }

    // デストラクタ: C言語ライブラリ特有のメモリ解放を確実に行う
    ~VideoEngine() {
        releaseResources();
        std::cout << "[VideoEngine] Resources released safely." << std::endl;
    }

    /**
     * 動画ファイルを読み込み、解析準備を完了させる
     * @param filepath 動画ファイルのパス (例: "test.mp4")
     * @return 成功なら true
     */
    bool loadVideo(const std::string& filepath) {
        std::cout << "-> Opening media file: " << filepath << std::endl;

        // 1. コンテナ（ファイル）を開く
        if (avformat_open_input(&formatContext, filepath.c_str(), nullptr, nullptr) != 0) {
            std::cerr << "[Error] Could not open file: " << filepath << std::endl;
            return false;
        }

        // 2. ストリーム情報（映像・音声など）を取得する
        if (avformat_find_stream_info(formatContext, nullptr) < 0) {
            std::cerr << "[Error] Could not retrieve stream info." << std::endl;
            return false;
        }

        // 3. 最適な映像ストリーム（ビデオトラック）を探す
        const AVCodec* decoder = nullptr;
        videoStreamIndex = av_find_best_stream(formatContext, AVMEDIA_TYPE_VIDEO, -1, -1, &decoder, 0);
        
        if (videoStreamIndex < 0 || !decoder) {
            std::cerr << "[Error] No valid video stream or decoder found." << std::endl;
            return false;
        }

        // 4. デコーダー用のコンテキストを割り当て、パラメータをコピー
        codecContext = avcodec_alloc_context3(decoder);
        if (!codecContext) {
            std::cerr << "[Error] Failed to allocate codec context." << std::endl;
            return false;
        }

        avcodec_parameters_to_context(codecContext, formatContext->streams[videoStreamIndex]->codecpar);

        // 5. デコーダーをオープンする
        if (avcodec_open2(codecContext, decoder, nullptr) < 0) {
            std::cerr << "[Error] Failed to open codec." << std::endl;
            return false;
        }

        // 解析成功: 情報の出力 (Appleライクなミニマルな出力)
        std::cout << "   [Success] Video stream ready." << std::endl;
        std::cout << "   - Codec: " << decoder->name << " (" << decoder->long_name << ")" << std::endl;
        std::cout << "   - Resolution: " << codecContext->width << " x " << codecContext->height << std::endl;
        
        // フレームレートの計算
        AVRational framerate = formatContext->streams[videoStreamIndex]->avg_frame_rate;
        if (framerate.den > 0) {
            double fps = static_cast<double>(framerate.num) / framerate.den;
            std::cout << "   - Framerate: " << fps << " fps" << std::endl;
        }

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
 * メイン関数 (エントリーポイント)
 */
int main() {
    std::cout << "=============================================" << std::endl;
    std::cout << "  VO-SE Cut Studio: Video Engine v1.0.0" << std::endl;
    std::cout << "  Powered by aural core technologies" << std::endl;
    std::cout << "=============================================" << std::endl;
    std::cout << "FFmpeg Version: " << av_version_info() << "\n" << std::endl;

    {
        // VideoEngineのスコープ。ここを抜けると自動でデストラクタが呼ばれる
        vose::VideoEngine engine;
        
        // TODO: ここに実際のmp4ファイルのパスを渡してテストします
        // engine.loadVideo("sample_video.mp4");
    }

    std::cout << "\nEngine shutdown sequence completed." << std::endl;
    return 0;
}

class VideoInspector {
public:
    static void inspect(const std::string& filename) {
        AVFormatContext* fmt_ctx = nullptr;

        // 1. ファイルを開く
        if (avformat_open_input(&fmt_ctx, filename.c_str(), nullptr, nullptr) < 0) {
            std::cerr << "[Error] Could not open file: " << filename << std::endl;
            return;
        }

        // 2. ストリーム情報を取得
        if (avformat_find_stream_info(fmt_ctx, nullptr) < 0) {
            std::cerr << "[Error] Could not find stream information." << std::endl;
            avformat_close_input(&fmt_ctx);
            return;
        }

        // 3. 基本情報の出力 (Appleスタイルのクリーンな出力)
        std::cout << "\n--- Media Inspection: " << filename << " ---" << std::endl;
        
        // 再生時間 (duration) は微妙な計算が必要
        if (fmt_ctx->duration != AV_NOPTS_VALUE) {
            double secs = fmt_ctx->duration / (double)AV_TIME_BASE;
            std::cout << "Duration   : " << std::fixed << std::setprecision(2) << secs << " seconds" << std::endl;
        }

        std::cout << "Format     : " << fmt_ctx->iformat->long_name << std::endl;
        std::cout << "Streams    : " << fmt_ctx->nb_streams << std::endl;

        // 4. 各ストリーム（映像・音声）の詳細
        for (unsigned int i = 0; i < fmt_ctx->nb_streams; i++) {
            AVStream* stream = fmt_ctx->streams[i];
            AVCodecParameters* codec_par = stream->codecpar;

            std::cout << "\n[Stream #" << i << "]" << std::endl;
            std::cout << "  Type     : " << av_get_media_type_string(codec_par->codec_type) << std::endl;
            
            if (codec_par->codec_type == AVMEDIA_TYPE_VIDEO) {
                std::cout << "  Codec    : " << avcodec_get_name(codec_par->codec_id) << std::endl;
                std::cout << "  Res      : " << codec_par->width << "x" << codec_par->height << std::endl;
            } else if (codec_par->codec_type == AVMEDIA_TYPE_AUDIO) {
                std::cout << "  Codec    : " << avcodec_get_name(codec_par->codec_id) << std::endl;
                std::cout << "  Channels : " << codec_par->ch_layout.nb_channels << std::endl;
                std::cout << "  SampleRt : " << codec_par->sample_rate << " Hz" << std::endl;
            }
        }

        std::cout << "-------------------------------------------\n" << std::endl;

        avformat_close_input(&fmt_ctx);
    }
};

} // namespace vose

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cout << "Usage: ./video_engine <video_path>" << std::endl;
        return 1;
    }

    vose::VideoInspector::inspect(argv[1]);
    return 0;
}
