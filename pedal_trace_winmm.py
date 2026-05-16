# Pedal Trace  Windows (Modular Main)
# -----------------------------------------
# UI runner wired to: backend_winmm.py
#
# Features
# • WinMM backend (joy.cpl layer) — no SDL/pygame/HID
# • Big live graph (brake blue, throttle green) with top-right brake %
# • Device picker, axis selectors, invert/deadzone/smoothing/window
# • Start/Stop, Save CSV
# • Axis Monitor is a popup (open/close button)
#
# Quick start:
#   py -3 -m venv .venv
#   .venv\Scripts\activate
#   python pedal_trace_winmm.py

from __future__ import annotations
import os, time, csv
from collections import deque


import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- Local modules ---
from backend_winmm import AXIS_NAMES, Backend as WinMMBackend

# ---- Paths (keep everything beside this file; works fine inside .venv) -----
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONFIG_DIR = os.path.join(ROOT, 'config')
for _p in (DATA_DIR, CONFIG_DIR):
    os.makedirs(_p, exist_ok=True)

# ---- UI --------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Pedal Trace — WinMM (Modular)')
        self.geometry('500x220')

        # Backend
        self.backend = WinMMBackend()  # defaults: dev_id=0, brake='Y', throttle='X'

        # Plot buffer
        self.buffer = deque(maxlen=120*60*5)

        # Popup refs
        self.axis_popup = None
        self.axis_tree = None

        self._build_ui()
        self.refresh()

    # -- UI build ------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=12, pady=8)

        ttk.Label(top, text='Device ID:').grid(row=0,column=0,sticky='w')
        self.dev_combo = ttk.Combobox(top, state='readonly', width=20)
        self.dev_combo.grid(row=0,column=1,sticky='w')
        ttk.Button(top, text='Refresh', command=self.refresh).grid(row=0,column=2,padx=6)
        ttk.Button(top, text='Open', command=self.apply_device).grid(row=0,column=3,padx=6)

        ttk.Label(top, text='Brake axis').grid(row=1,column=0,sticky='w')
        self.br_combo = ttk.Combobox(top, state='readonly', width=10, values=AXIS_NAMES)
        self.br_combo.set(getattr(self.backend, 'axis_brake', 'Y'))
        self.br_combo.grid(row=1,column=1,sticky='w')
        self.br_inv = tk.BooleanVar(value=getattr(self.backend.cfg_b, 'invert', False))
        ttk.Checkbutton(top, text='Invert', variable=self.br_inv, command=self.apply_axes).grid(row=1,column=2,sticky='w')

        ttk.Label(top, text='Throttle axis').grid(row=2,column=0,sticky='w')
        self.th_combo = ttk.Combobox(top, state='readonly', width=10, values=AXIS_NAMES)
        self.th_combo.set(getattr(self.backend, 'axis_thr', 'X'))
        self.th_combo.grid(row=2,column=1,sticky='w')
        self.th_inv = tk.BooleanVar(value=getattr(self.backend.cfg_t, 'invert', False))
        ttk.Checkbutton(top, text='Invert', variable=self.th_inv, command=self.apply_axes).grid(row=2,column=2,sticky='w')

        ttk.Label(top, text='Deadzone').grid(row=3,column=0,sticky='w')
        self.dz = tk.DoubleVar(value=getattr(self.backend.cfg_b, 'deadzone', 0.02))
        ttk.Spinbox(top, from_=0.0, to=0.2, increment=0.005, textvariable=self.dz, width=7, command=self.apply_axes).grid(row=3,column=1,sticky='w')
        ttk.Label(top, text='Smoothing (ms)').grid(row=3,column=2,sticky='w')
        self.sm = tk.IntVar(value=20)
        ttk.Spinbox(top, from_=0, to=200, increment=5, textvariable=self.sm, width=7, command=self.apply_axes).grid(row=3,column=3,sticky='w')
        ttk.Label(top, text='Window (s)').grid(row=3,column=4,sticky='w')
        self.win = tk.DoubleVar(value=8.0)
        ttk.Spinbox(top, from_=2, to=30, increment=1, textvariable=self.win, width=7).grid(row=3,column=5,sticky='w')

        # Actions
        ttk.Button(top, text='Calibrate zero', command=self.calibrate).grid(row=4,column=0,pady=6,sticky='w')
        self.btn_start = ttk.Button(top, text='Start', command=self.start)
        self.btn_start.grid(row=4,column=1,sticky='w')
        self.btn_stop  = ttk.Button(top, text='Stop', command=self.stop, state=tk.DISABLED)
        self.btn_stop.grid(row=4,column=2,sticky='w')
        ttk.Button(top, text='Save CSV', command=self.save_csv).grid(row=4,column=3,sticky='w')
        ttk.Button(top, text='Axis Monitor', command=self.toggle_axis_popup).grid(row=4,column=4,sticky='w', padx=6)

        # Graph area
        self.canvas = tk.Canvas(self, bg='#0a0f19', highlightthickness=0, height=100)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.debug = ttk.Label(self, text='', foreground='#93a0b3')
        self.debug.pack(anchor='w', padx=12, pady=(0,6))

        try:
            ttk.Style().theme_use('clam')
        except Exception:
            pass

    # -- Device mgmt ---------------------------------------------------------
    def refresh(self):
        try:
            ids = self.backend.list_devices()
        except Exception as e:
            messagebox.showerror('Backend error', f'list_devices failed: {e}')
            ids = []
        if not ids:
            self.dev_combo['values'] = []
            self.dev_combo.set('(no WinMM devices)')
        else:
            self.dev_combo['values'] = [str(i) for i in ids]
            self.dev_combo.set(str(ids[0]))

    def apply_device(self):
        s = self.dev_combo.get()
        if not s.isdigit():
            messagebox.showwarning('No device','Pick a device ID first.')
            return
        did = int(s)
        try:
            self.backend.apply_device(did)
        except AttributeError:
            self.backend.dev_id = did
        except Exception as e:
            messagebox.showerror('Open failed', str(e)); return
        messagebox.showinfo('Opened', f'Using WinMM device ID {did}')

    def apply_axes(self):
        self.backend.axis_brake = self.br_combo.get()
        self.backend.axis_thr   = self.th_combo.get()
        try:
            self.backend.cfg_b.invert = bool(self.br_inv.get())
            self.backend.cfg_t.invert = bool(self.th_inv.get())
            dz = float(self.dz.get())
            self.backend.cfg_b.deadzone = dz
            self.backend.cfg_t.deadzone = dz
            self.backend.set_smoothing(float(self.sm.get()))
        except Exception:
            pass

    def calibrate(self):
        try:
            zb, zt = self.backend.calibrate_zero()
        except Exception as e:
            messagebox.showerror('Calibrate failed', str(e)); return
        messagebox.showinfo('Calibrated', f'Zero set to:\nBrake {zb:.3f}\nThrottle {zt:.3f}')

    # -- Run/plot ------------------------------------------------------------
    def start(self):
        self.buffer.clear()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        # Reset backend filters
        try:
            self.backend.set_smoothing(float(self.sm.get()))
        except Exception:
            pass

        self._loop()

    def stop(self):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def save_csv(self):
        if not self.buffer:
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV','*.csv')], initialfile='pedal_trace.csv')
        if not path:
            return
        with open(path,'w',newline='') as f:
            w=csv.writer(f); w.writerow(['time_ms','brake','throttle'])
            for t,b,tb in self.buffer: w.writerow([int(t), f'{b:.4f}', f'{tb:.4f}'])

    def _loop(self):
        # Update popup axis monitor if open
        vals = None
        try:
            vals = self.backend.read_raw(getattr(self.backend, 'dev_id', 0))
        except Exception:
            pass
        if self.axis_tree is not None and self.axis_tree.winfo_exists():
            self.axis_tree.delete(*self.axis_tree.get_children())
            if vals:
                for name, v in zip(AXIS_NAMES, vals):
                    self.axis_tree.insert('', 'end', values=(name, v))

        # Plot + drill engine
        sample = None
        try:
            sample = self.backend.poll()
        except Exception:
            pass
        if sample:
            t,b,tb,raw = sample
            self.buffer.append((t,b,tb))
            self._draw()
            self.debug.config(text=f'Brake: {b:.3f}   Throttle: {tb:.3f}   Raw: {raw}')

        # Keep looping while running
        if str(self.btn_stop['state']) == 'normal':
            self.after(8, self._loop)

    def _draw(self):
        w = self.canvas.winfo_width() or 1000
        h = self.canvas.winfo_height() or 360
        self.canvas.delete('all')
        # grid
        for i in range(0,11):
            y = h - int(h*(i/10))
            self.canvas.create_line(0,y,w,y,fill='#162033')
        # live brake % (top-right)
        if self.buffer:
            _, b_last, _ = self.buffer[-1]
            pct = int(round(b_last*100))
            self.canvas.create_text(w-10, 18, text=f'{pct}%', anchor='ne', fill='#e6eefc', font=('Segoe UI', 14, 'bold'))
        # traces
        if not self.buffer:
            return
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

    # -- Axis monitor popup --------------------------------------------------
    def toggle_axis_popup(self):
        # Close if open
        if self.axis_popup is not None and self.axis_popup.winfo_exists():
            self.axis_popup.destroy(); self.axis_popup = None; self.axis_tree = None
            return
        # Create popup
        self.axis_popup = tk.Toplevel(self)
        self.axis_popup.title("Axis Monitor (raw 0..65535)")
        self.axis_popup.geometry("600x340")
        def _on_close():
            self.axis_popup.destroy(); self.axis_popup=None; self.axis_tree=None
        self.axis_popup.protocol("WM_DELETE_WINDOW", _on_close)
        ttk.Label(self.axis_popup, text='Axis Monitor (raw 0..65535)', font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=8, pady=6)
        cols = ('axis','value')
        self.axis_tree = ttk.Treeview(self.axis_popup, columns=cols, show='headings', height=10)
        self.axis_tree.heading('axis', text='Axis');   self.axis_tree.column('axis', width=80, anchor='w')
        self.axis_tree.heading('value', text='Value'); self.axis_tree.column('value', width=160, anchor='w')
        self.axis_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        # First fill
        try:
            vals = self.backend.read_raw(getattr(self.backend, 'dev_id', 0))
            if vals:
                for name, v in zip(AXIS_NAMES, vals):
                    self.axis_tree.insert('', 'end', values=(name, v))
        except Exception:
            pass


if __name__ == '__main__':
    app = App()
    app.mainloop()
