# modules/talk/__init__.py
from .intonation_analyzer import IntonationAnalyzer
from .talk_manager import TalkManager, generate_talk_events

__all__ = ["IntonationAnalyzer", "TalkManager", "generate_talk_events"]
