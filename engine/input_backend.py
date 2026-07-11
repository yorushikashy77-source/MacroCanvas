"""Compatibility exports for the split Windows and Interception backends."""

from engine.interception import InterceptionInputHook, InterceptionOutput
from engine.win_input import POINT, WinInput

__all__ = ["POINT", "WinInput", "InterceptionInputHook", "InterceptionOutput"]
