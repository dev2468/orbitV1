import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

load_dotenv()

# Temporarily on Groq while the DeepSeek account balance is topped up.
# Revert to LiteLlm(model="deepseek/deepseek-v4-flash") once that's sorted.
if not os.environ.get("GROQ_API_KEY"):
    raise RuntimeError(
        "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in."
    )

playwright_mcp = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@playwright/mcp@latest"],
        ),
        timeout=30,
    ),
    # Curated subset for the spike, per Section 6: the model gets a small
    # skill-shaped surface, not the full ~24-tool raw Playwright MCP list.
    tool_filter=["browser_navigate", "browser_snapshot", "browser_evaluate"],
)

root_agent = Agent(
    name="spike_agent",
    model=LiteLlm(model="groq/llama-3.3-70b-versatile", drop_params=True),
    description="Section 2 spike: DeepSeek via LiteLLM, driving Playwright MCP.",
    instruction=(
        "You are a browser research assistant. Use the Playwright browser "
        "tools to navigate to pages and answer the user's question based on "
        "what you observe. Always report exactly what you found on the page."
    ),
    tools=[playwright_mcp],
)
