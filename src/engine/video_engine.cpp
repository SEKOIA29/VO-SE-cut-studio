#include <iostream>

// FFmpegのヘッダーを読み込む
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
}

/**
 * VO-SE Video Engine - 初期化テスト
 */
int main() {
    std::cout << "--- VO-SE cut studio: Video Engine Booting ---" << std::endl;

    // ライブラリのバージョン情報を出力（SDKが正しくリンクされている証拠）
    std::cout << "FFmpeg Version: " << av_version_info() << std::endl;
    std::cout << "License: " << avutil_license() << std::endl;

    std::cout << "---------------------------------------------" << std::endl;
    std::cout << "Engine status: READY." << std::endl;

    return 0;
}
