# Source: https://modelcontextprotocol.io/quickstart/client#best-practices

import asyncio
import logging
import os
from contextlib import AsyncExitStack

from anthropic import Anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.utilities.logging import configure_logging, get_logger
from rich import print
from rich.prompt import Confirm, Prompt

load_dotenv(verbose=True)

log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
configure_logging(log_level)

logger = get_logger(__name__)
loggers_to_mute = [
    "anthropic",
    "httpcore",
    "requests",
    "urllib3",
    "httpx",
    "botocore",
    "PIL",
]
for logger_name in loggers_to_mute:
    logging.getLogger(logger_name).setLevel(
        max(logging.WARNING, log_level),  # Set error if error or higher
    )


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.tool_name_to_session: dict[str, ClientSession] = {}
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()
        self.history = []
        self.available_tools = []

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py or .js)
        """
        session = await self.connect_using_mcp(server_script_path)

        # Get tools from session and add to available tools
        response = await session.list_tools()
        tools = response.tools

        for tool in tools:
            # Remember which server has to be connected when using the tool
            self.tool_name_to_session[tool.name] = session
            # Add tool to available tools
            self.available_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema,
                },
            )

        new_tool_names = [tool.name for tool in tools]
        logger.info(f"Connected to server with tools: {new_tool_names}")

    async def connect_using_mcp(self, server_script_path: str) -> ClientSession:
        # Check if the path is a URL
        is_url = server_script_path.startswith(("http://", "https://"))

        if is_url:
            # Use SSE client for URL connections
            sse_transport = await self.exit_stack.enter_async_context(
                sse_client(url=server_script_path),
            )
            session = await self.exit_stack.enter_async_context(
                ClientSession(*sse_transport),
            )
        else:
            # Use stdio client for local script files
            is_python = server_script_path.endswith(".py")
            is_js = not is_python

            command = "npx" if is_js else "python"
            args = [server_script_path]

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=None,
            )

            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            session = await self.exit_stack.enter_async_context(
                ClientSession(*stdio_transport),
            )

        await session.initialize()
        return session

    async def process_query(self, query: str, confirm_tool_use: bool = True) -> None:
        """Process a query using Claude and available tools"""
        self.history.append({"role": "user", "content": query})

        response = self.anthropic.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            messages=self.history,
            tools=self.available_tools,
        )
        logger.info(f"ASSISTANT response: {response}")
        content_to_process = response.content

        max_model_calls = 10
        model_call = 1

        while content_to_process:

            if model_call > max_model_calls:
                break

            content_item = content_to_process.pop(0)
            self.history.append({"role": "assistant", "content": [content_item]})

            if content_item.type == "text":
                print(f"\n[bold red]ASSISTANT[/bold red]\n{content_item.text}")
            elif content_item.type == "tool_use":

                tool_name = content_item.name
                tool_args = content_item.input

                if confirm_tool_use:
                    should_execute_tool = Confirm.ask(
                        f"\n[bold cyan]TOOL CALL[/bold cyan]\nAccept execution of "
                        f"{tool_name} with args {tool_args}?",
                        default=True,
                    )
                else:
                    print(
                        f"\n[bold cyan]TOOL CALL[/bold cyan]\n"
                        f"Executing {tool_name} with args {tool_args}\n",
                    )
                    should_execute_tool = True

                if should_execute_tool:
                    selected_session = self.tool_name_to_session.get(tool_name)
                    result = await selected_session.call_tool(tool_name, tool_args)
                    logger.info(f"TOOL result: {result}")

                    for result_item in result.content:
                        print(f"\n[bold cyan]TOOL OUTPUT[/bold cyan]:\n{result_item.text}\n")

                    self.history.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": content_item.id,
                                    "content": result.content,
                                },
                            ],
                        },
                    )
                else:
                    message = f"User declined execution of {tool_name} with args {tool_args}"
                    self.history.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": content_item.id,
                                    "content": message,
                                    "is_error": True,
                                },
                            ],
                        },
                    )

                response = self.anthropic.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=1000,
                    messages=self.history,
                    tools=self.available_tools,
                )
                model_call += 1

                logger.info(f"ASSISTANT response: {response}")

                content_to_process.extend(response.content)
            else:
                logger.error(f"Unsupported content type: {content_item.type}")

    async def chat_loop(self, confirm_tool_use: bool = True) -> None:
        """Run an interactive chat loop"""
        logger.info("MCP Client Started!")
        logger.info("Type your queries or 'quit' to exit.")

        while True:
            try:
                query = Prompt.ask("\n[bold green]Query[/bold green] (q/quit to end chat)")
                print()
                query = query.strip()

                if query.lower() in ["quit", "q"]:
                    break

                if not query:
                    continue

                await self.process_query(query, confirm_tool_use)
            except Exception as e:
                logger.error(f"Error: {str(e)}")

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()


async def main():
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server_script>")
        sys.exit(1)

    client = MCPClient()
    try:
        for server_script_path in sys.argv[1:]:
            await client.connect_to_server(server_script_path)
        confirm_tool_use = os.getenv("CONFIRM_TOOL_USE", "false").lower() == "true"
        await client.chat_loop(confirm_tool_use=confirm_tool_use)
    finally:
        await client.cleanup()


if __name__ == "__main__":
    import sys

    asyncio.run(main())
