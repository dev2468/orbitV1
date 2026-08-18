"""Text-to-speech out. Kokoro-82M (Apache-2.0) via an isolated Python 3.11
subprocess (tts_worker.py, venv_tts/) — NOT Piper. The actively-maintained
piper-tts PyPI package relicensed to GPL-3.0-or-later in Oct 2025 (repo
moved to OHF-Voice/piper1-gpl, phonemizer no longer swappable); the only
MIT-licensed Piper is the archived, unmaintained original, and even that
depended on a GPL espeak-ng wrapper. Do not "helpfully" swap back to Piper
without re-reading that history. Not moonshine-voice either — that whole
build was replaced by this Deepgram/Kokoro migration.

Why a subprocess, not an in-process import: Kokoro's G2P dependency
(misaki[en]) has never published a version supporting Python 3.13+, and
this project's own venv is 3.13. venv_tts/ is a second, isolated Python
3.11 environment holding only kokoro+misaki+soundfile; tts_worker.py runs
inside it as a long-lived, stateless synthesis worker (JSON-lines over
stdin/stdout: text in, a WAV file path out). Playback and interruption
both live HERE, in the parent, via sounddevice directly (already a
dependency for capture.py) — the subprocess never plays audio itself.

Mic-muting and interruption reduce to the same one rule the original
(Moonshine) build established: push-to-talk only ever captures while the
hotkey is held, so "mute capture while speaking" is automatically true
UNLESS the hotkey is pressed WHILE speech is playing — and "interruptible
by hotkey" is exactly that same moment. runtime.py enforces this with one
call: always stop() the speaker before starting capture on hotkey-press.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger("orbit.voice.tts")

_VENV_TTS_PYTHON = Path(__file__).resolve().parent.parent.parent / "venv_tts" / "Scripts" / "python.exe"
_WORKER_SCRIPT = Path(__file__).resolve().parent / "tts_worker.py"


class VoiceSpeaker:
    def __init__(
        self,
        *,
        voice: str = "af_heart",
        output_device: Optional[object] = None,
        enabled: bool = True,
        python_exe: Optional[Path] = None,
    ) -> None:
        self.enabled = enabled
        self._voice = voice
        self._output_device = output_device
        self._python_exe = python_exe or _VENV_TTS_PYTHON
        self._proc: Optional[subprocess.Popen] = None
        self._request_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._loaded = False
        self._speaking = threading.Event()
        self._current_stream: Optional[sd.OutputStream] = None

    def load(self) -> None:
        """Blocking — spawns the venv_tts subprocess and waits for its
        model-load handshake. May be slow on first run (Kokoro/spacy
        download assets from Hugging Face / PyPI on first use). Safe to
        call even when disabled, so toggling on later doesn't need a
        reload. Logs an error and leaves TTS silent (never raises) if
        venv_tts wasn't set up — see this module's docstring."""
        if self._loaded:
            return
        if not self._python_exe.exists():
            logger.error(
                "venv_tts Python not found at %s — TTS will be silent. "
                "See orbit/voice/tts.py's module docstring for setup.",
                self._python_exe,
            )
            return

        self._proc = subprocess.Popen(
            [str(self._python_exe), str(_WORKER_SCRIPT), "--voice", self._voice],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._pump_stderr, daemon=True, name="kokoro-stderr").start()

        ready_line = self._proc.stdout.readline() if self._proc.stdout else ""
        try:
            msg = json.loads(ready_line) if ready_line else {}
        except ValueError:
            msg = {}
        if not msg.get("ok"):
            logger.error("Kokoro TTS worker failed to start: %s", msg.get("error", ready_line))
            self._proc = None
            return

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="kokoro-tts")
        self._worker_thread.start()
        self._loaded = True

    def _pump_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            logger.debug("[tts_worker] %s", line.rstrip())

    def speak(self, text: str) -> None:
        """Fire-and-forget: queues text for the background worker thread
        and returns immediately. No-ops when disabled or never loaded —
        callers don't need to check `.enabled` themselves."""
        if not self.enabled or not text.strip() or not self._loaded:
            return
        self._request_queue.put(text)

    def _worker_loop(self) -> None:
        while True:
            text = self._request_queue.get()
            if text is None:
                self._request_queue.task_done()
                return
            wav_path = self._synthesize(text)
            if wav_path is not None:
                self._play(wav_path)
            self._request_queue.task_done()

    def _synthesize(self, text: str) -> Optional[Path]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            return None
        try:
            self._proc.stdin.write(json.dumps({"text": text, "voice": self._voice}) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except (BrokenPipeError, OSError):
            logger.error("Kokoro TTS worker process is gone — cannot speak")
            return None
        if not line:
            logger.error("Kokoro TTS worker closed its output unexpectedly")
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            logger.error("Kokoro TTS worker sent an unparseable response: %r", line)
            return None
        if not msg.get("ok"):
            logger.error("Kokoro synthesis failed: %s", msg.get("error"))
            return None
        return Path(msg["wav_path"])

    def _play(self, wav_path: Path) -> None:
        # NOT sd.play()/sd.stop()/sd.wait(): measured empirically that
        # sd.stop() (which calls stream.stop(), a GRACEFUL stop) does not
        # reliably unblock a concurrent sd.wait() in this sounddevice
        # version — is_speaking() stayed True 5+ seconds after stop() was
        # called in real testing. A raw OutputStream + explicit
        # finished_callback, stopped with stream.abort() (immediate, not
        # graceful), was confirmed in isolation to unblock within
        # milliseconds — same "own the stream directly" pattern
        # capture.py already uses for input.
        try:
            with wave.open(str(wav_path), "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            samples = (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0).reshape(-1, 1)

            done = threading.Event()
            position = [0]

            def _callback(outdata, frames, time_info, status) -> None:
                if status:
                    logger.warning("TTS playback status flags: %s", status)
                remaining = len(samples) - position[0]
                if remaining <= 0:
                    outdata[:] = 0
                    raise sd.CallbackStop
                n = min(remaining, frames)
                outdata[:n] = samples[position[0]:position[0] + n]
                if n < frames:
                    outdata[n:] = 0
                position[0] += n
                if n < frames:
                    raise sd.CallbackStop

            def _finished() -> None:
                done.set()

            # Order matters: sd.OutputStream() construction is measurably
            # slow (PortAudio device negotiation) — confirmed empirically
            # that setting _speaking before _current_stream is assigned
            # opens a real race where stop() (on another thread) can see
            # is_speaking()==True, read _current_stream as still None, and
            # silently no-op instead of aborting. _current_stream must be
            # set BEFORE _speaking, never after.
            stream = sd.OutputStream(
                samplerate=sr, channels=1, dtype="float32", device=self._output_device,
                callback=_callback, finished_callback=_finished,
            )
            self._current_stream = stream
            self._speaking.set()
            stream.start()
            done.wait()
            stream.close()
        except Exception:  # noqa: BLE001 — playback failure must not kill the worker thread
            logger.exception("TTS playback failed")
        finally:
            self._current_stream = None
            self._speaking.clear()
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

    def is_speaking(self) -> bool:
        """Accurate, unlike the retired Moonshine wrapper's documented-buggy
        equivalent: we own the whole play loop directly via sounddevice, so
        this is a plain Event set/cleared around the actual sd.play()/wait()
        call, not a queue-emptiness proxy."""
        return self._speaking.is_set()

    def wait(self) -> None:
        """Blocks until everything queued via speak() has been synthesized
        and played (queue.Queue's task_done()/join() contract)."""
        self._request_queue.join()

    def stop(self) -> None:
        """Interrupt: halts current playback immediately and drops
        anything still queued. Safe to call even when nothing is playing."""
        while True:
            try:
                self._request_queue.get_nowait()
                self._request_queue.task_done()
            except queue.Empty:
                break
        stream = self._current_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                logger.exception("error aborting TTS playback stream")

    def close(self) -> None:
        if not self._loaded:
            return
        self.stop()
        self._request_queue.put(None)
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
            self._proc = None
        self._loaded = False
