# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "langchain-openai",
#   "langchain-mcp-adapters",
#   "langgraph",
# ]
# ///
"""Manual test harness: a LangGraph agent with seafile-mcp wired in as its toolset.

Not part of the deployed package — run standalone with `uv run scripts/test_agent.py`,
which installs its own deps (langchain-openai, langchain-mcp-adapters, langgraph)
without touching the server's pyproject.toml.

Example:
    uv run scripts/test_agent.py \\
        --mcp-url http://localhost:8000/mcp \\
        --mcp-token "Token <your_seafile_token>" \\
        --llm-base-url https://api.openai.com/v1 \\
        --llm-api-key sk-... \\
        --llm-model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("MCP_URL", "http://localhost:8000/mcp"),
        help="seafile-mcp streamable-http endpoint (env: MCP_URL)",
    )
    parser.add_argument(
        "--mcp-token",
        default=os.environ.get("MCP_TOKEN"),
        help="Seafile API token, e.g. 'Token abc123' (env: MCP_TOKEN)",
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("OPENAI_BASE_URL"),
        help="OpenAI-compatible endpoint base URL (env: OPENAI_BASE_URL)",
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="API key for the LLM endpoint (env: OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        help="Model name (env: OPENAI_MODEL)",
    )
    args = parser.parse_args()

    if not args.mcp_token:
        parser.error("--mcp-token is required (or set MCP_TOKEN)")
    if not args.llm_api_key:
        parser.error("--llm-api-key is required (or set OPENAI_API_KEY)")

    return args


async def main() -> None:
    args = parse_args()

    client = MultiServerMCPClient(
        {
            "seafile": {
                "transport": "streamable_http",
                "url": args.mcp_url,
                "headers": {"Authorization": args.mcp_token},
            }
        }
    )
    tools = await client.get_tools()
    print(f"Loaded {len(tools)} tool(s): {', '.join(t.name for t in tools)}")

    llm = ChatOpenAI(
        base_url=args.llm_base_url,
        api_key=args.llm_api_key,
        model=args.llm_model,
    )
    agent = create_react_agent(llm, tools)

    print("Type a message and press enter. Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            user_input = input("you> ").strip()
        except EOFError:
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        result = await agent.ainvoke({"messages": [HumanMessage(content=user_input)]})
        reply = result["messages"][-1]
        print(f"agent> {reply.content}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
