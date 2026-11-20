"""Tastatur fuer das Overlay abfangen, BEVOR Steam, Windows-Eingabe oder DirectInput sie sehen.

Hat ein Feld im Overlay den Fokus und ist Empire im Vordergrund, haengt das Steuerpult einen Low-Level-
Tastaturhook ein (eigener Faden mit Nachrichtenschleife) und verschluckt die Tasten: so oeffnet z. B. Umschalt+Tab
beim Tippen nicht das Steam-Overlay, und weder Spiel noch Steam sehen die Eingabe.

Warum im Steuerpult und nicht in der DLL: Hat ein Prozess eine DirectInput-Tastatur erfasst, ruft Windows die
Low-Level-Hooks DIESES Prozesses nicht mehr auf (im Test nachgewiesen) - ein Hook im Spiel selbst waere unzuverlaessig.

Durch laufen nur F8 (Overlay auf/zu), Umschalt/Strg/Alt/Win, Feststell/Num und Alt+Tab/Alt+F4/Alt+Esc.
Der Hook ist nur eingehaengt, solange want=True ist; Ereignisse gehen als ("key", vk, mods) / ("char", code, 0)
an on_event (aus dem Hook-Faden - der Empfaenger reicht sie an den Tk-Faden weiter).
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
VK_TAB, VK_ESCAPE, VK_F4, VK_F8 = 0x09, 0x1B, 0x73, 0x77
VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL = 0x10, 0x11, 0x12, 0x14
VK_LSHIFT, VK_LCONTROL, VK_RMENU = 0xA0, 0xA2, 0xA5
PASS = {VK_F8, VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, 0x90, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
QS_ALLINPUT = 0x04FF
PM_REMOVE = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetKeyState.restype = ctypes.c_short
    user32.GetKeyboardLayout.restype = wintypes.HKL
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_char_p, wintypes.LPWSTR, ctypes.c_int,
                                   wintypes.UINT, wintypes.HKL]
    user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                                 wintypes.DWORD]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    wintypes.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]


class KeyboardHook:
    """Low-Level-Tastaturhook fuer das Overlay (nur aktiv, solange want=True und das Zielprogramm vorne ist)."""

    def __init__(self, on_event, targets=("empire.exe",)) -> None:
        self.on_event = on_event
        self.targets = {t.lower() for t in targets}
        self.want = False
        self.active = False
        self.error: str | None = None
        self._hook = None
        self._names: dict[int, str] = {}
        self._proc = HOOKPROC(self._callback)       # Referenz halten (sonst raeumt Python den Rueckruf weg)
        self._thread = threading.Thread(target=self._run, name="overlay-kbd", daemon=True)
        self._thread.start()

    # --- Zielprogramm im Vordergrund?
    def _target(self) -> tuple[bool, int]:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False, 0
        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = self._names.get(pid.value)
        if name is None:
            name = ""
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if h:
                buf = ctypes.create_unicode_buffer(520)
                n = wintypes.DWORD(520)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    name = os.path.basename(buf.value).lower()
                kernel32.CloseHandle(h)
            self._names[pid.value] = name
        return name in self.targets, tid

    def _callback(self, code, wparam, lparam):
        try:
            if code == HC_ACTION and self.want:
                ok, tid = self._target()
                if ok:
                    k = KBDLLHOOKSTRUCT.from_address(lparam)
                    vk = k.vkCode
                    down = lambda v: bool(user32.GetAsyncKeyState(v) & 0x8000)  # noqa: E731
                    ctrl, alt, shift = down(VK_CONTROL), down(VK_MENU), down(VK_SHIFT)
                    if vk in PASS or (alt and not ctrl and vk in (VK_TAB, VK_F4, VK_ESCAPE)):
                        return user32.CallNextHookEx(None, code, wparam, lparam)
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self.on_event("key", vk, (1 if ctrl and not alt else 0) | (2 if shift else 0))
                        if not ctrl or alt:                 # Strg+Taste = Kuerzel; AltGr = Strg+Alt -> Zeichen
                            ks = (ctypes.c_ubyte * 256)()
                            if shift:
                                ks[VK_SHIFT] = ks[VK_LSHIFT] = 0x80
                            if ctrl:
                                ks[VK_CONTROL] = ks[VK_LCONTROL] = 0x80
                            if alt:
                                ks[VK_MENU] = ks[VK_RMENU] = 0x80
                            if user32.GetKeyState(VK_CAPITAL) & 1:
                                ks[VK_CAPITAL] = 0x01
                            buf = ctypes.create_unicode_buffer(8)
                            n = user32.ToUnicodeEx(vk, k.scanCode, ctypes.cast(ks, ctypes.c_char_p), buf, 8, 4,
                                                   user32.GetKeyboardLayout(tid))
                            if n < 0:
                                n = 1                       # Tottaste (^, ´): das Zeichen selbst
                            for ch in buf[:n]:
                                if ch >= " " and ch != "\x7f":
                                    self.on_event("char", ord(ch), 0)
                    return 1                                # verschluckt: Spiel und Steam sehen nichts
        except Exception as e:  # Hook darf nie werfen
            self.error = str(e)
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _run(self) -> None:
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)       # Nachrichtenschlange anlegen
        while True:
            if self.want and not self._hook:
                self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
                if not self._hook:
                    self.error = f"SetWindowsHookEx fehlgeschlagen ({ctypes.get_last_error()})"
            elif not self.want and self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
            self.active = bool(self._hook)
            user32.MsgWaitForMultipleObjects(0, None, False, 30, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.DispatchMessageW(ctypes.byref(msg))
