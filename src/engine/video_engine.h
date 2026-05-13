#include "video_engine.h"
#include <iostream>

bool VideoDecoder::open_file(const char* filename) {
    // 1. ファイルを開く
    if (avformat_open_input(&fmt_ctx, filename, nullptr, nullptr) < 0) {
        return false;
    }

    // 2. ストリーム情報を取得
    if (avformat_find_stream_info(fmt_ctx, nullptr) < 0) {
        return false;
    }

    // ここでビデオストリームのインデックスを探し、デコーダを準備する処理が続きます
    return true;
}

void VideoDecoder::decode_frame(int frame_index, uint8_t* out_buffer) {
    // 1. 指定のフレームまでシーク (av_seek_frame)
    // 2. パケットを読み込み (av_read_frame)
    // 3. デコーダに送って (avcodec_send_packet)
    // 4. フレームを取り出す (avcodec_receive_frame)
    // 5. sws_scale で RGB24 等に変換して out_buffer に書き込む
}
