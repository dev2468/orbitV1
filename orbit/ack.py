"""The acknowledgement track — Orbit's fast first reply.

A goal submitted to the agent takes seconds before it says anything: MCP
toolsets connect (~3.3s) and the first model turn carries an ~8,000-token
prompt with 44 tool declarations. That is fine for *doing the work* and
hopeless for *sounding like an assistant*, because a person who just spoke
gets silence for the length of a long pause in conversation.

So a spoken request now drives two tracks at once:

    transcript ─┬─▶ ACK   (this module)     one short line, ~1.1s, spoken
                └─▶ WORK  (orbit.run_task)  the actual task, tools and all

This module is the first one. It answers a different question from the agent
— not "what should I do about this" but "what did I just hear" — so it needs
none of the agent's machinery: no tools, no MCP servers, no ADK runner, and a
prompt of a few hundred tokens instead of eight thousand.

## Why this talks to OpenRouter over raw HTTP instead of through LiteLLM

Because the GUI process is what calls it, and `import litellm` measured 8.7s
(google-adk another 1.6s on top). The GUI deliberately keeps to the light end
of this package — `orbit.db`, `orbit.policy` — so that it starts promptly, and
an acknowledgement whose whole purpose is to arrive in about a second cannot
be the thing that adds ten seconds to startup.

LiteLLM earns its keep in `orbit/agent.py`, where it normalises tool-call
shapes across providers and carries the caching and screenshot-injection
seams. None of that applies here: one message in, a stream of text out.
`httpx` is already installed (LiteLLM depends on it) and imports in 0.22s.

**Keep this module dependency-light.** If you find yourself importing litellm,
google-adk, or PySide6 here, the thing you want probably belongs in
`orbit/agent.py` or `gui/speech.py` instead.
"""

from __future__ import annotations

import json
import os
from typing import Iterator, Optional

import httpx

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Its own setting, independent of ORBIT_MODEL. The work track may reasonably
# be pointed at a slow, strong model for a hard task; the acknowledgement must
# stay fast regardless, because its entire value is arriving before the user
# wonders whether anything is happening.
#
# Measured 2026-09-07, streaming, on this prompt shape:
#   gemini-2.5-flash       0.93s to first token, 1.08s complete
#   gpt-4.1-nano           1.05s / 1.25s
#   gemini-2.5-flash-lite  1.28s / 1.41s
#   gemini-3.7-flash       1.67s / 1.92s, and it truncated mid-word — its
#                          mandatory reasoning ate the token budget
DEFAULT_ACK_MODEL = "openrouter/google/gemini-2.5-flash"

# Sent to the provider without the "openrouter/" routing prefix LiteLLM uses.
_PREFIX = "openrouter/"

# The acknowledgement classifies its own turn, and the classification rides in
# the first few characters of the reply rather than in a separate call. One
# request, one latency budget.
#
# The marker is on a ~400-token prompt whose only job is this one line, which
# is why it is acceptable here when the [STEP:*] markers on the agent's
# 8,000-token prompt were not: that protocol was buried among forty other
# instructions and the model routinely dropped it. This one is the first thing
# the prompt asks for.
#
# **Absent or unrecognised means TASK.** The whole design leans on that: a
# missed marker costs a few seconds of work nobody needed, while a wrongly
# confident CHAT means a real request silently does nothing.
MARKER_CHAT = "[CHAT]"
MARKER_TASK = "[TASK]"
_MARKERS = (MARKER_CHAT, MARKER_TASK)

_SYSTEM_PROMPT = (
    "You are Orbit, a voice assistant on the user's Windows desktop. The "
    "user has just spoken to you, and a separate process is already starting "
    "on their request. Your ONLY job is the immediate spoken reply — the "
    "half-second of 'yes, I heard you' that a person gives before they start "
    "working.\n\n"
    "FIRST, classify the turn. Begin your reply with exactly one of:\n"
    "  [CHAT] — the user is ONLY being social or conversational: a greeting, "
    "thanks, small talk, or a question about you that needs no tools, no "
    "files, no browsing and no app. Nothing needs to be done.\n"
    "  [TASK] — anything the assistant has to actually DO or look up, "
    "however small. If you are even slightly unsure, use [TASK].\n"
    "Then a space, then your one spoken sentence. Never explain the marker "
    "and never mention it.\n\n"
    "RULES:\n"
    "- ONE sentence. Under 18 words. It will be read aloud.\n"
    "- Say what you understood and what you are about to do.\n"
    "- NEVER answer the request itself, never guess at results, never list "
    "steps. Something else is doing the work.\n"
    "- Never promise a specific time ('one moment' is fine, 'in five "
    "seconds' is not).\n"
    "- Plain speech only: no markdown, no bullet points, no emoji, no "
    "quotation marks around your reply.\n"
    "- If the user is just being social (a greeting, thanks, small talk), "
    "reply naturally and briefly instead of announcing work.\n"
    "- If the request is genuinely ambiguous, ask the one question that "
    "would resolve it, in the same one sentence.\n"
    "- Use the conversation history to resolve references like 'that one' or "
    "'do it again', so your reply shows you followed."
)


