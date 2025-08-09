# Pedal Trace — Windows (WinMM Game Controller)
# --------------------------------------------
# Reads pedals via the same Windows joystick layer used by joy.cpl (WinMM),
# so axes appear as X / Y / Z / Rx / Ry / Rz — no SDL/pygame, no HID.
#
# Quick start (Windows):
#   py -3 -m venv .venv
#   .venv\Scripts\activate
#   python pedal_trace_winmm.py
#
# Defaults chosen from your probe:
#   • Device ID = 0
#   • Throttle = X
#   • Brake    = Y
# You can change these from the UI if needed.

from __future__ import annotations
import time, csv
from dataclasses import dataclass
from collections import deque
from typing import Optional, Tuple

import ctypes
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----- WinMM bindings -----
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
AXIS_NAMES = ["X", "Y", "Z", "Rx", "Ry", "Rz"]

# ----- Helpers -----
@dataclass
class NormCfg:
    invert: bool = False
    deadzone: float = 0.02
    zero_raw: float = 0.0

class EMA:
    def __init__(self, ms: float = 20.0):
        self.ms = ms; self._init=False; self.y=0.0; self.t=None
    def reset(self):
        self._init=False; self.y=0.0; self.t=None
    def step(self, x: float, now: float) -> float:
        if not self._init:
            self._init=True; self.t=now; self.y=x; return x
        dt=max(1e-6, now-(self.t or now)); self.t=now
        if self.ms<=0: self.y=x; return x
        a=min(1.0, dt/(self.ms/1000.0))
        self.y=a*x+(1-a)*self.y
        return self.y

def raw01_from_uint(v: int) -> float:
    # WinMM range is 0..65535 (commonly). Safeguard bounds.
    v = 0 if v < 0 else 65535 if v > 65535 else v
    return v/65535.0

def map_norm(v_uint: int, cfg: NormCfg) -> float:
    x = raw01_from_uint(v_uint)
    if cfg.invert:
        x = 1.0 - x
    z = cfg.zero_raw
    floor = min(0.98, z + cfg.deadzone)
    if x <= floor:
        return 0.0
    den = 1.0 - floor
    return max(0.0, min(1.0, (x - floor)/den if den>1e-9 else 0.0))

# ----- Backend -----
class WinMMBackend:
    def __init__(self):
        self.dev_id = 0  # default from your probe
        self.axis_brake = 'Y'
        self.axis_thr   = 'X'
        self.cfg_b = NormCfg(invert=False)
        self.cfg_t = NormCfg(invert=False)
        self.ema_b = EMA(20.0)
        self.ema_t = EMA(20.0)
        self.t0 = None

    @staticmethod
    def list_devices():
        n = joyGetNumDevs()
        found = []
        for i in range(min(n, MAX_DEVICES)):
            j = JOYINFOEX(); j.dwSize=ctypes.sizeof(JOYINFOEX); j.dwFlags=JOY_RETURNALL
            r = joyGetPosEx(i, ctypes.byref(j))
            if r == 0:
                found.append(i)
        return found

    def _read(self, dev_id: int) -> Optional[Tuple[int,int,int,int,int,int]]:
        j = JOYINFOEX(); j.dwSize=ctypes.sizeof(JOYINFOEX); j.dwFlags=JOY_RETURNALL
        r = joyGetPosEx(dev_id, ctypes.byref(j))
        if r != 0:
            return None
        return (j.dwXpos, j.dwYpos, j.dwZpos, j.dwRpos, j.dwUpos, j.dwVpos)

    def calibrate_zero(self):
        vals = self._read(self.dev_id)
        if not vals: return (0.0,0.0)
        ax_idx = AXIS_NAMES.index(self.axis_brake)
        at_idx = AXIS_NAMES.index(self.axis_thr)
        self.cfg_b.zero_raw = raw01_from_uint(vals[ax_idx])
        self.cfg_t.zero_raw = raw01_from_uint(vals[at_idx])
        return (self.cfg_b.zero_raw, self.cfg_t.zero_raw)

    def poll(self):
        vals = self._read(self.dev_id)
        if not vals: return None
        ax_idx = AXIS_NAMES.index(self.axis_brake)
        at_idx = AXIS_NAMES.index(self.axis_thr)
        b = map_norm(vals[ax_idx], self.cfg_b)
        t = map_norm(vals[at_idx], self.cfg_t)
        now = time.perf_counter()
        if self.t0 is None: self.t0 = now
        b = self.ema_b.step(b, now)
        t = self.ema_t.step(t, now)
        return ( (now-self.t0)*1000.0, b, t, vals )

