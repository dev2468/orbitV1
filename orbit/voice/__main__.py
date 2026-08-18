"""python -m orbit.voice — starts the voice runtime and keeps it listening
until Ctrl+C. Hold the configured hotkey (default ctrl+space,
orbit/config/voice.yaml) to talk; works with this terminal window
unfocused or minimized, since the hotkey is a global OS-level hook, not a
window event.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from orbit.voice.runtime import VoiceRuntime


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    runtime = VoiceRuntime()
    await runtime.start()
    print("\nVoice runtime ready. Hold the hotkey to talk. Ctrl+C here to quit.\n")
    try:
        while True:
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
