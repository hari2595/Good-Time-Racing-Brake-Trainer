"""
backend_winmm.py - WinMM backend for Pedal Trace (modular)

Provides a thin wrapper around the legacy Windows joystick API (winmm)
so pedals show up as X/Y/Z/Rx/Ry/Rz axes (same layer as joy.cpl).

Contract expected by the UI (pedal_trace_winmm.py):
    AXIS_NAMES: list[str]
    class Backend:
        dev_id: int
        axis_brake: str
        axis_thr:   str
        cfg_b / cfg_t: have fields (invert: bool, deadzone: float, zero_raw: float)
        list_devices() -> list[int]
        apply_device(dev_id: int) -> None
        read_raw(dev_id: int) -> Optional[Tuple[int,int,int,int,int,int]]
        calibrate_zero() -> Tuple[float,float]       # zeros in [0..1]
        set_smoothing(ms: float) -> None
        poll() -> Optional[Tuple[float,float,float,Tuple[int,int,int,int,int,int]]]
            # (t_ms, brake01, throttle01, raw_axes)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, List
import time
import ctypes
from ctypes import wintypes

# ---- WinMM bindings ---------------------------------------------------------
joyGetNumDevs = ctypes.windll.winmm.joyGetNumDevs
joyGetPosEx   = ctypes.windll.winmm.joyGetPosEx

class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('dwXpos', wintypes.DWORD),
        ('dwYpos', wintypes.DWORD),
        ('dwZpos', wintypes.DWORD),
        ('dwRpos', wintypes.DWORD),  # Rx
        ('dwUpos', wintypes.DWORD),  # Ry
        ('dwVpos', wintypes.DWORD),  # Rz
        ('dwButtons', wintypes.DWORD),
        ('dwButtonNumber', wintypes.DWORD),
        ('dwPOV', wintypes.DWORD),
        ('dwReserved1', wintypes.DWORD),
        ('dwReserved2', wintypes.DWORD),
    ]

JOY_RETURNALL = 0xFF
MAX_DEVICES   = 16
AXIS_NAMES: List[str] = ["X", "Y", "Z", "Rx", "Ry", "Rz"]

# ---- Helpers ----------------------------------------------------------------
@dataclass
class NormCfg:
    invert: bool = False
    deadzone: float = 0.02
    zero_raw: float = 0.0  # in [0..1], stored as normalized baseline

class EMA:
    def __init__(self, ms: float = 20.0):
        self.ms = ms
        self._init = False
        self.y = 0.0
        self.t = None
    def reset(self):
        self._init = False
        self.y = 0.0
        self.t = None
    def step(self, x: float, now: float) -> float:
        if not self._init:
            self._init = True
            self.t = now
            self.y = x
            return x
        dt = max(1e-6, now - (self.t or now))
        self.t = now
        if self.ms <= 0:
            self.y = x
            return x
        a = min(1.0, dt / (self.ms / 1000.0))
        self.y = a * x + (1 - a) * self.y
        return self.y

def _raw01_from_uint(v: int) -> float:
    # WinMM nominal range is 0..65535.
    if v < 0: v = 0
    if v > 65535: v = 65535
    return v / 65535.0

def _map_norm(v_uint: int, cfg: NormCfg) -> float:
    x = _raw01_from_uint(v_uint)
    if cfg.invert:
        x = 1.0 - x
    # Apply zero + deadzone in normalized space
    floor = min(0.98, cfg.zero_raw + cfg.deadzone)
    if x <= floor:
        return 0.0
    den = 1.0 - floor
    if den <= 1e-9:
        return 0.0
    return max(0.0, min(1.0, (x - floor) / den))

# ---- Backend ----------------------------------------------------------------
class Backend:
    def __init__(self):
        # Defaults based on the user's probe: device 0, throttle=X, brake=Y
        self.dev_id: int = 0
        self.axis_brake: str = 'X'
        self.axis_thr:   str = 'Y'
        self.cfg_b = NormCfg(invert=False)
        self.cfg_t = NormCfg(invert=False)
        self._ema_b = EMA(20.0)
        self._ema_t = EMA(20.0)
        self._t0: Optional[float] = None

    # -- device enumeration --------------------------------------------------
    def list_devices(self) -> List[int]:
        n = int(joyGetNumDevs())
        found: List[int] = []
        for i in range(min(n, MAX_DEVICES)):
            j = JOYINFOEX(); j.dwSize = ctypes.sizeof(JOYINFOEX); j.dwFlags = JOY_RETURNALL
            r = joyGetPosEx(i, ctypes.byref(j))
            if r == 0:
                found.append(i)
        return found

    def apply_device(self, dev_id: int) -> None:
        self.dev_id = int(dev_id)
        # Reset clocks/filters on device change
        self._t0 = None
        self._ema_b.reset(); self._ema_t.reset()

    # -- raw reads -----------------------------------------------------------
    def read_raw(self, dev_id: int) -> Optional[Tuple[int,int,int,int,int,int]]:
        j = JOYINFOEX(); j.dwSize = ctypes.sizeof(JOYINFOEX); j.dwFlags = JOY_RETURNALL
        r = joyGetPosEx(int(dev_id), ctypes.byref(j))
        if r != 0:
            return None
        return (j.dwXpos, j.dwYpos, j.dwZpos, j.dwRpos, j.dwUpos, j.dwVpos)

    # -- calibration ---------------------------------------------------------
    def calibrate_zero(self) -> Tuple[float, float]:
        vals = self.read_raw(self.dev_id)
        if not vals:
            return (0.0, 0.0)
        bi = AXIS_NAMES.index(self.axis_brake)
        ti = AXIS_NAMES.index(self.axis_thr)
        self.cfg_b.zero_raw = _raw01_from_uint(vals[bi])
        self.cfg_t.zero_raw = _raw01_from_uint(vals[ti])
        return (self.cfg_b.zero_raw, self.cfg_t.zero_raw)

    # -- smoothing -----------------------------------------------------------
    def set_smoothing(self, ms: float) -> None:
        ms = float(ms)
        self._ema_b.ms = ms
        self._ema_t.ms = ms

    # -- poll loop -----------------------------------------------------------
    def poll(self) -> Optional[Tuple[float,float,float,Tuple[int,int,int,int,int,int]]]:
        vals = self.read_raw(self.dev_id)
        if not vals:
            return None
        bi = AXIS_NAMES.index(self.axis_brake)
        ti = AXIS_NAMES.index(self.axis_thr)
        b = _map_norm(vals[bi], self.cfg_b)
        t = _map_norm(vals[ti], self.cfg_t)
        now = time.perf_counter()
        if self._t0 is None:
            self._t0 = now
        b = self._ema_b.step(b, now)
        t = self._ema_t.step(t, now)
        t_ms = (now - self._t0) * 1000.0
        return (t_ms, b, t, vals)
