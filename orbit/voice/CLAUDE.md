# orbit/voice/ — pipeline and threading model

hotkey → capture → transcribe → `run_task()` → speak. It reuses `run_task()` exactly as-is, so a
voice task takes the same task/lane/safety path as every other caller. There is no parallel
execution route, and adding one would bypass the safety layer.

**The threading model is the most important thing in this directory.** Read this section before
editing anything here.

## Three threads that are not asyncio-aware

1. **pynput's listener thread** — a raw low-level OS keyboard hook. `_on_hotkey_press` and
   `_on_hotkey_release` run here.
2. **PortAudio's callback thread** — `_callback` in `capture.py`, and therefore
   `_on_audio_chunk` → `feed_audio`, run here.
3. **Deepgram's own websocket thread** — `DeepgramTranscriber` spawns it and runs a private
   `asyncio.run()` loop inside it, so callers never touch asyncio. `on_partial`/`on_final` fire
   from there.

**The runtime's single asyncio crossing point** is
`asyncio.run_coroutine_threadsafe(self._finish_utterance(), self._loop)` on hotkey release.
Everything else the runtime does — capture start/stop, the daily-cap check, `speaker.stop()` — is a
plain thread-safe call made directly on whichever thread triggered it.

`DeepgramTranscriber` owns a **second, independent** asyncio loop on its own persistent connection
thread (see the STT section). `feed_audio` and the `Finalize`/drain controls cross into *that* loop
via `loop.call_soon_threadsafe(queue.put_nowait, …)` onto an `asyncio.Queue` — chosen over the old
`run_in_executor(queue.get)` precisely because it is natively cancellable, so a background reconnect
cannot leak a blocked executor thread. These two loops never touch each other; the transcriber
crossing is entirely inside `transcriber.py`. Adding a crossing anywhere else is a design change.

### The rule that follows

**Never do blocking or slow work on the hook thread or the audio callback thread.**

- Windows silently unhooks a low-level keyboard hook that overruns its timeout. The hotkey then
  stops working with no error anywhere — you get a dead hotkey, not an exception.
- Blocking the PortAudio callback drops the stream: audio is lost for the duration, and the
  utterance is silently truncated.

Both callback layers already swallow-and-log exceptions for this reason (`_handle_press`,
`_handle_release`, `capture._callback`, `_on_audio_chunk`) — a raising callback would kill the
thread outright. Keep that. The heavy work — flush, `run_task()`, synthesis — is why
`_finish_utterance` hops to the loop and then uses `asyncio.to_thread` for the blocking flush.

`pynput.keyboard.Listener` is used rather than `GlobalHotKeys` because push-to-talk needs both
edges; `GlobalHotKeys` fires once on combo press with no notion of held or released.

## `_busy` guards re-entrancy — and every path that sets it must reset it

`_busy` is set the instant capture actually starts, not when `run_task()` begins, so a press
arriving before the release-triggered coroutine is even scheduled still sees the guard up. It is
reset in `_finish_utterance`'s `finally`, **or immediately** in the `AudioCaptureError` branch where
capture never started. Any new early-return between those two points must reset it, or the hotkey
wedges permanently with no error.

`_on_hotkey_release` returns early when `self._capture.is_active` is false — that is how a rejected
press (busy, or over the cost cap) avoids scheduling a flush for an utterance that never happened.

## TTS: Kokoro, not Piper — do not swap back

The actively-maintained `piper-tts` PyPI package relicensed to **GPL-3.0-or-later in Oct 2025**
(repo moved to OHF-Voice/piper1-gpl; its phonemizer is a bundled compiled espeak-ng bridge, no
longer swappable). The only MIT Piper is the archived, unmaintained original, and even that depended
on a GPL espeak-ng wrapper. "Keep Piper, swap in an MIT G2P" is not achievable. Kokoro-82M is
Apache-2.0 for both weights and code.

**Kokoro runs in an isolated Python 3.11 subprocess** (`venv_tts/`, `tts_worker.py`) because
`misaki[en]` — Kokoro's G2P dependency — has never published a version supporting Python 3.13+,
which this project's venv is. The worker is stateless JSON-lines over stdio: text in, a WAV path
out. It never plays audio; playback and interruption live in the parent.

