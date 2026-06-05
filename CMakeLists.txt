# SDK内のスタティックライブラリを指定
set(FFMPEG_LIB_DIR ${CMAKE_SOURCE_DIR}/sdk/ffmpeg/lib)

target_link_libraries(vo_se_cut PRIVATE 
    # --- macOS (Apple Silicon) 向けのスタティックライブラリ強制ロード指定 ---
    # シンボルが間引きされるのを防ぐため、明示的にリンカフラグで囲みます
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libavfilter.a
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libavformat.a
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libavcodec.a
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libswscale.a
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libswresample.a
    -Wl,-force_load ${FFMPEG_LIB_DIR}/libavutil.a

    # --- macOSのコアシステムフレームワークの明示的リンク ---
    # FFmpegが内部で要求するハードウェアアクセラレーション等の依存解決
    "-framework CoreMedia"
    "-framework CoreVideo"
    "-framework VideoToolbox"
    "-framework AudioToolbox"
    "-framework CoreFoundation"
    iconv
    z
)
