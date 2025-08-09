"""
coach.py  ChatGPT integration (optional) for Pedal Trainer

This module is safe to import even if there's no internet or OpenAI package.
If an API key is present in settings and the OpenAI client is available,
it will call the model; otherwise it falls back to a local heuristic coach.

Files/paths (kept alongside your main script):
  ./config/settings.json  → { "openai_api_key": "...", "model": "gpt-4o-mini", "coach_enabled": true }

Typical use from the UI:
    from coach import load_settings, save_settings, coach_advice

    settings = load_settings(CONFIG_DIR)
    text = coach_advice(settings=settings,
                        recent_summary=summary_text,
                        drill_cfg=cfg_as_dict,
                        session_stats=session_stats_dict)
    if text is None:
        text = "(Coach disabled or no data)"
    coach_textbox.set(text)
"""
from __future__ import annotations
import os, json, time
from typing import Optional, Dict, Any

# ---------- Settings I/O -----------------------------------------------------
DEFAULT_MODEL = "gpt-4o-mini"

def _settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "settings.json")

_DEF_SETTINGS = {
    "openai_api_key": "",
    "model": DEFAULT_MODEL,
    "coach_enabled": False,
}

def load_settings(config_dir: str) -> Dict[str, Any]:
    os.makedirs(config_dir, exist_ok=True)
    path = _settings_path(config_dir)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_DEF_SETTINGS, f, indent=2)
        return dict(_DEF_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # merge defaults for any missing keys
        merged = dict(_DEF_SETTINGS); merged.update(data or {})
        return merged
    except Exception:
        return dict(_DEF_SETTINGS)

def save_settings(config_dir: str, data: Dict[str, Any]) -> None:
    os.makedirs(config_dir, exist_ok=True)
    merged = dict(_DEF_SETTINGS); merged.update(data or {})
    with open(_settings_path(config_dir), "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

# ---------- Prompting --------------------------------------------------------
_DEF_SYS = (
    "You are a concise driving coach for sim-racing pedal technique. "
    "Given a summary of the latest drill session and the user's target settings, "
    "analyze mistakes and propose a short, specific next-drill plan. "
    "Keep it under 130 words. Use bullet points."
)

_DEF_USER_TEMPLATE = (
    "Latest session summary (plain text):\n"
    "{recent_summary}\n\n"
    "Drill config: {drill_cfg}\n"
    "Session stats: {session_stats}\n\n"
    "Return:\n"
    "- 3 bullets of observations (overshoot/correction/ttb/release/jitter)\n"
    "- 2 bullets of actionable cues to try next rep\n"
    "- 1 bullet: suggested next drill (target %, speed, release)\n"
)

# ---------- OpenAI call (optional) ------------------------------------------

def _call_openai(api_key: str, model: str, system: str, user: str) -> Optional[str]:
    """Return text or None if the call cannot be made (no pkg, bad key, etc.)."""
    if not api_key:
        return None
    try:
        # Try the official OpenAI Python client (>=1.0 style)
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(model=model or DEFAULT_MODEL,
                                       input=[{"role":"system","content":system},
                                              {"role":"user","content":user}],
                                       temperature=0.2,
                                       max_output_tokens=300)
        # Extract text
        for item in getattr(resp, "output", []) or []:
            if getattr(item, "type", "") == "message":
                parts = getattr(item, "content", []) or []
                for p in parts:
                    if p.get("type") == "text":
                        return p.get("text")
        # Fallback older access path
        if hasattr(resp, "output_text"):
            return resp.output_text
    except Exception:
        pass
    return None

# ---------- Local fallback coach --------------------------------------------

def _local_advice(recent_summary: str, drill_cfg: Dict[str, Any], session_stats: Dict[str, Any]) -> str:
    # Very small heuristic coach if API is unavailable
    ttb = session_stats.get("avg_ttb_ms") or session_stats.get("median_ttb_ms")
    overs = session_stats.get("overshoots", 0)
    corr = session_stats.get("early_corrections", 0)
    rel  = session_stats.get("avg_release_ms")
    release_goal = (drill_cfg or {}).get("release_goal", "MEDIUM")

    tips = []
    tips.append("• Breathe, then commit to first hit — no early dips.")
    if ttb is not None and ttb > 200:
        tips.append("• Quicker initial ramp: squeeze to target in ≤150 ms.")
    if overs and overs > 0:
        tips.append("• Ease the stab: stop at the band, don’t punch through.")
    if corr and corr > 0:
        tips.append("• Hold pressure for 200 ms after entry — avoid corrections.")
    if rel is not None:
        if release_goal == "MEDIUM" and not (300 <= rel <= 800):
            tips.append("• Aim to bleed off in ~500 ms for medium release.")
        if release_goal == "SLOW" and rel < 800:
            tips.append("• Slow your release — count ‘one-thousand-and’ to ~0.8–1.0 s.")

    nxt = "• Next drill: target 80% · FAST to band · MEDIUM release · 10 clean reps"
    if release_goal == "SLOW":
        nxt = "• Next drill: 50% · MEDIUM to band · SLOW release · 10 clean reps"

    header = "Coach (local):\n"
    body = "\n".join(tips[:3])
    return f"{header}{body}\n{nxt}"

# ---------- Public API -------------------------------------------------------

def coach_advice(settings: Dict[str, Any],
                 recent_summary: str,
                 drill_cfg: Dict[str, Any],
                 session_stats: Dict[str, Any]) -> Optional[str]:
    """Return advice text. If disabled or errors occur, returns local advice.
    Returns None only when there is no data at all.
    """
    if not recent_summary:
        return None

    enabled = bool(settings.get("coach_enabled", False))
    api_key = (settings or {}).get("openai_api_key", "")
    model   = (settings or {}).get("model", DEFAULT_MODEL)

    if enabled and api_key:
        user = _DEF_USER_TEMPLATE.format(recent_summary=recent_summary,
                                         drill_cfg=drill_cfg,
                                         session_stats=session_stats)
        text = _call_openai(api_key=api_key, model=model, system=_DEF_SYS, user=user)
        if text:
            return text

    # Fallback to local advice
    return _local_advice(recent_summary, drill_cfg, session_stats)

# ---------- Tiny demo --------------------------------------------------------
if __name__ == "__main__":
    cfg = {"target_pct": 80, "app_goal": "FAST", "release_goal": "MEDIUM"}
    stats = {"avg_ttb_ms": 180, "overshoots": 2, "early_corrections": 1, "avg_release_ms": 620}
    print(coach_advice(_DEF_SETTINGS, "Peak 0.82, some oscillation near 0.5.", cfg, stats))