# ----- UI -----
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Pedal Trace — WinMM')
        self.geometry('1040x740')
        self.backend = WinMMBackend()
        self.window_seconds = tk.DoubleVar(value=8.0)
        self.buffer = deque(maxlen=120*60*5)
        self._build_ui(); self.refresh()

    def _build_ui(self):
        top = ttk.Frame(self); top.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(top, text='Device ID:').grid(row=0,column=0,sticky='w')
        self.dev_combo = ttk.Combobox(top, state='readonly', width=20); self.dev_combo.grid(row=0,column=1,sticky='w')
        ttk.Button(top, text='Refresh', command=self.refresh).grid(row=0,column=2,padx=6)
        ttk.Button(top, text='Open', command=self.apply_device).grid(row=0,column=3,padx=6)

        ttk.Label(top, text='Brake axis').grid(row=1,column=0,sticky='w')
        self.br_combo = ttk.Combobox(top, state='readonly', width=10, values=AXIS_NAMES); self.br_combo.set('Y'); self.br_combo.grid(row=1,column=1,sticky='w')
        self.br_inv = tk.BooleanVar(value=False); ttk.Checkbutton(top, text='Invert', variable=self.br_inv, command=self.apply_axes).grid(row=1,column=2,sticky='w')

        ttk.Label(top, text='Throttle axis').grid(row=2,column=0,sticky='w')
        self.th_combo = ttk.Combobox(top, state='readonly', width=10, values=AXIS_NAMES); self.th_combo.set('X'); self.th_combo.grid(row=2,column=1,sticky='w')
        self.th_inv = tk.BooleanVar(value=False); ttk.Checkbutton(top, text='Invert', variable=self.th_inv, command=self.apply_axes).grid(row=2,column=2,sticky='w')

        ttk.Label(top, text='Deadzone').grid(row=3,column=0,sticky='w')
        self.dz = tk.DoubleVar(value=0.02)
        ttk.Spinbox(top, from_=0.0, to=0.2, increment=0.005, textvariable=self.dz, width=7, command=self.apply_axes).grid(row=3,column=1,sticky='w')
        ttk.Label(top, text='Smoothing (ms)').grid(row=3,column=2,sticky='w')
        self.sm = tk.IntVar(value=20)
        ttk.Spinbox(top, from_=0, to=200, increment=5, textvariable=self.sm, width=7, command=self.apply_axes).grid(row=3,column=3,sticky='w')
        ttk.Label(top, text='Window (s)').grid(row=3,column=4,sticky='w')
        self.win = tk.DoubleVar(value=8.0)
        ttk.Spinbox(top, from_=2, to=30, increment=1, textvariable=self.win, width=7).grid(row=3,column=5,sticky='w')

        ttk.Button(top, text='Calibrate zero', command=self.calibrate).grid(row=4,column=0,pady=6,sticky='w')
        self.btn_start = ttk.Button(top, text='Start', command=self.start); self.btn_start.grid(row=4,column=1,sticky='w')
        self.btn_stop  = ttk.Button(top, text='Stop', command=self.stop, state=tk.DISABLED); self.btn_stop.grid(row=4,column=2,sticky='w')
        ttk.Button(top, text='Save CSV', command=self.save_csv).grid(row=4,column=3,sticky='w')

        cwrap = ttk.Frame(self); cwrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.canvas = tk.Canvas(cwrap, bg='#0a0f19', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.debug = ttk.Label(self, text='', foreground='#93a0b3'); self.debug.pack(anchor='w', padx=12)

        mon = ttk.Frame(self); mon.pack(fill=tk.BOTH, expand=False, padx=12, pady=(0,12))
        ttk.Label(mon, text='Axis Monitor (raw 0..65535)', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.tree = ttk.Treeview(mon, columns=('val',), show='headings', height=8)
        self.tree.heading('val', text='Value'); self.tree.column('val', width=360, anchor='w'); self.tree.pack(fill=tk.X, expand=True)

        try:
            ttk.Style().theme_use('clam')
        except Exception:
            pass

    # ---- Device mgmt ----
    def refresh(self):
        ids = WinMMBackend.list_devices()
        if not ids:
            self.dev_combo['values'] = []
            self.dev_combo.set('(no WinMM devices)')
        else:
            self.dev_combo['values'] = [str(i) for i in ids]
            # prefer 0 by default (your probe)
            self.dev_combo.set(str(ids[0]))

    def apply_device(self):
        s = self.dev_combo.get()
        if not s.isdigit():
            messagebox.showwarning('No device','Pick a device ID first.'); return
        self.backend.dev_id = int(s)
        messagebox.showinfo('Opened', f'Using WinMM device ID {s}')

    def apply_axes(self):
        self.backend.axis_brake = self.br_combo.get()
        self.backend.axis_thr   = self.th_combo.get()
        self.backend.cfg_b.invert = bool(self.br_inv.get())
        self.backend.cfg_t.invert = bool(self.th_inv.get())
        dz = float(self.dz.get()); self.backend.cfg_b.deadzone = dz; self.backend.cfg_t.deadzone = dz
        ms = float(self.sm.get()); self.backend.ema_b.ms = ms; self.backend.ema_t.ms = ms

    def calibrate(self):
        zb, zt = self.backend.calibrate_zero()
        messagebox.showinfo('Calibrated', f'Zero set to:\nBrake {zb:.3f}\nThrottle {zt:.3f}')

    # ---- Run/plot ----
    def start(self):
        self.buffer.clear(); self.btn_start.config(state=tk.DISABLED); self.btn_stop.config(state=tk.NORMAL)
        self.backend.t0=None; self.backend.ema_b.reset(); self.backend.ema_t.reset()
        self._loop()

    def stop(self):
        self.btn_start.config(state=tk.NORMAL); self.btn_stop.config(state=tk.DISABLED)

    def save_csv(self):
        if not self.buffer: return
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV','*.csv')], initialfile='pedal_trace.csv')
        if not path: return
        with open(path,'w',newline='') as f:
            w=csv.writer(f); w.writerow(['time_ms','brake','throttle'])
            for t,b,tb in self.buffer: w.writerow([int(t), f'{b:.4f}', f'{tb:.4f}'])

    def _loop(self):
        # Monitor
        vals = self.backend._read(self.backend.dev_id)
        self.tree.delete(*self.tree.get_children())
        if vals:
            for name, v in zip(AXIS_NAMES, vals):
                self.tree.insert('', 'end', values=(f'{name}: {v}',))
        # Plot
        sample = self.backend.poll()
        if sample:
            t,b,tb,raw = sample
            self.buffer.append((t,b,tb))
            self._draw()
            self.debug.config(text=f'Brake: {b:.3f}   Throttle: {tb:.3f}   Raw: {raw}')
        if str(self.btn_stop['state']) == 'normal':
            self.after(8, self._loop)

    def _draw(self):
        w = self.canvas.winfo_width() or 1000
        h = self.canvas.winfo_height() or 360
        self.canvas.delete('all')
        for i in range(0,11):
            y = h - int(h*(i/10))
            self.canvas.create_line(0,y,w,y,fill='#162033')
        if not self.buffer: return
        t_now = self.buffer[-1][0]/1000.0
        T = max(2.0, float(self.win.get()))
        t_min = max(0.0, t_now - T)
        def x_at(tms): return int(((tms/1000.0 - t_min)/T) * w)
        def y_at(v): return int(h - v*h)
        last=None
        for t,b,_ in self.buffer:
            if t/1000.0 < t_min: continue
            pt=(x_at(t), y_at(b))
            if last: self.canvas.create_line(last[0],last[1],pt[0],pt[1], fill='#48a0ff', width=2)
            last=pt
        last=None
        for t,_,tb in self.buffer:
            if t/1000.0 < t_min: continue
            pt=(x_at(t), y_at(tb))
            if last: self.canvas.create_line(last[0],last[1],pt[0],pt[1], fill='#7cdb6f', width=2)
            last=pt

if __name__ == '__main__':
    app = App()
    app.mainloop()
