# SDK内のスタティックライブラリを指定
set(FFMPEG_LIB_DIR ${CMAKE_SOURCE_DIR}/sdk/ffmpeg/lib)

target_link_libraries(vo_se_cut PRIVATE 
    ${FFMPEG_LIB_DIR}/libavformat.a
    ${FFMPEG_LIB_DIR}/libavfilter.a    # 👈 まず avfilter を上に配置（avformat や avcodec を使用するため）
    ${FFMPEG_LIB_DIR}/libavcodec.a
    ${FFMPEG_LIB_DIR}/libswscale.a
    ${FFMPEG_LIB_DIR}/libswresample.a  # 👈 avfilter が内部で依存するリサンプラー
    ${FFMPEG_LIB_DIR}/libavutil.a       # 👈 すべての基礎となる最下層ライブラリは一番下に配置
    
    # macOS の場合、スタティックリンク時に以下のコアフレームワークが必要になる場合があります
    "-framework CoreMedia"
    "-framework CoreVideo"
    "-framework VideoToolbox"
)
