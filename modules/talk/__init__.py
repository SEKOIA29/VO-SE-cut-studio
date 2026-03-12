# modules/talk/__init__.py
from __future__ import annotations
from typing import Any, List, Dict, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    # Pyright (CI) 用のモック定義
    class IntonationAnalyzer:
        def __init__(self) -> None: ...
        def analyze(self, text: str) -> str: ...
        def analyze_to_phonemes(self, text: str) -> List[str]: ...
        def analyze_to_accent_phrases(self, text: str) -> Any: ...

    class TalkManager:
        def __init__(self) -> None: ...
        def set_voice(self, path: str) -> bool: ...
        def synthesize(
            self,
            text: str,
            output_path: str,
            speed: float = 1.0
        ) -> Tuple[bool, str]: ...

    def generate_talk_events(
        text: str,
        analyzer: IntonationAnalyzer
    ) -> List[Dict[str, Any]]: ...

else:
    # 実行環境での動的ロード
    try:
        from .vo_se_engine import (
            IntonationAnalyzer,
            TalkManager,
            generate_talk_events
        )
    except (ImportError, AttributeError):
        # フォールバック定義（宣言形式を if ブロックと合わせる）
        class IntonationAnalyzer:
            pass

        class TalkManager:
            pass

        def generate_talk_events(*args: Any, **kwargs: Any) -> list[Any]:
            return []

__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]
