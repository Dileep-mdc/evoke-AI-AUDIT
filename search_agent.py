import asyncio

from agents import Agent, Runner
from serpapi_search_tools import web_search


async def main():
    agent = Agent(
        name="research-assistant",
        model="gpt-5.4-mini",
        instructions=(
            "Use web search for current facts. Explain which sources support "
            "your answer and say when the results are inconclusive."
        ),
        tools=[web_search()],
    )
    result = await Runner.run(
        agent,
        "Find three recent Python packaging changes and explain why they matter.",
    )
    print(result.final_output)


asyncio.run(main())