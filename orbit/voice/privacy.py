"""User-facing privacy copy for the voice runtime — requirement 8 of the
Deepgram migration. No Settings screen, home-screen mockup, or orb
animation asset exists anywhere in this project (confirmed by a full-repo
search before this was written), so there's nothing to edit in place; this
module exists so the correct copy is defined as real, importable data, and
ready to paste in as soon as a Settings screen exists, rather than left as
prose in a doc that's easy to skip past.

The old (Moonshine-era) claim — "runs on this machine, audio never leaves
your laptop" — is now false and must not ship anywhere: speech-to-text
audio is sent to Deepgram over the network while the hotkey is held.
"""

from __future__ import annotations

PRIVACY_NOTICE = (
    "Voice input: your speech is sent to Deepgram (a third-party "
    "speech-to-text service) for transcription while your microphone is "
    "held. If there's no network connection, transcription happens fully "
    "offline on this machine instead, using a lower-accuracy local model "
    "— you'll be told when that happens. Text-to-speech (the assistant's "
    "spoken replies) is generated entirely locally and never leaves this "
    "machine."
)

# Shorter variant for a settings toggle/tooltip rather than a full screen.
PRIVACY_NOTICE_SHORT = (
    "Speech is sent to Deepgram for transcription (or transcribed locally, "
    "offline, if there's no connection). Spoken replies are generated "
    "locally and never sent anywhere."
)

# Rolling pre-buffer disclosure (requirement 3). Shown next to the
# pre_buffer_enabled toggle: the microphone stays open even when you're not
# pressing the key. Nothing leaves the ring buffer, but on a shared machine
# the user should be able to see and turn this off deliberately.
PRE_BUFFER_NOTICE = (
    "Fast start (pre-buffer): to catch words you speak the instant you press "
    "the key, the microphone stays open continuously and the last half-second "
    "of audio is kept in a small local buffer that is constantly overwritten. "
    "This audio is never sent anywhere unless you actually press the hotkey, "
    "and never leaves this machine. On a shared computer you can turn this off "
    "— the microphone will then open only while the key is held."
)

# Barge-in disclosure. Shown next to the barge_in_enabled toggle: to let you
# talk over a spoken reply, the mic is monitored locally while it's speaking.
BARGE_IN_NOTICE = (
    "Talk to interrupt: while Orbit is speaking a reply, it listens for you "
    "talking over it and stops so you can cut in. This check happens entirely "
    "on this machine — nothing is transcribed or sent anywhere, it only decides "
    "whether to stop the reply. Like fast-start above, it keeps the microphone "
    "open while Orbit is running; turn it off to have the mic open only while "
    "the hotkey is held."
)