`tts_worker.py` also never uses `KPipeline`'s default G2P wiring, because `KPipeline.__init__` tries
to construct `misaki.espeak.EspeakFallback` and only *silently* no-ops when espeak-ng's native
library is absent — so an unrelated future espeak-ng install would quietly pull a GPL path back in.
It builds the G2P explicitly instead. `fallback=None` alone was not enough: verified live that
out-of-dictionary words ("Flipkart", "Croma" — both real keyterms) get `phonemes=None` and are
**silently dropped from the audio entirely**. That is silence, not best-effort pronunciation.
`_SpellOutFallback` spells them letter-by-letter using misaki's own entries for the English letter
names — crude but audible, and still zero espeak-ng.

The worker redirects `sys.stdout` to stderr process-wide *before any third-party import*, so no
library warning can land on the real stdout and corrupt the JSON-lines handshake. Only `_emit`,
writing to the saved handle, may use it. Do not print to stdout anywhere in that file.

## tts.py: raw OutputStream + abort(), never sd.play()/sd.stop()

`sd.stop()` is a graceful stop, and it was measured **not** to reliably unblock playback —
`is_speaking()` stayed true 5+ seconds after the call. A raw `sd.OutputStream` with an explicit
`finished_callback`, aborted with `stream.abort()` (immediate, not graceful), unblocks within
milliseconds. Same "own the stream directly" pattern `capture.py` uses for input.

**In `_play`, `_current_stream` must be assigned before `_speaking.set()`, never after.**
`sd.OutputStream()` construction is measurably slow (PortAudio device negotiation), so setting
`_speaking` first opens a real race: `stop()` on another thread sees `is_speaking() == True`, reads
`_current_stream` as still `None`, and silently no-ops instead of aborting. The interrupt just
does not happen.

Mute-while-speaking and interruption are the same mechanism, not two: push-to-talk only captures
while the key is held, so `_on_hotkey_press` calling `self._speaker.stop()` unconditionally — before
any busy or cap check — is the whole design. Keep it unconditional and keep it first.

**`speak()` is fire-and-forget.** It queues text and returns immediately; playback happens on
`VoiceSpeaker`'s own worker thread. So in `_finish_utterance` the `self.state = "speaking"` line is
followed almost immediately by the `finally` resetting it to `idle` — **`self.state` is NOT a
reliable "audio is playing" signal.** The reliable one is `self._speaker.is_speaking()` (the
`_speaking` Event set/cleared around the actual `sd.OutputStream`). Anything that needs to act only
while she is really talking (barge-in) must gate on `is_speaking()`, never on `state`. A corollary:
because `_busy` is also cleared as soon as `_finish_utterance` returns, a hotkey press *during*
playback is not "busy" — it stops her and starts a new capture in one press.

## Barge-in: talk over a reply to cut it off (`bargein.py`)

Hands-free interruption of a *spoken* reply, deliberately scoped to the minimum: a local energy VAD
(`BargeInDetector`) runs on the mic and, while `is_speaking()`, fires on sustained speech above an
adaptive floor — the runtime then dispatches `speaker.stop()`. It does **not** transcribe, capture a
new command, cancel a running task, or touch the network; the user still presses the hotkey for the
next command. So there is zero Deepgram cost and no endpointing problem.

The hard part is **echo, not speech detection**: through laptop speakers the mic hears her own reply,
and a speech-classifier VAD (silero/webrtcvad) would flag that as a barge-in because it *is* speech.
The only lever without acoustic echo cancellation is loudness — so the detector tracks an adaptive
baseline of the bleed level during a short `start_grace_ms` and fires only on a sustained jump above
`baseline * energy_ratio` (floored by `abs_floor`). On headphones the bleed is ~zero, the baseline
sits near silence, and the same code trips easily. That is why the thresholds are tunable in
`voice.yaml` and biased conservative by default (fire late rather than cut her off on bleed).

Wiring: `capture.py` gained an `on_monitor` callback that fires for **every** block while the stream
is open (independent of `_recording`); `monitor_enabled` forces the stream continuously open the
same way `pre_buffer_enabled` does (both set `_continuous`). `runtime._on_monitor_chunk` runs on the
PortAudio thread, resets the detector whenever `is_speaking()` is false (so each reply gets a fresh
detector), and dispatches `stop()` via `loop.call_soon_threadsafe` to keep the abort off the audio
thread. The detector latches after firing until `reset()`, so one barge-in is one stop.

## STT: Deepgram over the raw wire protocol

`DeepgramTranscriber` talks Deepgram's documented WebSocket protocol via `websockets` directly —
**not `deepgram-sdk`**. The SDK's Python API has reshaped incompatibly across its major versions;
the wire protocol underneath (JSON query string, binary audio frames, JSON control/result messages)
is far more stable and small enough to own.

