import asyncio
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
import mcp.types as types

import uvicorn

# Initialize MCP Server
print("Initializing MCP Server v2")
mcp_server = Server("mcp-lambda")

@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="add",
            description="Add two numbers",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        ),
        types.Tool(
            name="register_trace",
            description="Register a trace from the client",
            inputSchema={
                "type": "object",
                "properties": {
                    "trace": {"type": "string"},
                },
                "required": ["trace"],
            },
        )
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if name == "add":
        a = arguments.get("a")
        b = arguments.get("b")
        result = a + b
        return [types.TextContent(type="text", text=str(result))]
    elif name == "register_trace":
        trace = arguments.get("trace")
        print(f"DEBUG: Received trace: {trace}")
        return [types.TextContent(type="text", text=f"Trace registered: {trace}")]
    raise ValueError(f"Unknown tool: {name}")

import os
import boto3
import time
import json
from uuid import UUID, uuid4
from urllib.parse import quote, parse_qs
from mcp.server.sse import SseServerTransport
from mcp.shared.message import SessionMessage, ServerMessageMetadata
import mcp.types as types
from mcp.types import JSONRPCMessage
from mcp import types
from starlette.requests import Request
from pydantic import ValidationError
import anyio
from sse_starlette.sse import EventSourceResponse

# SSE Transport handling
sse_transport = None

# DynamoDB Setup
dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "mcp-sessions")
table = dynamodb.Table(table_name)

class DynamoDBSseTransport(SseServerTransport):
    async def handle_post_message(self, request):
        session_id_param = request.query_params.get("session_id")
        
        if not session_id_param:
            return Response("session_id required", status_code=400)

        try:
            session_id = UUID(hex=session_id_param)
        except ValueError:
            return Response("Invalid session_id", status_code=400)

        body = await request.body()
        try:
            # Validate it's a generic JSONRPC message first
            message_json = json.loads(body)
            # Store in DynamoDB
            item = {
                "session_id": str(session_id),
                "timestamp": int(time.time() * 1000000), # Microseconds for ordering
                "message": json.dumps(message_json)
            }
            # Use run_in_threadpool since boto3 is blocking
            # Fix: Use lambda to pass args correctly to put_item
            await anyio.to_thread.run_sync(lambda: table.put_item(Item=item))
            
            return Response("Accepted", status_code=202)
            
        except Exception as e:
            print(f"Error handling POST: {e}")
            return Response(str(e), status_code=500)

    @asynccontextmanager
    async def connect_sse(self, scope, receive, send):
        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
        
        # Parse query string for session_id
        query_string = scope.get("query_string", b"").decode("utf-8")
        qs = parse_qs(query_string)
        session_id_val = qs.get("session_id", [None])[0]
        
        if not session_id_val:
             print("DEBUG: session_id missing in connect_sse")
             # In a context manager we can't easily return a 400 response effectively without handling errors upstream
             # But raising an error will likely cause a 500 or drop.
             # Alternatively we could rely on the client being correct.
             raise ValueError("session_id query parameter is required")
             
        try:
            session_id = UUID(session_id_val)
        except ValueError:
             print(f"DEBUG: Invalid session_id: {session_id_val}")
             raise ValueError("Invalid session_id format")
        
        root_path = scope.get("root_path", "")
        full_endpoint = root_path.rstrip("/") + self._endpoint
        client_url = f"{quote(full_endpoint)}?session_id={session_id.hex}"

        sse_writer, sse_reader = anyio.create_memory_object_stream(0)

        async def poller_loop():
            """Polls DynamoDB for new messages sent to this session"""
            last_timestamp = 0
            last_activity = time.time()
            INACTIVITY_TIMEOUT = 10 # seconds

            while True:
                try:
                    # Check for inactivity
                    now = time.time()
                    elapsed = now - last_activity
                    # print(f"DEBUG: Session {session_id} polling. Elapsed: {elapsed:.2f}s") # excessive?

                    if elapsed > INACTIVITY_TIMEOUT:
                        print(f"DEBUG: Session {session_id} inactive for {INACTIVITY_TIMEOUT}s (Elapsed: {elapsed:.2f}). Closing.")
                        try:
                            # Perform cleanup directly here to ensure it runs
                            print(f"DEBUG: Performing cleanup from poller for session {session_id}...")
                            await anyio.to_thread.run_sync(lambda: cleanup_session(session_id))
                            print(f"DEBUG: Cleanup successful.")
                        except Exception as e:
                            print(f"DEBUG: Cleanup failed in poller: {e}")
                            
                        tg.cancel_scope.cancel()
                        break

                    # Query for messages > last_timestamp
                    response = await anyio.to_thread.run_sync(
                        lambda: table.query(
                            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(str(session_id)) & 
                                                   boto3.dynamodb.conditions.Key("timestamp").gt(last_timestamp)
                        )
                    )
                    items = response.get("Items", [])
                    if items:
                        last_activity = time.time() # Update activity on new messages
                        
                    for item in items:
                        last_timestamp = max(last_timestamp, int(item["timestamp"]))
                        msg_str = item["message"]
                        try:
                            # Use proper Pydantic model validation from strict types
                            message = types.JSONRPCMessage.model_validate_json(msg_str)
                            # Put into the read stream for the Server to consume
                            await read_stream_writer.send(SessionMessage(message, metadata=None))
                        except Exception as e:
                            print(f"Failed to parse or send message from DB: {e}")

                    # Cleanup old messages occasionally? (optional optimization)
                    
                except Exception as e:
                    print(f"Poller error: {e}")
                
                await anyio.sleep(0.5) # Poll every 500ms
        
        # Remove monitor_disconnect as it might conflict or block
        # async def monitor_disconnect(): ... 

        async def output_sender():
            """Sends messages FROM the Server TO the Client via SSE"""
            async with sse_writer, write_stream_reader:
                await sse_writer.send({"event": "endpoint", "data": client_url})
                
                async for session_message in write_stream_reader:
                     # Update activity on outgoing messages too (optional, but good)
                     # But we can't easily update local 'last_activity' variable in poller_loop scope unless it's a mutable or nonlocal
                     # For simplicity, reliance on incoming messages (POSTs) is sufficient for this "client verification" use case.
                     # If the server is sending data, the client is likely listening.
                     # However, to be safe, let's just rely on POSTs for "liveness" of interaction.
                    
                    await sse_writer.send({
                        "event": "message", 
                        "data": session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    })

        async with anyio.create_task_group() as tg:
            tg.start_soon(poller_loop)
            
            async def run_response():
                try:
                    await EventSourceResponse(sse_reader, data_sender_callable=output_sender, ping=5)(scope, receive, send)
                except Exception as e:
                    print(f"Error in EventSourceResponse: {e}")
                finally:
                    print(f"DEBUG: run_response finally block entered for session {session_id}")
                    with anyio.CancelScope(shield=True):
                        # Cleanup streams
                        await read_stream_writer.aclose()
                        await write_stream_reader.aclose()
                        
                        # Delete session items from DynamoDB
                        print(f"DEBUG: Starting cleanup for session {session_id}...")
                        try:
                            # Run synchronously to avoid async cancellation issues in finally
                            # await anyio.to_thread.run_sync(lambda: cleanup_session(session_id))
                            # Just calling the function via run_sync
                            await anyio.to_thread.run_sync(lambda: cleanup_session(session_id))
                            print(f"DEBUG: Session {session_id} cleaned up successfully.")
                        except Exception as e:
                            print(f"DEBUG: Error cleaning up session: {e}")
                            import traceback
                            traceback.print_exc()

            tg.start_soon(run_response)
            
            try:
                yield (read_stream, write_stream)
            finally:
                print("DEBUG: connect_sse generator exiting, cancelling tg")
                tg.cancel_scope.cancel()

