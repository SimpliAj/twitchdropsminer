"""Hide a running macOS application (NSRunningApplication.hide) without any extra dependency."""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any


def _send(objc: Any, receiver: int, selector: bytes, restype: Any, argtypes: list[Any], *args: Any) -> Any:
    func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes))
    return func(receiver, objc.sel_registerName(selector), *args)


def hide_pid(pid: int) -> bool:
    """True if the application owning ``pid`` is known to AppKit and was asked to hide."""
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    cls = objc.objc_getClass(b"NSRunningApplication")
    app = _send(objc, cls, b"runningApplicationWithProcessIdentifier:", ctypes.c_void_p, [ctypes.c_int], pid)
    if not app:
        return False
    return bool(_send(objc, app, b"hide", ctypes.c_bool, []))
