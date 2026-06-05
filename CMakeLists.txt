# --- SDKの手動指定をやめ、システムのFFmpeg（Homebrew等）を自動探索 ---
find_package(PkgConfig REQUIRED)

# libavfilter を含む、必要なすべてのモジュールを一括検索（アーキテクチャ不整合を自動回避）
pkg_check_modules(FFMPEG REQUIRED 
    libavformat 
    libavfilter 
    libavcodec 
    libswscale 
    libswresample 
    libavutil
)

# ターゲット（vo_se_cut）へのリンク処理
target_link_libraries(vo_se_cut PRIVATE 
    ${FFMPEG_LIBRARIES}
)

# インクルードパスもシステム側に通す
target_include_directories(vo_se_cut PRIVATE 
    ${FFMPEG_INCLUDE_DIRS}
)
