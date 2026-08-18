"""Barge-in detection — let the user cut off a spoken reply by talking over
it, hands-free (no hotkey), while it is speaking.

Scope (deliberately minimal, per the design decision): this ONLY decides
"the user has started talking while she's speaking". The action it triggers is
just `VoiceSpeaker.stop()` — it does NOT transcribe, capture a new command, or
touch the network. So there is zero Deepgram cost and no "when did they stop
speaking" endpointing problem; the user still presses the hotkey to give the
next command.

Why a local energy VAD and not silero/webrtcvad: the hard part here is echo,
not speech-vs-noise. While she speaks through laptop speakers, the microphone
hears HER — and a speech-classifier VAD would flag her own voice as a barge-in
(it *is* speech). The only lever without acoustic echo cancellation is
loudness: the user talking into the laptop mic is louder than the speaker
bleed. So we track an adaptive baseline of the bleed level during a short
start-grace and fire only on a sustained jump above it. On headphones the
bleed is ~zero, so the baseline sits near silence and any real speech trips it
easily — the same code just works better. Thresholds are tunable in voice.yaml
because the speaker case is inherently device/volume dependent.

`feed()` is called from PortAudio's callback thread, so it must stay cheap
(one RMS over the block). It latches after firing and stays quiet until
`reset()` — the runtime resets it whenever `is_speaking()` is false, so each
spoken reply gets a fresh detector.
"""

from __future__ import annotations

import math

import numpy as np


class BargeInDetector:
    def __init__(
        self,
        *,
        samplerate: int = 16000,
        blocksize: int = 512,
        energy_ratio: float = 3.0,
        min_ms: int = 300,
        start_grace_ms: int = 350,
        abs_floor: float = 0.01,
        baseline_attack: float = 0.1,
        baseline_decay: float = 0.05,
    ) -> None:
        block_ms = (blocksize / samplerate) * 1000.0 if blocksize else 32.0
        self._need_hot = max(1, math.ceil(min_ms / block_ms))
        self._grace_blocks = max(0, math.ceil(start_grace_ms / block_ms))
        self._ratio = energy_ratio
        self._abs_floor = abs_floor
        self._grace_alpha = baseline_attack   # settle quickly onto the bleed level
        self._quiet_alpha = baseline_decay    # track slow bleed drift when quiet
        self._baseline: float = 0.0
        self._have_baseline = False
        self._hot = 0
        self._blocks = 0
        self._fired = False

    def reset(self) -> None:
        self._baseline = 0.0
        self._have_baseline = False
        self._hot = 0
        self._blocks = 0
        self._fired = False

    @staticmethod
    def _rms(chunk: np.ndarray) -> float:
        if chunk.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))

    def feed(self, chunk: np.ndarray) -> bool:
        """Return True exactly once, when sustained speech above the adaptive
        floor is seen (i.e. the user has barged in). Stays False afterwards
        until reset()."""
        if self._fired:
            return False
        rms = self._rms(chunk)
        self._blocks += 1
        if not self._have_baseline:
            self._baseline = rms
            self._have_baseline = True
            return False
        # Start-grace: let the baseline settle onto the echo/bleed level before
        # any trigger is allowed, so we don't fire on the reply's own onset.
        if self._blocks <= self._grace_blocks:
            self._baseline = (1 - self._grace_alpha) * self._baseline + self._grace_alpha * rms
            self._hot = 0
            return False
        threshold = max(self._baseline * self._ratio, self._abs_floor)
        if rms > threshold:
            self._hot += 1
            if self._hot >= self._need_hot:
                self._fired = True
                return True
        else:
            self._hot = 0
            self._baseline = (1 - self._quiet_alpha) * self._baseline + self._quiet_alpha * rms
        return False
