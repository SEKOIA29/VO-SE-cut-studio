# SDK内のスタティックライブラリを指定
set(FFMPEG_LIB_DIR ${CMAKE_SOURCE_DIR}/sdk/ffmpeg/lib)

target_link_libraries(vo_se_cut PRIVATE 
    # --- macOS (Apple Silicon) 向けの厳格な強制シンボルロード指定 ---
    # CMake 3.13以降で最も安全に `-force_load` をリンカに透過させる構文です
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libavfilter.a"
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libavformat.a"
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libavcodec.a"
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libswscale.a"
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libswresample.a"
    "LINKER:-force_load,${FFMPEG_LIB_DIR}/libavutil.a"

    # --- macOS システムフレームワークの明示的リンク ---
    "-framework CoreMedia"
    "-framework CoreVideo"
    "-framework VideoToolbox"
    "-framework AudioToolbox"
    "-framework CoreFoundation"
    "-framework Security"
    iconv
    z
)
