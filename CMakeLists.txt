find_package(FFmpeg REQUIRED COMPONENTS avformat avcodec swscale avutil)

add_library(vo_se_cut SHARED src/engine/video_engine.cpp)
target_include_directories(vo_se_cut PRIVATE ${FFMPEG_INCLUDE_DIRS})
target_link_libraries(vo_se_cut PRIVATE ${FFMPEG_LIBRARIES})
