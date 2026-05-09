# SDK内のスタティックライブラリを指定
set(FFMPEG_LIB_DIR ${CMAKE_SOURCE_DIR}/sdk/ffmpeg/lib)

target_link_libraries(vo_se_cut PRIVATE 
    ${FFMPEG_LIB_DIR}/libavformat.a
    ${FFMPEG_LIB_DIR}/libavcodec.a
    ${FFMPEG_LIB_DIR}/libavutil.a
    ${FFMPEG_LIB_DIR}/libswscale.a
    # Windowsの場合、追加でシステムライブラリが必要なことが多いです
    # bcrypt, user32, ws2_32 等
)