**Moonshine was removed for accuracy on accented English — a model-capacity problem, not a
streaming-architecture one. It must not come back.** `faster-whisper` (`small`) is the offline
fallback only, not a benchmarked equal.

`speech_final` and endpointing are never read or configured: push-to-talk's key release *is* the
utterance boundary. Only `is_final` matters. A single sentence produces several `is_final` segments
(verified: an 11s sentence gave 3), so they are concatenated in arrival order —
keeping only the latest silently drops most of what was said.

### The socket is persistent and pre-warmed (Aug-2026 latency work)

The socket is opened **once**, at construction (`warm_up()`, kicked off from `__init__` and so at
runtime start), and held open for the whole session. This is the biggest latency fix: a hotkey press
used to open a fresh socket and block up to `connect_timeout+1`s on the DNS+TLS+WS handshake — from
India that cold handshake *is* the "press, pause, then speak" symptom. Now:

- A background daemon thread runs the connection: it connects, sends `{"type":"KeepAlive"}` every
  `keepalive_interval_s` (Deepgram closes an idle socket after ~10s, NET-0001), and on any drop
  reconnects immediately (a previously-good socket) or with exponential backoff (a failing connect,
  reset on success). KeepAlive is a control frame — **not** billed and produces no Results, so it is
  never counted as audio or logged as a transcription event.
- `start_stream()` no longer opens anything: it marks the utterance active on the warm socket and
  returns instantly. If the socket happens to be mid-reconnect it raises `TranscriberConnectError`
  so `ResilientTranscriber` falls back to offline for *that* utterance while the background thread
  keeps warming — it never blocks the hotkey thread waiting to connect.
- `stop_and_flush` (**fast release**, requirement 5) sends `Finalize`, collects the remaining
  `is_final` segments, and returns `finalize_quiet_s` after the last one (hard-capped by
  `flush_timeout_s`). It does **not** send `CloseStream`, wait for `Metadata`, or tear the socket
  down — all of which were extra round trips at ~250ms+ each. The socket stays warm for the next
  press. On a high-RTT link watch the `Finalize -> last is_final` timing (below): if the tail clips,
  raise `flush_timeout_s`/`finalize_quiet_s`.

`ResilientTranscriber` buffers the utterance's raw audio so a connect failure *or* a mid-utterance
drop replays the same audio to the fallback rather than making the user repeat themselves. The
fallback re-transcribes from scratch and **supersedes** anything the primary already emitted — a
future live-rendering UI must replace its displayed transcript when `on_fallback` fires, not append.
A fallback switch calls `primary.cancel_utterance()` (abandon this utterance, **keep the socket
warm**), never `primary.close()` — closing the persistent socket on a single drop would kill
pre-warming for the rest of the session. Only runtime teardown (`close()`) tears it down.

### Instrumentation and pre-buffer

`instrumentation.UtteranceTimings` records per-utterance stage boundaries and prints them when
`perf_logging` is on (requirement 1): hotkey-down → ws-ready → first-frame-sent → first-interim,
and release → Finalize-sent → last-is_final → transcript-dispatched. The transcriber reports its
marks through the `on_mark` callback (`load_transcriber(..., on_mark=…)`), which the runtime routes
into the current utterance's timings; the runtime marks the hotkey/dispatch boundaries itself. This
is the "instrument first, optimize second" tool — and the way to tell whether a stage is already at
the India→US network floor (~200-300ms each way) and should be left alone.

`capture.py`'s **rolling pre-buffer** (requirement 3): when `pre_buffer_enabled`, the mic is held
open continuously and the last `pre_buffer_ms` sit in a ring buffer that is never transmitted, only
prepended to the stream on hotkey-down — so speech that starts *as* the key is pressed survives.
Because that keeps the mic open the whole session it is a deliberate, visible setting (`voice.yaml`,
`privacy.PRE_BUFFER_NOTICE`), off-switchable for a shared machine. `is_active` now means "recording
an utterance", not "the stream object exists", so the runtime's press/release/flush logic is
identical in both modes. Capture frames are `capture_blocksize` samples (default 512 = 32ms, inside
requirement 4's 20-50ms band).

The daily cost cap is checked **before** starting a transcription, not after, so it actually stops
spend. `state` (idle/listening/thinking/speaking) and `on_partial` are exposed for a UI that does
not exist yet — nothing currently subscribes to either.
