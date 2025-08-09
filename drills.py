"""
Drill grading engine for Pedal Trainer (modular).

Pure logic (no UI, no sound). Feed it time (ms) and brake value (0..1),
get back *events* with per-rep metrics and pass/fail.

Usage from UI loop:
    eng = DrillEngine(DrillConfig(target_pct=80, app_goal='FAST', release_goal='MEDIUM'))
    events = eng.update(t_ms, brake01)  # call every frame
    for ev in events:
        if ev['type'] == 'rep_complete':
            metrics = ev['metrics']
            passed  = ev['passed']
            # update streak, show toast, play beep if passed, etc.

Key rules (defaults tuned for load-cell pedals):
- Onset threshold: 3% (after deadzone)
- Rep ends when brake < 3% for ≥200 ms
- Application speed: FAST ≤120 ms; MEDIUM ≤250 ms; slower = slow (bad)
- Release speed (time from last-in-band to <3%):
    MEDIUM = 300–800 ms; SLOW > 800 ms; (<300 considered too fast for these drills)
- Band tolerance: ±4% (configurable)
- Detect overshoot (> target + 6%), early correction (drop ≥6% within 200 ms of entry),
  oscillation (≥3 band crossings in 500 ms), release bump (+≥5% during release)
- Optional hold requirement: stay in band for ≥ 150 ms (configurable)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# ---- Thresholds -------------------------------------------------------------
FAST_MAX_MS   = 120.0
MED_MAX_MS    = 250.0
REL_MED_MIN   = 300.0
REL_MED_MAX   = 800.0
REL_SLOW_MIN  = 800.0
BAND_TOL_PCT  = 0.04
OVER_PCT      = 0.06
CORRECT_PCT   = 0.06
RELBUMP_PCT   = 0.05
ONSET_THRESH  = 0.03
END_HYST_MS   = 200.0
HOLD_MIN_MS   = 150.0
OSC_WIN_MS    = 500.0
OSC_MIN_CROSS = 3

# ---- Config -----------------------------------------------------------------
@dataclass
class DrillConfig:
    target_pct: int = 80                 # 10..99
    app_goal: str = 'FAST'               # 'FAST' | 'MEDIUM' | 'ANY'
    release_goal: str = 'MEDIUM'         # 'MEDIUM' | 'SLOW' | 'ANY'
    band_tol: float = BAND_TOL_PCT       # ± tolerance in 0..1
    hold_required_ms: int = 0            # 0 to disable; else e.g., 150 or 10000
    hold_tol: float = 0.03               # for holding drills
    onset_thresh: float = ONSET_THRESH
    end_hysteresis_ms: float = END_HYST_MS

    def band_low(self) -> float:
        return max(0.0, self.target_pct/100.0 - self.band_tol)
    def band_high(self) -> float:
        return min(1.0, self.target_pct/100.0 + self.band_tol)

# ---- Engine -----------------------------------------------------------------
@dataclass
class DrillEngine:
    cfg: DrillConfig
    state: str = 'IDLE'  # IDLE | APPLY | IN_BAND | RELEASE
    last_t: Optional[float] = None
    last_b: float = 0.0
    onset_t: Optional[float] = None
    first_inband_t: Optional[float] = None
    last_inband_t: Optional[float] = None
    end_candidate_t: Optional[float] = None
    peak: float = 0.0
    overshoot: float = 0.0
    did_correction: bool = False
    crosses: List[float] = field(default_factory=list)
    release_bump: bool = False

    def reset_rep(self):
        self.state = 'IDLE'
        self.onset_t = None
        self.first_inband_t = None
        self.last_inband_t = None
        self.end_candidate_t = None
        self.peak = 0.0
        self.overshoot = 0.0
        self.did_correction = False
        self.crosses.clear()
        self.release_bump = False

    def update(self, t_ms: float, b: float) -> List[Dict]:
        """Feed one sample. Returns list of events (may be empty).
        Each event: {'type': 'rep_complete', 'metrics': {...}, 'passed': bool}
        """
        events: List[Dict] = []
        cfg = self.cfg
        lo, hi = cfg.band_low(), cfg.band_high()

        # Track band crossings for oscillation detection
        if self.last_t is None:
            self.last_t = t_ms
            self.last_b = b
        else:
            was_in = lo <= self.last_b <= hi
            now_in = lo <= b <= hi
            if was_in != now_in:
                self.crosses.append(t_ms)
            self.last_t = t_ms
            self.last_b = b

        # Update peak & overshoot
        if b > self.peak:
            self.peak = b
            if b > hi + OVER_PCT:
                self.overshoot = max(self.overshoot, b - (self.cfg.target_pct/100.0))

        # State machine
        if self.state == 'IDLE':
            if b >= cfg.onset_thresh:
                self.state = 'APPLY'
                self.onset_t = t_ms
        elif self.state == 'APPLY':
            if lo <= b <= hi:
                self.state = 'IN_BAND'
                self.first_inband_t = t_ms
                self.last_inband_t = t_ms
            # Early correction: after entering band we check dips, so nothing here yet
        elif self.state == 'IN_BAND':
            # update last-in-band
            if lo <= b <= hi:
                self.last_inband_t = t_ms
                # detect early correction within 200 ms of first entry
                if (t_ms - (self.first_inband_t or t_ms)) <= 200.0:
                    if b < (self.cfg.target_pct/100.0 - CORRECT_PCT):
                        self.did_correction = True
            else:
                # left the band → move to RELEASE phase
                self.state = 'RELEASE'
        elif self.state == 'RELEASE':
            # release bump: any upward movement ≥5% after leaving band
            if self.last_b is not None and (b - self.last_b) >= RELBUMP_PCT:
                self.release_bump = True

        # End-of-rep detection: below onset for a while
        if self.state in ('APPLY','IN_BAND','RELEASE'):
            if b < cfg.onset_thresh:
                if self.end_candidate_t is None:
                    self.end_candidate_t = t_ms
                elif (t_ms - self.end_candidate_t) >= cfg.end_hysteresis_ms:
                    # finalize rep
                    metrics = self._finalize_metrics()
                    passed = self._passed(metrics)
                    events.append({'type':'rep_complete','metrics':metrics,'passed':passed})
                    self.reset_rep()
                    # after reset, keep sampling
            else:
                self.end_candidate_t = None

        return events

    # -- metrics & grading ---------------------------------------------------
    def _finalize_metrics(self) -> Dict:
        cfg = self.cfg
        onset = self.onset_t
        first = self.first_inband_t
        last  = self.last_inband_t
        ttb   = None if (onset is None or first is None) else max(0.0, first - onset)
        release_ms = None
        if last is not None and self.end_candidate_t is not None:
            # Approx release time: last in band → time we first went <3% (the end_candidate start)
            release_ms = max(0.0, self.end_candidate_t - last)
        # Oscillations in a short window
        osc = 0
        if self.crosses:
            t0 = self.crosses[-1] - OSC_WIN_MS
            osc = sum(1 for t in self.crosses if t >= t0)
        # Hold time if required
        hold_ms = 0.0
        if first is not None and last is not None:
            hold_ms = max(0.0, last - first)
        # Overshoot against target
        overshoot_pct = max(0.0, self.peak - (cfg.target_pct/100.0))
        return {
            'ttb_ms': ttb,
            'peak_pct': self.peak*100.0,
            'overshoot_pct': overshoot_pct*100.0,
            'early_correction': self.did_correction,
            'oscillations': osc,
            'release_ms': release_ms,
            'release_bump': self.release_bump,
            'hold_ms': hold_ms,
        }

    def _passed(self, m: Dict) -> bool:
        cfg = self.cfg
        # must enter band within 400ms
        if m['ttb_ms'] is None or m['ttb_ms'] > 400.0:
            return False
        # application goal
        if cfg.app_goal == 'FAST' and not (m['ttb_ms'] <= FAST_MAX_MS):
            return False
        if cfg.app_goal == 'MEDIUM' and not (FAST_MAX_MS < m['ttb_ms'] <= MED_MAX_MS):
            return False
        # overshoot & early correction
        if m['overshoot_pct'] > OVER_PCT*100.0:
            return False
        if m['early_correction']:
            return False
        # hold requirement (if any)
        if cfg.hold_required_ms > 0 and (m['hold_ms'] < max(HOLD_MIN_MS, cfg.hold_required_ms)):
            return False
        # release goal
        if m['release_ms'] is None:
            return False
        if cfg.release_goal == 'MEDIUM' and not (REL_MED_MIN <= m['release_ms'] <= REL_MED_MAX):
            return False
        if cfg.release_goal == 'SLOW' and not (m['release_ms'] >= REL_SLOW_MIN):
            return False
        # no big release bump
        if m['release_bump']:
            return False
        # oscillations
        if m['oscillations'] >= OSC_MIN_CROSS:
            return False
        return True

# ---- Streak/session tracker -------------------------------------------------
@dataclass
class StreakTracker:
    goal: int = 10
    streak: int = 0
    best: int = 0
    reps: int = 0
    start_ms: Optional[float] = None
    end_ms: Optional[float] = None

    def note_rep(self, t_ms: float, passed: bool):
        if self.start_ms is None:
            self.start_ms = t_ms
        self.reps += 1
        if passed:
            self.streak += 1
            self.best = max(self.best, self.streak)
        else:
            self.streak = 0
        self.end_ms = t_ms

    def complete(self) -> bool:
        return self.streak >= self.goal

    def summary(self) -> Dict:
        dur = 0 if (self.start_ms is None or self.end_ms is None) else max(0, self.end_ms - self.start_ms)
        return {
            'reps': self.reps,
            'best_streak': self.best,
            'completed_goal': self.best >= self.goal,
            'duration_ms': dur,
        }

# ---- Feedback strings -------------------------------------------------------

def feedback_for(metrics: Dict, cfg: DrillConfig) -> str:
    msgs: List[str] = []
    ttb = metrics['ttb_ms']
    if ttb is None:
        msgs.append('Never reached the target band — apply quicker to reach it within 400 ms.')
    else:
        if cfg.app_goal == 'FAST' and ttb > FAST_MAX_MS:
            msgs.append(f'Application too slow ({ttb:.0f} ms). Goal ≤ {FAST_MAX_MS:.0f} ms. Push faster initially.')
        elif cfg.app_goal == 'MEDIUM' and not (FAST_MAX_MS < ttb <= MED_MAX_MS):
            msgs.append(f'Application off target ({ttb:.0f} ms). Aim for {FAST_MAX_MS:.0f}–{MED_MAX_MS:.0f} ms.')
    if metrics['overshoot_pct'] > OVER_PCT*100.0:
        msgs.append(f'Overshoot +{metrics["overshoot_pct"]:.0f}%. Ease initial stab and stop at target.')
    if metrics['early_correction']:
        msgs.append('Early correction detected — commit to first hit, avoid dropping below target.')
    if metrics['oscillations'] >= OSC_MIN_CROSS:
        msgs.append('Oscillation detected — smooth the application and release.')
    rm = metrics['release_ms']
    if rm is not None:
        if cfg.release_goal == 'MEDIUM' and not (REL_MED_MIN <= rm <= REL_MED_MAX):
            msgs.append(f'Release {rm:.0f} ms is off. Aim for {REL_MED_MIN:.0f}–{REL_MED_MAX:.0f} ms.')
        if cfg.release_goal == 'SLOW' and rm < REL_SLOW_MIN:
            msgs.append(f'Release {rm:.0f} ms too fast. Aim ≥ {REL_SLOW_MIN:.0f} ms.')
    if metrics['release_bump']:
        msgs.append('Release bump detected — avoid re-adding pressure on exit.')
    if cfg.hold_required_ms > 0 and metrics['hold_ms'] < max(HOLD_MIN_MS, cfg.hold_required_ms):
        need = max(HOLD_MIN_MS, cfg.hold_required_ms)
        msgs.append(f'Hold too short ({metrics["hold_ms"]:.0f} ms). Hold ≥ {need:.0f} ms inside the band.')
    if not msgs:
        return 'Good rep — matches the drill targets.'
    return ' · '.join(msgs)

# ---- Quick self-test --------------------------------------------------------
if __name__ == '__main__':
    # Synthetic test: one fast-to-80% rep with medium release
    cfg = DrillConfig(target_pct=80, app_goal='FAST', release_goal='MEDIUM')
    eng = DrillEngine(cfg)
    t = 0.0
    evs_all: List[dict] = []
    # idle 200ms
    for _ in range(20): t+=10; evs_all += eng.update(t, 0.0)
    # fast ramp to ~0.8 in 100ms
    for k in range(10):
        t+=10
        b = min(0.8, 0.08*k)
        evs_all += eng.update(t, b)
    # hold 200ms
    for _ in range(20): t+=10; evs_all += eng.update(t, 0.8)
    # medium release ~500ms down to 0
    for k in range(50):
        t+=10
        b = max(0.0, 0.8*(1 - k/50))
        evs_all += eng.update(t, b)
    # stabilize below threshold 300ms
    for _ in range(30): t+=10; evs_all += eng.update(t, 0.0)

    reps = [e for e in evs_all if e['type']=='rep_complete']
    assert len(reps)==1, f"expected 1 rep, got {len(reps)}"
    m = reps[0]['metrics']
    print('Rep metrics:', m)
    print('Passed:', reps[0]['passed'])
    print('Feedback:', feedback_for(m, cfg))

