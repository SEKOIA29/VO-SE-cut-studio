// video_engine.h
#pragma once

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libswscale/swscale.h>
#include <libavutil/imgutils.h>
}

class VideoDecoder {
public:
    VideoDecoder() : fmt_ctx(nullptr) {}
    bool open_file(const char* filename);
    // 特定のフレームをRGBデータとして抽出する関数など
    void decode_frame(int frame_index, uint8_t* out_buffer);

private:
    AVFormatContext* fmt_ctx;
};
