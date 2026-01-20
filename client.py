import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

from uuid import uuid4
import sys
import os
from dotenv import load_dotenv

load_dotenv()

async def run():
    # The Lambda Function URL provided
    lambda_url = os.getenv("LAMBDA_URL")
    if not lambda_url:
        print("Error: LAMBDA_URL not set in .env")
        return
        
    # Ensure URL ends with /sse
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
                
                print("Session initialized.")
                
                print("Listing tools...")
                tools = await session.list_tools()
                print(f"Available tools: {tools}")

                print("Calling 'add' tool with 5 and 7...")
                result = await session.call_tool("add", arguments={"a": 5, "b": 7})
                print(f"Result: {result}")
                
                print("Calling 'register_trace' with 'client.py trace'...")
                trace_result = await session.call_tool("register_trace", arguments={"trace": "client.py trace"})
                print(f"Trace Result: {trace_result}")
                
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(run())
