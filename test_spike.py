import asyncio

from google.adk.runners import InMemoryRunner

from spike_agent.agent import root_agent


async def main():
    runner = InMemoryRunner(agent=root_agent)
    await runner.run_debug(
        "Go to https://example.com and tell me the exact page title and the "
        "first sentence of body text you see.",
        verbose=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
