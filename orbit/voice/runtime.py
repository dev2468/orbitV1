"""VoiceRuntime — wires hotkey -> capture -> transcriber -> run_task() ->
TTS. Reuses run_task() exactly as-is (same task/lane/safety path every
other caller goes through) — no parallel execution route.

Threading model unchanged from the original build: pynput's hotkey
listener and sounddevice's capture callback each run on their own native
OS thread — neither is asyncio-aware. Only the flush + run_task() + speak()
sequence needs the asyncio loop; hotkey handling, capture start/stop, and
the daily-cap check are plain thread-safe calls made directly on whichever
thread triggered them. The one asyncio-crossing point is scheduling
_finish_utterance() from the hotkey-release callback via
asyncio.run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import numpy as np

from orbit import db
from orbit.run_task import run_task
from orbit.tools.foundation import redact_secrets
from orbit.voice import instrumentation as perf
from orbit.voice.bargein import BargeInDetector
from orbit.voice.capture import AudioCaptureError, PushToTalkCapture
from orbit.voice.config import load_voice_config
from orbit.voice.hotkey import PushToTalkHotkey
from orbit.voice.instrumentation import UtteranceTimings
from orbit.voice.transcriber import TranscriberBackend, load_transcriber
from orbit.voice.tts import VoiceSpeaker

logger = logging.getLogger("orbit.voice.runtime")

_TRANSCRIPTION_TOOL_CALL = "voice_transcription"


class VoiceRuntime:
    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config = config or load_voice_config()
        if not self.config.get("deepgram_api_key"):
            logger.warning(
                "DEEPGRAM_API_KEY is not set (see .env) — every utterance "
                "will fall back to offline (faster-whisper) transcription."
            )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Barge-in: talk over a spoken reply to cut it off (hands-free). Local
        # energy VAD only, no transcription — see bargein.py. The detector is
        # fed from the capture monitor callback while she's actually speaking.
        barge_in = self.config["barge_in_enabled"]
        self._bargein: Optional[BargeInDetector] = (
            BargeInDetector(
                samplerate=16000,
                blocksize=self.config["capture_blocksize"],
                energy_ratio=self.config["barge_in_energy_ratio"],
                min_ms=self.config["barge_in_min_ms"],
                start_grace_ms=self.config["barge_in_start_grace_ms"],
                abs_floor=self.config["barge_in_abs_floor"],
            )
            if barge_in else None
        )
        self._capture = PushToTalkCapture(
            device=self.config["input_device"],
            blocksize=self.config["capture_blocksize"],
            pre_buffer_enabled=self.config["pre_buffer_enabled"],
            pre_buffer_ms=self.config["pre_buffer_ms"],
            monitor_enabled=barge_in,
            on_monitor=self._on_monitor_chunk if barge_in else None,
        )
        self._speaker = VoiceSpeaker(
            voice=self.config["tts_voice"],
            enabled=self.config["tts_enabled"],
            output_device=self.config["output_device"],
        )
        self._transcriber: Optional[TranscriberBackend] = None
        self._hotkey: Optional[PushToTalkHotkey] = None
        # Guards re-entrancy: set the instant capture actually starts (not
        # just when run_task() begins), so a hotkey press arriving before
        # the release-triggered coroutine has even been scheduled still
        # sees the guard up. Reset in _finish_utterance's finally, or
        # immediately if capture never actually started.
        self._busy = False
        self._pending_cost_usd = 0.0
        # Per-utterance timing (requirement 1). Set on hotkey-down, marked
        # from the hotkey/ws threads via _record_mark, reset in
        # _finish_utterance. None between utterances / when perf_logging off.
        self._timings: Optional[UtteranceTimings] = None
        # State a future UI orb animation should bind to. No such UI exists
        # anywhere in this project as of this build (checked: no mockup,
        # Settings screen, or animation asset found in a full-repo search)
        # — nothing currently reads this. Transitions are correct and
        # exposed now so a UI can attach later without touching this file.
        self.state = "idle"  # idle | listening | thinking | speaking

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("loading TTS...")
        await asyncio.to_thread(self._speaker.load)
        logger.info(
            "voice runtime ready — deepgram %s/%s, offline fallback faster-whisper/%s",
            self.config["deepgram_model"], self.config["deepgram_language"],
            self.config["fallback_model_size"],
        )
        # Constructing the transcriber opens the persistent Deepgram socket in
        # the background (requirement 2) — warm well before the first press.
        self._transcriber = load_transcriber(
            self.config,
            on_cost=self._on_transcription_cost,
            on_fallback=self._on_fallback,
            on_mark=self._record_mark,
        )
        # Rolling pre-buffer (requirement 3): open the mic now so the ring is
        # warm on the first press. No-op unless pre_buffer_enabled.
        self._capture.open()
        if self.config["pre_buffer_enabled"]:
            logger.info(
                "rolling pre-buffer ON — mic held open, last %dms kept locally "
                "(never transmitted); set pre_buffer_enabled: false to disable",
                self.config["pre_buffer_ms"],
            )
        if self._bargein is not None:
            logger.info(
                "barge-in ON — talk over a reply to cut it off (local only, no "
                "transcription); set barge_in_enabled: false to disable"
            )
        self._hotkey = PushToTalkHotkey(
            self.config["hotkey"], self._on_hotkey_press, self._on_hotkey_release
        )
        self._hotkey.start()
        logger.info("hold %s to talk", self.config["hotkey"])

    def stop(self) -> None:
        if self._hotkey is not None:
            self._hotkey.stop()
        self._capture.close()  # full teardown — closes the continuous stream too
        if self._transcriber is not None:
            self._transcriber.close()
        self._speaker.close()

    def _record_mark(self, name: str) -> None:
        # Routed from the transcriber's on_mark (ws thread) and called on the
        # hotkey thread. Marks into the current utterance's timings, if any.
        timings = self._timings
        if timings is not None:
            timings.mark(name)

    # ------------------------------------------------------------------
    # Hotkey callbacks — run on pynput's listener thread.
    # ------------------------------------------------------------------

    def _on_hotkey_press(self) -> None:
        # Stamp the true press instant first so the timing report measures
        # from the physical key-down, not from after the work below.
        t_press = time.monotonic()

        # Always interrupt in-flight speech first — this IS the mute/
        # interrupt mechanism (see tts.py's docstring). Unconditional and
        # safe even when nothing is playing.
        self._speaker.stop()

        if self._busy:
            logger.info("ignoring hotkey press — a task is already running")
            return

        # requirement 7: checked BEFORE starting a new transcription, not
        # after, so the cap actually stops spend rather than just
        # reporting it once it's already gone.
        today_cost = db.get_daily_cost(_TRANSCRIPTION_TOOL_CALL)
        cap = self.config["daily_cost_cap_usd"]
        if today_cost >= cap:
            logger.warning("daily voice transcription cap reached: $%.4f >= $%.2f", today_cost, cap)
            self._speaker.speak(
                "Today's voice transcription budget has been used up. "
                "Voice input is paused until tomorrow."
            )
            return

        self._busy = True
        self.state = "listening"
        self._pending_cost_usd = 0.0
        self._timings = UtteranceTimings() if self.config["perf_logging"] else None
        if self._timings is not None:
            self._timings.mark(perf.HOTKEY_DOWN, at=t_press)

        self._transcriber.start_stream()
        try:
            self._capture.start(self._on_audio_chunk)
        except AudioCaptureError as exc:
            logger.error("%s", exc)
            self._speaker.speak(str(exc))
            self.state = "idle"
            self._busy = False  # capture never actually started

    def _on_hotkey_release(self) -> None:
        t_release = time.monotonic()
        if not self._capture.is_active:
            return  # press was rejected (busy/cap) or errored — nothing to finish
        if self._timings is not None:
            self._timings.mark(perf.HOTKEY_RELEASE, at=t_release)
        self._capture.stop()
        # requirement 3: the flush window (stop_and_flush, in
        # _finish_utterance below) is exactly the gap this "thinking"
        # state is meant to cover for a future orb animation.
        self.state = "thinking"
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._finish_utterance(), self._loop)

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        # Runs on the PortAudio callback thread — must never block or raise.
        try:
            self._transcriber.feed_audio(chunk, 16000)
        except Exception:  # noqa: BLE001
            logger.exception("feed_audio failed")

    def _on_monitor_chunk(self, chunk: np.ndarray) -> None:
        # Runs on the PortAudio callback thread for EVERY block while the mic
        # is open — must stay cheap and never block. Barge-in: only while she's
        # ACTUALLY speaking (is_speaking(), not self.state — speak() is
        # fire-and-forget so state has already flipped back to idle), watch for
        # the user talking over the reply and stop playback. Local only.
        if self._bargein is None:
            return
        try:
            if not self._speaker.is_speaking():
                self._bargein.reset()
                return
            if self._bargein.feed(chunk):
                logger.info("barge-in detected — stopping playback")
                loop = self._loop
                if loop is not None:
                    loop.call_soon_threadsafe(self._speaker.stop)
                else:
                    self._speaker.stop()
        except Exception:  # noqa: BLE001 — a monitor failure must not kill the audio thread
            logger.exception("barge-in monitor failed")

    def _on_transcription_cost(self, cost_usd: float) -> None:
        # Fires synchronously inside stop_and_flush(), which
        # _finish_utterance() awaits via asyncio.to_thread — so by the time
        # that await returns, this has already run.
        self._pending_cost_usd += cost_usd

    def _on_fallback(self, reason: str) -> None:
        logger.warning("voice transcription fell back to offline mode: %s", reason)
        # requirement 6: tell the user plainly, never degrade silently.
        self._speaker.speak("I lost the connection, so I'm using offline transcription instead.")

    # ------------------------------------------------------------------
    # asyncio-side orchestration.
    # ------------------------------------------------------------------

    async def _finish_utterance(self) -> None:
        try:
            # requirement 5 (fast release): stop_and_flush sends Finalize and
            # collects the remaining is_final segments (returning ~finalize_quiet_s
            # after the last one, hard-capped by flush_timeout_s) — it does NOT
            # send CloseStream or tear the socket down; the socket stays warm
            # for the next press. Run off the event loop thread since it blocks
            # on real network I/O.
            transcript = (
                await asyncio.to_thread(self._transcriber.stop_and_flush, self.config["flush_timeout_s"])
            ).strip()
            if self._timings is not None:
                # Mark the hand-off point and print the per-stage report
                # (requirement 1) whether or not there's anything to run.
                self._timings.mark(perf.DISPATCHED)
                logger.info("%s", self._timings.report())
            if not transcript:
                logger.info("empty transcript — nothing to do")
                return
            logger.info("heard: %r", transcript)
            title = (transcript[:60] + "...") if len(transcript) > 60 else transcript
            outcome = await run_task(title, transcript)
            db.log_event(
                outcome["task_id"],
                tool_call=_TRANSCRIPTION_TOOL_CALL,
                args={"model": self.config["deepgram_model"], "language": self.config["deepgram_language"]},
                result=redact_secrets({"transcript": transcript}),
                cost_usd=self._pending_cost_usd,
            )
            answer = str(outcome["result"])
            logger.info("[%s] %s", outcome["status"], answer)
            self.state = "speaking"
            self._speaker.speak(answer)
        finally:
            self._pending_cost_usd = 0.0
            self._timings = None
            self.state = "idle"
            self._busy = False
