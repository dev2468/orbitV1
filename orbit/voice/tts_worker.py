"""Kokoro TTS synthesis worker — runs under venv_tts (Python 3.11), NOT
this project's own venv (3.13), because misaki[en] (Kokoro's G2P
dependency) has never published a version supporting Python 3.13+. Talks
JSON Lines over stdin/stdout to the parent process (orbit/voice/tts.py).
Stateless per request: text in, a WAV file path out. Playback and
interruption both live in the parent — this process only ever synthesizes.

Deliberately does NOT use kokoro.KPipeline's default English G2P wiring.
KPipeline.__init__ (read directly from the installed package — see
voice.yaml's tts_disable_espeak_fallback comment) tries to construct
misaki.espeak.EspeakFallback for English and only silently falls back to
fallback=None if espeak-ng's native library isn't found on the machine.
That's exactly the "activates only on rare/environmental conditions, easy
to miss later" trap the GPL-avoidance requirement called out. This always
constructs the G2P itself with an explicit fallback (below), so an
unrelated future espeak-ng install on this machine can never silently
reintroduce it.

fallback=None on its own turned out not to be good enough: verified live
(2026-08-13) that words missing from misaki's dictionary — "Flipkart",
"Croma", both real keyterms — get phonemes=None and are silently DROPPED
from the synthesized audio entirely (confirmed by inspecting
G2P.__call__'s token output directly, and by feeding the resulting audio
through two independent STT engines that both correctly transcribed
nothing there, because nothing was said). That's silence, not "best-effort
pronunciation" — worse than the requirement asked for. _SpellOutFallback
below spells an unknown word letter-by-letter using misaki's own
dictionary entries for the ENGLISH LETTER NAMES ("eff", "ell", "eye", ...)
— still zero espeak-ng, zero network, zero new dependency, just reusing
words the lexicon already knows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # belt-and-suspenders; see redirect below

_SAMPLE_RATE = 24000  # Kokoro's fixed output rate (confirmed in kokoro/pipeline.py)

_LETTER_NAMES = {
    "a": "ay", "b": "bee", "c": "see", "d": "dee", "e": "ee", "f": "eff",
    "g": "jee", "h": "aitch", "i": "eye", "j": "jay", "k": "kay", "l": "ell",
    "m": "em", "n": "en", "o": "oh", "p": "pee", "q": "cue", "r": "are",
    "s": "ess", "t": "tee", "u": "you", "v": "vee", "w": "double you",
    "x": "ex", "y": "why", "z": "zee",
}


class _SpellOutFallback:
    """Best-effort OOV pronunciation, no espeak-ng: spells an unknown word
    letter-by-letter, looking each letter's NAME up in the G2P's own
    dictionary (words it already knows, like NIDRA gets partly spelled out
    through misaki's own all-caps handling — this generalizes that idea to
    any word the dictionary and stem-based rules can't otherwise resolve).
    Crude ("Flipkart" -> eff-ell-eye-pee-kay-are-tee) but audible, which is
    the actual requirement: never silently say nothing for a real word.
    """

    def __init__(self, g2p) -> None:
        self._g2p = g2p

    def __call__(self, token):
        letters = [c for c in token.text if c.isalpha()]
        if not letters:
            return None, None
        parts = []
        for letter in letters:
            name = _LETTER_NAMES.get(letter.lower())
            if not name:
                continue
            ps, _rating = self._g2p.lexicon.get_word(name, None, None, None)
            if ps:
                parts.append(ps)
        if not parts:
            return None, None
        return " ".join(parts), 3  # lower confidence than a real dictionary hit (4)

# The JSON-lines protocol needs stdout to carry ONLY our own messages, but
# huggingface_hub/torch/etc. print stray warnings straight to stdout (not
# through the `warnings` module, which would go to stderr) — confirmed
# empirically: "WARNING: Defaulting repo_id to hexgrad/Kokoro-82M..." showed
# up as the first line of stdout during real testing and broke the parent's
# ready-handshake read. Redirecting sys.stdout to stderr for the WHOLE
# process lifetime, before any third-party import happens, means no
# library call — at init or at any later synthesis call — can ever land on
# the real stdout; only _emit() below (writing to the saved handle
# directly) can.
_real_stdout = sys.stdout
sys.stdout = sys.stderr


def _emit(obj: dict) -> None:
    print(json.dumps(obj), file=_real_stdout, flush=True)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _build_pipeline(lang_code: str = "a"):
    from kokoro import KPipeline
    from misaki import en as misaki_en

    pipeline = KPipeline(lang_code=lang_code)
    # Explicit override — see module docstring. fallback is _SpellOutFallback,
    # never espeak.EspeakFallback.
    g2p = misaki_en.G2P(trf=False, british=(lang_code == "b"), fallback=None, unk="")
    g2p.fallback = _SpellOutFallback(g2p)
    pipeline.g2p = g2p
    return pipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="af_heart")
    args = parser.parse_args()

    try:
        import numpy as np
        import soundfile as sf

        pipeline = _build_pipeline()
    except Exception:
        _emit({"ok": False, "event": "ready", "error": traceback.format_exc()})
        return 1

    _emit({"ok": True, "event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("cmd") == "shutdown":
            break

        text = req.get("text", "")
        voice = req.get("voice") or args.voice
        if not text.strip():
            _emit({"ok": False, "error": "empty text"})
            continue

        try:
            chunks = []
            for result in pipeline(text, voice=voice):
                if result.audio is not None:
                    chunks.append(result.audio.numpy())
            if not chunks:
                _emit({"ok": False, "error": "no audio produced"})
                continue
            audio = np.concatenate(chunks)
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="orbit_tts_")
            os.close(fd)
            sf.write(path, audio, _SAMPLE_RATE)
            _emit({"ok": True, "wav_path": path, "duration_s": len(audio) / _SAMPLE_RATE})
        except Exception:
            _log(traceback.format_exc())
            _emit({"ok": False, "error": "synthesis failed, see stderr"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
