"""Voice runtime config loader — mirrors orbit/policy.py's load_*() pattern
for the other config/*.yaml files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_CONFIG_PATH = _CONFIG_DIR / "voice.yaml"
_KEYTERMS_PATH = _CONFIG_DIR / "keyterms.yaml"

_DEFAULTS: dict[str, Any] = {
    "hotkey": "ctrl+space",
    "tts_enabled": True,
    "deepgram_model": "nova-3",
    "deepgram_language": "multi",
    "deepgram_cost_per_minute_usd": 0.0058,
    "daily_cost_cap_usd": 2.00,
    # Fast-release flush (requirement 5): hard cap on waiting for the tail
    # after Finalize. The socket is NOT torn down, so this is short.
    "flush_timeout_s": 1.0,
    # Once is_final segments start arriving after Finalize, return this many
    # seconds after the last one (requirement 5's "~250ms").
    "finalize_quiet_s": 0.25,
    "fallback_model_size": "small",
    # --- latency knobs (Aug-2026 Deepgram latency work) -------------------
    # Keep the persistent socket open with a KeepAlive every N seconds
    # (Deepgram drops idle sockets after ~10s). Background reconnect backoff.
    "keepalive_interval_s": 5.0,
    "reconnect_backoff_initial_s": 0.5,
    "reconnect_backoff_max_s": 8.0,
    # Capture frame size in samples (16kHz): 512 = 32ms, requirement 4's
    # 20-50ms band. Smaller = lower latency, more callbacks.
    "capture_blocksize": 512,
    # Rolling mic pre-buffer (requirement 3). When enabled the microphone is
    # held open continuously and the last `pre_buffer_ms` of audio is kept in
    # a local ring buffer (never transmitted) so speech that starts as the
    # key is pressed is still captured. Because this keeps the mic open, it is
    # a visible, deliberate setting — see voice.yaml / privacy.py.
    "pre_buffer_enabled": True,
    "pre_buffer_ms": 500,
    # Per-utterance stage timing to the log (requirement 1).
    "perf_logging": True,
    # --- Barge-in: talk over a spoken reply to cut it off (hands-free) -----
    # A local energy VAD runs only while she's actually speaking; on sustained
    # speech above the echo/bleed floor it stops playback. Local only — no
    # transcription, no network, no auto-capture (you still press to talk for
    # the next command). Keeps the mic open continuously while the runtime
    # runs (like the pre-buffer) — turn off to close it when not held.
    "barge_in_enabled": True,
    "barge_in_energy_ratio": 3.0,     # fire above baseline_bleed * this
    "barge_in_min_ms": 300,           # sustained speech required before firing
    "barge_in_start_grace_ms": 350,   # settle the bleed baseline before triggering
    "barge_in_abs_floor": 0.01,       # absolute RMS floor (protects the silent/headphone case)
    "tts_voice": "af_heart",
    "tts_disable_espeak_fallback": True,
    "input_device": None,
    "output_device": None,
}


def load_voice_config(path: Optional[Path] = None) -> dict[str, Any]:
    load_dotenv()  # idempotent; ensures DEEPGRAM_API_KEY is present even if
    # this is imported before orbit.agent (which also calls it) has run.
    path = path or _CONFIG_PATH
    raw = yaml.safe_load(path.read_text()) or {}
    config = {**_DEFAULTS, **raw}

    keyterms_raw = yaml.safe_load(_KEYTERMS_PATH.read_text()) or {}
    config["keyterms"] = keyterms_raw.get("keyterms") or []

    config["deepgram_api_key"] = os.environ.get("DEEPGRAM_API_KEY", "")
    return config
