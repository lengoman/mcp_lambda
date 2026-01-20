
import asyncio
import json
import logging
from typing import Any, Dict, List, Annotated
from typing_extensions import TypedDict

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp import types

from langchain_core.tools import StructuredTool
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END

from pydantic import BaseModel, create_model

# --- MCP to LangChain Adapter ---
class McpLangChainAdapter:
    def __init__(self, session: ClientSession):
        self.session = session

    async def list_tools(self) -> List[StructuredTool]:
        mcp_tools = await self.session.list_tools()
        langchain_tools = []
        for tool in mcp_tools.tools:
            langchain_tools.append(self._create_tool(tool))
        return langchain_tools

    def _create_tool(self, tool: types.Tool) -> StructuredTool:
        async def _invoke(
            **kwargs: Any,
        ) -> Any:
            result = await self.session.call_tool(tool.name, arguments=kwargs)
            if result.isError:
                 raise Exception(f"Tool call failed: {result}")
            # Combine text content
            text = "".join([c.text for c in result.content if c.type == "text"])
            return text

        # Dynamically create Pydantic model for args_schema
        fields = {}
        if tool.inputSchema and "properties" in tool.inputSchema:
            for field_name, prop in tool.inputSchema["properties"].items():
                # Map generic usage to Any for simplicity in this demo
                # In production, map 'number'->float, 'string'->str, etc.
                fields[field_name] = (Any, ...)
        
        Schema = create_model(f"{tool.name}Schema", **fields)

        return StructuredTool.from_function(
            func=None,
            coroutine=_invoke,
            name=tool.name,
            description=tool.description or "",
            args_schema=Schema, 
        )

# --- LangGraph Workflow ---

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], "The messages in the conversation"]

async def run_workflow(tools: List[StructuredTool]):
    tool_map = {t.name: t for t in tools}

    # Node that simulates an LLM deciding to call the tool
    def agent_node(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        
        tool_calls = []

        # Hardcoded logic to simulate "add 5 and 7" if asked
        if isinstance(last_message, HumanMessage) and "5 and 7" in last_message.content:
            print("Agent: Deciding to call 'add' with 5 and 7")
            tool_calls.append({
                "name": "add",
                "args": {"a": 5, "b": 7},
                "id": "call_123"
            })
        
        # Hardcoded logic for trace registration
        if isinstance(last_message, HumanMessage) and "trace" in last_message.content:
             print("Agent: Deciding to call 'register_trace'")
             tool_calls.append({
                "name": "register_trace",
                "args": {"trace": "trace from langgraph"},
                "id": "call_trace_123"
             })

        if tool_calls:
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=tool_calls
                    )
                ]
            }
            
        return {"messages": []}

    # Node that executes tools
    async def tool_node(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        
        new_messages = []
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            for tool_call in last_message.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]
                
                if tool_name in tool_map:
                    print(f"ToolNode: Executing {tool_name} with {tool_args}")
                    tool = tool_map[tool_name]
                    # Execute tool
                    res = await tool.ainvoke(tool_args)
                    print(f"ToolNode: Result: {res}")
                
                    new_messages.append(
                        ToolMessage(content=str(res), tool_call_id=tool_id)
                    )
        
        return {"messages": new_messages}

    def should_continue(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)

    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    workflow.add_edge("tools", END) # End after tool execution for this simple example

    app = workflow.compile()

    print("Workflow: Starting with 'Please add 5 and 7' and 'register trace'")
    inputs = {"messages": [HumanMessage(content="Please add 5 and 7. Also register trace.")]}
    
    async for output in app.astream(inputs):
        for key, value in output.items():
            print(f"Output from node '{key}':")
            # print(value) # Verbose

# --- Main Client ---

from uuid import uuid4
import sys
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    lambda_url = os.getenv("LAMBDA_URL")
    if not lambda_url:
        print("Error: LAMBDA_URL not set in .env")
        return
    base_url = f"{lambda_url.rstrip('/')}/sse"
    if len(sys.argv) > 1:
        session_id = sys.argv[1]
    else:
        session_id = uuid4().hex
    print(f"Session ID: {session_id}")
    url = f"{base_url}?session_id={session_id}"
    print(f"Connecting to {url}...")
    
    try:
         async with sse_client(url) as streams:
            print("SSE connection established.")
            async with ClientSession(streams[0], streams[1]) as session:
                print("Initializing session...")
                await session.initialize()
                
                adapter = McpLangChainAdapter(session)
                print("Listing and adapting tools...")
                lc_tools = await adapter.list_tools()
                print(f"Adapted tools: {[t.name for t in lc_tools]}")
                
                await run_workflow(lc_tools)
                
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
