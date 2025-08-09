# Pedal Trace  Windows (FINAL Modular Main)
# -----------------------------------------
# UI runner wired to: backend_winmm.py, drills.py, coach.py
#
# Features
# • WinMM backend (joy.cpl layer) — no SDL/pygame/HID
# • Big live graph (brake blue, throttle green) with top-right brake %
# • Device picker, axis selectors, invert/deadzone/smoothing/window
# • Start/Stop, Save CSV, Coach panel (right)
# • Axis Monitor is a popup (open/close button)
# • Drill engine: grades reps live, shows per‑rep feedback, 10‑in‑a‑row streak
# • Coach button: uses ChatGPT if enabled (coach.py) or local fallback
# • Beep on pass (drop your beep.wav into ./assets/beep.wav)
#
# Quick start:
#   py -3 -m venv .venv
#   .venv\Scripts\activate
#   python pedal_trace_winmm.py

from __future__ import annotations
import os, time, csv
from collections import deque
from typing import Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import winsound  # Windows sound API

# --- Local modules ---
from backend_winmm import AXIS_NAMES, Backend as WinMMBackend
from drills import DrillConfig, DrillEngine, StreakTracker, feedback_for
from coach import load_settings, coach_advice

# ---- Paths (keep everything beside this file; works fine inside .venv) -----
ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(ROOT, 'assets')
DATA_DIR = os.path.join(ROOT, 'data')
CONFIG_DIR = os.path.join(ROOT, 'config')
BEEP_PATH = os.path.join(ASSETS_DIR, 'beep.wav')
for _p in (ASSETS_DIR, DATA_DIR, CONFIG_DIR):
    os.makedirs(_p, exist_ok=True)