def cleanup_session(session_id):
    """Deletes all items for a given session_id"""
    print(f"DEBUG: cleanup_session helper called for {session_id}")
    try:
        # Query all items for the session
        items = []
        last_evaluated_key = None
        while True:
            query_args = {
                "KeyConditionExpression": boto3.dynamodb.conditions.Key("session_id").eq(str(session_id))
            }
            if last_evaluated_key:
                query_args["ExclusiveStartKey"] = last_evaluated_key
                
            response = table.query(**query_args)
            batch_items = response.get("Items", [])
            items.extend(batch_items)
            print(f"DEBUG: Found {len(batch_items)} items to delete (Total: {len(items)})")
            
            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break
        
        if not items:
            print("DEBUG: No items to delete.")
            return

        # Batch delete
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(
                    Key={
                        "session_id": item["session_id"],
                        "timestamp": item["timestamp"]
                    }
                )
        print("DEBUG: Batch delete complete.")
    except Exception as e:
        print(f"DEBUG: Cleanup helper error: {e}")
        raise

from starlette.datastructures import MutableHeaders
from starlette.responses import Response

class MCPSSEResponse(Response):
    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Accel-Buffering"] = "no"
                headers["Transfer-Encoding"] = "chunked"
                headers["Content-Type"] = "text/event-stream"
            elif message["type"] == "http.response.body":
                 if not hasattr(self, 'padding_sent'):
                     self.padding_sent = True
                     # Inject 64KB padding comment to flush Lambda buffer
                     padding = b": " + b" " * 65536 + b"\n\n"
                     message["body"] = padding + message.get("body", b"")
            await send(message)

        async with sse_transport.connect_sse(scope, receive, send_wrapper) as streams:
            await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())



# Replace the default transport with our Custom one
async def handle_sse(request):
    global sse_transport
    # Use our custom transport
    sse = DynamoDBSseTransport("/messages") 
    sse_transport = sse # Although global variable is less relevant now as state is external
    return MCPSSEResponse()

async def handle_messages(request):
    # Instantiate transport directly for stateless execution (Lambda split-brain)
    sse = DynamoDBSseTransport("/messages")
    # DynamoDBSseTransport.handle_post_message returns a Response object
    return await sse.handle_post_message(request)
    # We don't verify return here as the transport handles "Accepted"



# Starlette App definition
routes = [
    Route("/sse", handle_sse),
    Route("/messages", handle_messages, methods=["POST"]),
]

app = Starlette(debug=True, routes=routes)

# Lambda Handler
# handler = Mangum(app, lifespan="off")  # Unused with Web Adapter

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
