"""
Compatibility shim — logic has moved to scoring.linkedin_analyzer.
Import detect_move_signals from there for new code.
"""

from scoring.linkedin_analyzer import detect_move_signals as detect_move_signals  # noqa: F401