# ---- UI --------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Pedal Trace — WinMM (Modular)')
        self.geometry('1260x780')

        # Backend
        self.backend = WinMMBackend()  # defaults: dev_id=0, brake='Y', throttle='X'

        # Drill state
        self.drill_cfg = DrillConfig(target_pct=80, app_goal='FAST', release_goal='MEDIUM',
                                     band_tol=0.04, hold_required_ms=0)
        self.drill: Optional[DrillEngine] = None
        self.streak = StreakTracker(goal=10)
        self.session_stats = {
            "avg_ttb_ms": None,
            "avg_release_ms": None,
            "overshoots": 0,
            "early_corrections": 0,
            "release_bumps": 0,
            "oscillations": 0,
            "reps": 0,
        }
        self._ttb_accum = []
        self._rel_accum = []
        self._last_feedback = ""

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
        ttk.Button(top, text='Coach', command=self.on_coach).grid(row=4,column=4,sticky='w', padx=6)
        ttk.Button(top, text='Axis Monitor', command=self.toggle_axis_popup).grid(row=4,column=5,sticky='w', padx=6)
        self.streak_label = ttk.Label(top, text="Streak 0/10")
        self.streak_label.grid(row=4, column=6, sticky='w', padx=6)

        # Split main area into left (graph) and right (coach box)
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        left = ttk.Frame(paned); right = ttk.Frame(paned, width=380)
        paned.add(left, weight=3); paned.add(right, weight=1)

        self.canvas = tk.Canvas(left, bg='#0a0f19', highlightthickness=0, height=460)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.debug = ttk.Label(left, text='', foreground='#93a0b3')
        self.debug.pack(anchor='w', padx=4, pady=(4,6))

        ttk.Label(right, text='Coach', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        self.coach_out = ScrolledText(right, height=20, wrap=tk.WORD)
        self.coach_out.pack(fill=tk.BOTH, expand=True)
        self.coach_out.insert('end', "No data yet. Press Start and do a few reps, then click Coach.\n")
        self.coach_out.config(state='disabled')

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
        # Reset plot + drill state
        self.buffer.clear()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)

        self.drill = DrillEngine(self.drill_cfg)
        self.streak = StreakTracker(goal=10)
        self.session_stats.update({
            "avg_ttb_ms": None,
            "avg_release_ms": None,
            "overshoots": 0,
            "early_corrections": 0,
            "release_bumps": 0,
            "oscillations": 0,
            "reps": 0,
        })
        self._ttb_accum.clear(); self._rel_accum.clear(); self._last_feedback = ""

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

            # Drill grading
            if self.drill is not None:
                events = self.drill.update(t_ms=t, b=b)
                for ev in events:
                    if ev.get('type') == 'rep_complete':
                        m = ev['metrics']
                        passed = ev['passed']
                        self.streak.note_rep(t_ms=t, passed=passed)
                        self.streak_label.config(text=f"Streak {self.streak.streak}/{self.streak.goal}")

                        # Aggregates
                        self.session_stats["reps"] += 1
                        if m.get("ttb_ms") is not None:
                            self._ttb_accum.append(m["ttb_ms"])
                            self.session_stats["avg_ttb_ms"] = sum(self._ttb_accum)/len(self._ttb_accum)
                        if m.get("release_ms") is not None:
                            self._rel_accum.append(m["release_ms"])
                            self.session_stats["avg_release_ms"] = sum(self._rel_accum)/len(self._rel_accum)
                        if (m.get("overshoot_pct") or 0) > 0:
                            self.session_stats["overshoots"] += 1
                        if m.get("early_correction"):
                            self.session_stats["early_corrections"] += 1
                        if m.get("release_bump"):
                            self.session_stats["release_bumps"] += 1
                        self.session_stats["oscillations"] += int(m.get("oscillations") or 0)

                        # Per-rep feedback
                        fb = feedback_for(m, self.drill_cfg)
                        rep_no = self.session_stats["reps"]
                        verdict = "PASS ✅" if passed else "FAIL ⚠️"
                        self._last_feedback = (
                            f"Rep {rep_no}: {verdict} · "
                            f"TTB {m.get('ttb_ms') and int(m['ttb_ms'])} ms · "
                            f"Rel {m.get('release_ms') and int(m['release_ms'])} ms\n{fb}"
                        )
                        # Show feedback immediately
                        self.coach_out.config(state='normal')
                        self.coach_out.delete('1.0', 'end')
                        self.coach_out.insert('end', self._last_feedback)
                        self.coach_out.config(state='disabled')

                        # Beep on pass
                        if passed and os.path.isfile(BEEP_PATH):
                            try:
                                winsound.PlaySound(BEEP_PATH, winsound.SND_FILENAME | winsound.SND_ASYNC)
                            except Exception:
                                pass

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

    # -- Coach panel ---------------------------------------------------------
    def on_coach(self):
        # Need at least some samples
        if not self.buffer:
            self.coach_out.config(state='normal')
            self.coach_out.delete('1.0', 'end')
            self.coach_out.insert('end', 'No data yet. Press Start and perform a few reps, then click Coach.')
            self.coach_out.config(state='disabled')
            return

        # Build a compact summary from current session stats
        ss = self.session_stats
        rep_ct = ss.get("reps", 0)
        avg_ttb = ss.get("avg_ttb_ms")
        avg_rel = ss.get("avg_release_ms")
        summary = [
            f"Reps: {rep_ct}",
            f"Avg TTB: {int(avg_ttb)} ms" if avg_ttb is not None else "Avg TTB: n/a",
            f"Avg release: {int(avg_rel)} ms" if avg_rel is not None else "Avg release: n/a",
            f"Overshoots: {ss.get('overshoots',0)}",
            f"Early corrections: {ss.get('early_corrections',0)}",
            f"Oscillations: {ss.get('oscillations',0)}",
            f"Release bumps: {ss.get('release_bumps',0)}",
        ]
        summary_text = " · ".join(summary)

        cfg_dict = {
            "target_pct": self.drill_cfg.target_pct,
            "app_goal": self.drill_cfg.app_goal,
            "release_goal": self.drill_cfg.release_goal,
            "band_tol_pct": self.drill_cfg.band_tol,
            "hold_required_ms": self.drill_cfg.hold_required_ms,
        }

        settings = load_settings(CONFIG_DIR)
        advice_text = coach_advice(
            settings=settings,
            recent_summary=summary_text,
            drill_cfg=cfg_dict,
            session_stats=self.session_stats
        ) or "(No advice available)"

        # Show in panel (include last rep feedback if present)
        self.coach_out.config(state='normal')
        self.coach_out.delete('1.0', 'end')
        if self._last_feedback:
            self.coach_out.insert('end', self._last_feedback + "\n\n")
        self.coach_out.insert('end', advice_text)
        self.coach_out.config(state='disabled')

if __name__ == '__main__':
    app = App()
    app.mainloop()
