"""Local SDK client with explicit lifetime, timeouts and envelope normalization."""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

from clinical_trial_patient_matching.patient_tools import ToolResult


class MCPTransport:
    def __init__(self, config):
        self.config = config

    def __enter__(self):
        self.tools = asyncio.run(self.request())
        return self

    async def request(self, name=None, arguments=None):
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "clinical_trial_patient_matching.patient_mcp_server",
                    "--config",
                    str(self.config.resolve()),
                ],
            ),
            read_timeout_seconds=15,
        ) as client:
            if name is None:
                discovered = await client.list_tools()
                return [t.name for t in discovered.tools]
            return await client.call_tool(name, arguments)

    def call(self, name, arguments):
        try:
            response = asyncio.run(self.request(name, arguments))
            if response.is_error:
                raise ValueError(
                    "MCP server returned an error: " + str(response.content)
                )
            value = response.structured_content
            if value is None:
                value = json.loads(
                    next(c.text for c in response.content if c.type == "text")
                )
            return ToolResult.model_validate(value)
        except Exception as error:
            return ToolResult(
                status="error",
                source_version="transport-unavailable",
                coverage={"complete": False},
                error=str(error),
            )

    def __exit__(self, *exc):
        return False
