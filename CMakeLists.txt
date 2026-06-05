# SDK内のスタティックライブラリを指定
set(FFMPEG_LIB_DIR ${CMAKE_SOURCE_DIR}/sdk/ffmpeg/lib)

target_link_libraries(vo_se_cut PRIVATE 
    ${FFMPEG_LIB_DIR}/libavformat.a
    ${FFMPEG_LIB_DIR}/libavcodec.a
    ${FFMPEG_LIB_DIR}/libavutil.a
    ${FFMPEG_LIB_DIR}/libswscale.a
    ${FFMPEG_LIB_DIR}/libavfilter.a    # 字幕焼き込み(avfilter)の実体
    ${FFMPEG_LIB_DIR}/libswresample.a # avfilterが内部で依存する音声リサンプラー
    
    # Windowsの場合、追加でシステムライブラリが必要なことが多いです
    # bcrypt, user32, ws2_32 等
)