class MarkerSplitter:
    """Peels the leading [CHAT]/[TASK] marker off a streaming reply.

    The reply is streamed straight into the output pane and then to
    text-to-speech, so the marker has to come off *before* either sees it —
    and a stream arrives in arbitrary chunks, so the first delta may be `"["`,
    or `"[TA"`, or the whole sentence at once.

    Hence a state machine rather than a `startswith` check: hold text back only
    while it is still a possible prefix of a marker, and release everything the
    moment that stops being true. The longest thing ever held is six
    characters, which is not a perceptible delay.

    `chat_only` is None until decided, then True/False. Undecided at
    end-of-stream means no marker was sent, which is TASK — see the note on
    MARKER_CHAT.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._decided = False
        self._emitted = False
        self.chat_only: Optional[bool] = None

    def feed(self, delta: str) -> str:
        """Return the part of `delta` that is safe to show/speak."""
        if self._decided:
            # The space between the marker and the sentence can land in a
            # later chunk than the marker itself — it always does when the
            # stream arrives one character at a time — so leading whitespace
            # is suppressed until real text has been emitted. Without this the
            # reply is spoken and rendered with a leading space.
            if not self._emitted:
                delta = delta.lstrip()
                if not delta:
                    return ""
            self._emitted = True
            return delta

        self._buffer += delta
        stripped = self._buffer.lstrip()

        for marker in _MARKERS:
            if stripped.upper().startswith(marker):
                self._decided = True
                self.chat_only = marker == MARKER_CHAT
                out = stripped[len(marker):].lstrip()
                self._buffer = ""
                self._emitted = bool(out)
                return out

        # Still possibly mid-marker? Keep holding.
        upper = stripped.upper()
        if any(m.startswith(upper) for m in _MARKERS) and upper:
            return ""

        # Definitely not a marker — the model skipped it. Release everything
        # and treat the turn as work, which is the safe default.
        self._decided = True
        self.chat_only = False
        out = self._buffer
        self._buffer = ""
        self._emitted = bool(out.strip())
        return out

    def flush(self) -> str:
        """Release anything still held. Call once at end of stream."""
        if self._decided:
            return ""
        self._decided = True
        if self.chat_only is None:
            self.chat_only = False
        out = self._buffer
        self._buffer = ""
        return out


_SPOKEN_SUMMARY_PROMPT = (
    "You turn a completed task's written result into something worth saying "
    "out loud. The user already has the full text in front of them; you are "
    "the spoken version, not a replacement for it.\n\n"
    "RULES:\n"
    "- One or two sentences. Under 40 words total.\n"
    "- Lead with the answer, not with what you did. 'It's twelve degrees and "
    "raining' — not 'I checked the weather site and found that'.\n"
    "- Speech only. No markdown, no bullet points, no URLs, no file paths, no "
    "code, no emoji. Read numbers and units as a person would say them.\n"
    "- Where something was saved, say it in plain words ('on your desktop', "
    "'in your downloads folder'). NEVER read out a path or URL — spoken "
    "aloud, 'C colon slash Users slash HP' is noise.\n"
    "- If the result is a list, say how many and the most useful one or two, "
    "not all of them.\n"
    "- If the task failed, say plainly what failed in one sentence. Do not "
    "apologise at length.\n"
    "- Never invent detail that is not in the result."
)


def stream_spoken_summary(
    answer: str,
    *,
    goal: str = "",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 15.0,
    client: Optional[httpx.Client] = None,
) -> Iterator[str]:
    """Stream a speech-shaped summary of a finished task's result.

    ## Why a second model call instead of asking the agent for one

    The obvious alternative is to have the agent wrap a spoken version in
    `<speech>...</speech>` as part of its answer — no extra call, no extra
    latency. That was the plan and it is the wrong shape, for the reason the
    `[STEP:*]` markers were removed from the same prompt on 2026-09-07: an
    instruction buried in an 8,000-token prompt among forty others is followed
    when the model feels like it, and the failure is silent. Speech that
    sometimes does not happen is worse than speech that costs a second.

    It also solves a problem the tag does not. Agent answers are markdown —
    bullet lists, URLs, file paths, code fences — which is unreadable aloud.
    This produces text written to be *heard*.

    The latency is affordable precisely because it comes last: the user is
    already reading the real answer while this is generated.
    """
    answer = (answer or "").strip()
    if not answer:
        return
    # A very long answer is truncated rather than sent whole. The summary is
    # forty words; the tail of a 50KB browser dump changes nothing about it
    # and costs real tokens on every task.
    if len(answer) > 6000:
        answer = answer[:6000] + "\n[...truncated]"

    content = f"The user asked: {goal}\n\nThe result was:\n{answer}" if goal else answer
    yield from _stream_completion(
        system=_SPOKEN_SUMMARY_PROMPT,
        user=content,
        model=model,
        api_key=api_key,
        timeout=timeout,
        client=client,
        max_tokens=120,
        temperature=0.4,
    )


def _resolve_model(model: Optional[str] = None) -> str:
    name = model or os.environ.get("ORBIT_ACK_MODEL") or DEFAULT_ACK_MODEL
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else name


def recent_context(conversation_id: Optional[str], *, limit: int = 3) -> str:
    """The last few turns of a conversation, as context for the next ack.

    This is what lets the acknowledgement say "I'll check that again in
    Chrome" rather than "I'll do that" — it is the difference between the
    reply sounding like it followed and sounding like a doorbell.

    Kept much shorter than the work track's equivalent
    (`run_task._build_conversation_context`): the ack is one sentence and does
    not need prior results in full, only enough to resolve a pronoun. Results
    are clipped hard for the same reason.

    Returns "" for no conversation, no turns, or any DB error — the ack must
    still work when history is unavailable, just with less to go on. The
    import is local so that this module stays importable (and testable)
    without a database present.
    """
    if not conversation_id:
        return ""
    try:
        from orbit import db

        turns = db.conversation_turns(conversation_id)[-limit:]
    except Exception:  # noqa: BLE001
        return ""
    if not turns:
        return ""
    lines = []
    for turn in turns:
        goal = (turn.get("goal") or "").strip()
        result = (turn.get("result") or "").strip()
        if len(result) > 200:
            result = result[:200] + "..."
        if goal:
            lines.append(f"User: {goal}")
        if result:
            lines.append(f"Orbit: {result}")
    return "\n".join(lines)


def _iter_sse_text(response: httpx.Response) -> Iterator[str]:
    """Yield content deltas from an OpenAI-shaped SSE stream.

    Three things on this wire are easy to trip over and all three are real:
    OpenRouter sends `: OPENROUTER PROCESSING` comment lines as keepalives
    while a model warms up, the stream ends with a literal `data: [DONE]`
    sentinel that is not JSON, and a chunk can carry a `delta` with no
    `content` at all (a role announcement, or a reasoning-only step).
    """
    for line in response.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = (choices[0].get("delta") or {}).get("content")
        except (ValueError, AttributeError, IndexError, TypeError):
            continue
        if delta:
            yield delta


def stream_ack(
    goal: str,
    *,
    history: str = "",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 12.0,
    client: Optional[httpx.Client] = None,
) -> Iterator[str]:
    """Stream the acknowledgement for `goal`, one text delta at a time.

    Deltas rather than a finished string because the caller pipes them
    straight into text-to-speech: `gui/speech.py` synthesises as soon as it
    has a whole sentence, so the first audio does not wait on the last token.

    `client` is injectable so the caller can hand in a connection that is
    already open. That matters more than it looks — the first HTTPS request a
    process makes pays TLS setup, measured at roughly 0.6s of extra latency,
    and paying it on the user's first spoken request is exactly the wrong
    time. See `gui/speech.py`'s prewarm for the same trick on the TTS side.

    Raises on a transport or HTTP error rather than swallowing it: the caller
    decides whether a failed acknowledgement is worth surfacing, and the work
    track is unaffected either way — the two are independent by design, which
    is the point of running them side by side.
    """
    user_content = f"{history}\n\nUser just said: {goal}" if history else goal
    yield from _stream_completion(
        system=_SYSTEM_PROMPT,
        user=user_content,
        model=model,
        api_key=api_key,
        timeout=timeout,
        client=client,
        # 60 is comfortably over the 18-word ceiling the prompt asks for (plus
        # the marker), and low enough that a model ignoring that ceiling is cut
        # off rather than narrating over the top of the work track.
        max_tokens=60,
        temperature=0.6,
    )


def _stream_completion(
    *,
    system: str,
    user: str,
    model: Optional[str],
    api_key: Optional[str],
    timeout: float,
    client: Optional[httpx.Client],
    max_tokens: int,
    temperature: float,
) -> Iterator[str]:
    """The one HTTP path both fast calls share.

    Deliberately carries no `tools` key. Both callers are fast precisely
    because they send none — putting the agent's ~5,900-token tool schema on
    either of these would quietly undo the whole arrangement.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set — add it to the .env file in the "
            "project root."
        )

    body = {
        "model": _resolve_model(model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        with http.stream("POST", _ENDPOINT, json=body, headers=headers) as response:
            response.raise_for_status()
            yield from _iter_sse_text(response)
    finally:
        if owned:
            http.close()
