import asyncio
import json
import logging
from typing import List, Dict, Any, Optional, Callable
from .node import DHTNode
from ..core.validation import flatten_json_schema

logger = logging.getLogger(__name__)

class MCPBridgeError(Exception):
    """Base class for MCP bridge errors."""
    pass

class MCPToolError(MCPBridgeError):
    """Raised when an MCP tool returns an error."""
    pass

class MCPTimeoutError(MCPBridgeError):
    """Raised when an MCP tool call times out."""
    pass

class MCPBridge:
    """Wraps an MCP server subprocess and exposes its tools as Knarr skills."""

    def __init__(self, command: List[str], node: DHTNode,
                 tool_filter: Optional[Dict[str, Any]] = None,
                 tool_timeout: float = 30.0):
        self._command = command
        self._node = node
        self._tool_filter = tool_filter or {}
        self._tool_timeout = tool_timeout
        self._process: Optional[asyncio.subprocess.Process] = None
        self._msg_id = 0
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._pending_requests: Dict[int, tuple[asyncio.Future, asyncio.AbstractEventLoop]] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        """Starts the MCP subprocess and initializes the bridge."""
        self._loop = asyncio.get_running_loop()
        logger.info(f"Starting MCP bridge: {' '.join(self._command)}")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception as e:
            raise MCPBridgeError(f"Failed to spawn MCP process: {e}")

        self._read_task = asyncio.create_task(self._read_loop())

        # 1. Initialize
        await self._initialize()
        
        # 2. List tools
        tools = await self._list_tools()
        
        # 3. Filter and register
        filtered_tools = self._apply_filter(tools)
        for tool in filtered_tools:
            name = tool["name"]
            self._tools[name] = tool
            skill_data = self._generate_skill_sheet(tool)
            
            # Register handler and announce
            self._node.register_handler(skill_data["name"], self._make_handler(name), slow=False)
            await self._node.announce(skill_data)
            logger.info(f"Bridged MCP tool '{name}' as Knarr skill '{skill_data['name']}'")

    async def stop(self):
        """Stops the MCP subprocess gracefully."""
        if self._read_task:
            self._read_task.cancel()
        
        if self._process:
            if self._process.stdin:
                self._process.stdin.close()
            
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("MCP process did not exit gracefully, terminating...")
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("MCP process still alive, killing...")
                    self._process.kill()
            
            self._process = None

    async def _initialize(self):
        """Performs the MCP initialization handshake."""
        resp = await self._send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "KnarrMCPBridge", "version": "0.1.0"}
        })
        if "protocolVersion" not in resp:
            raise MCPBridgeError("Invalid initialize response from MCP server")
        
        await self._notify("notifications/initialized")

    async def _list_tools(self) -> List[Dict[str, Any]]:
        """Lists all tools from the MCP server, handling pagination."""
        all_tools = []
        cursor = None
        
        while True:
            params = {}
            if cursor:
                params["cursor"] = cursor
                
            resp = await self._send("tools/list", params)
            all_tools.extend(resp.get("tools", []))
            
            cursor = resp.get("nextCursor")
            if not cursor:
                break
                
        return all_tools

    def _apply_filter(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Applies allow/deny filters to the tool list."""
        allow = self._tool_filter.get("allow")
        deny = self._tool_filter.get("deny")
        
        if allow:
            allow_set = set(allow)
            tools = [t for t in tools if t["name"] in allow_set]
        
        if deny:
            deny_set = set(deny)
            tools = [t for t in tools if t["name"] not in deny_set]
            
        return tools

    def _generate_skill_sheet(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        """Generates a Knarr SkillSheet from an MCP tool definition."""
        mcp_name = tool["name"]
        knarr_name = mcp_name.lower().replace("_", "-")
        input_schema_full = tool.get("inputSchema", {})
        
        return {
            "name": knarr_name,
            "version": "1.0.0",
            "description": tool.get("description", f"MCP tool: {mcp_name}")[:1024],
            "tags": ["mcp"],
            "input_schema": flatten_json_schema(input_schema_full),
            "output_schema": {"content": "string"},
            "input_schema_full": input_schema_full
        }

    def _make_handler(self, mcp_tool_name: str) -> Callable:
        """Creates a handler function for a bridged tool."""
        async def handler(input_data: Dict[str, Any]) -> Dict[str, Any]:
            return await self._call_tool(mcp_tool_name, input_data)
        return handler

    async def _call_tool(self, mcp_tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Calls an MCP tool and maps the result."""
        if not self._process or self._process.returncode is not None:
            raise MCPBridgeError("MCP server process crashed or not running")

        try:
            mcp_result = await asyncio.wait_for(
                self._send("tools/call", {
                    "name": mcp_tool_name,
                    "arguments": arguments
                }),
                timeout=self._tool_timeout
            )
            return self._map_result(mcp_result)
        except asyncio.TimeoutError:
            if self._process:
                self._process.kill()
            raise MCPTimeoutError("MCP tool execution timed out")
        except Exception as e:
            if not isinstance(e, (MCPToolError, MCPBridgeError)):
                raise MCPBridgeError(f"Unexpected error during tool call: {e}")
            raise

    def _map_result(self, mcp_result: Dict[str, Any]) -> Dict[str, Any]:
        """Maps an MCP result to a Knarr output dict."""
        content = mcp_result.get("content", [])
        is_error = mcp_result.get("isError", False)
        
        text_parts = [c["text"] for c in content if c.get("type") == "text"]
        text = "\n".join(text_parts) if text_parts else ""
        
        attachments = [c for c in content if c.get("type") != "text"]
        
        if is_error:
            raise MCPToolError(text or "Unknown MCP tool error")
            
        output = {"content": text}
        if attachments:
            output["attachments"] = attachments
            
        return output

    async def _send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sends a JSON-RPC request and waits for the response."""
        if not self._process or not self._process.stdin:
            raise MCPBridgeError("Bridge not started")
            
        self._msg_id += 1
        msg_id = self._msg_id
        
        req = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params or {}
        }
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_requests[msg_id] = (future, loop)
        
        try:
            line = json.dumps(req).encode() + b"\n"
            
            async def _write():
                if self._process and self._process.stdin:
                    self._process.stdin.write(line)
                    await self._process.stdin.drain()
            
            if loop != self._loop and self._loop:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_write(), self._loop))
            else:
                await _write()
            
            return await future
        finally:
            self._pending_requests.pop(msg_id, None)

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None):
        """Sends a JSON-RPC notification."""
        if not self._process or not self._process.stdin:
            return
            
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        line = json.dumps(msg).encode() + b"\n"
        
        async def _write():
            if self._process and self._process.stdin:
                self._process.stdin.write(line)
                await self._process.stdin.drain()
            
        loop = asyncio.get_running_loop()
        if loop != self._loop and self._loop:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_write(), self._loop))
        else:
            await _write()

    async def _read_loop(self):
        """Background task to read lines from MCP server's stdout."""
        if not self._process or not self._process.stdout:
            return
            
        try:
            while True:
                try:
                    line = await self._process.stdout.readline()
                    if not line:
                        break
                        
                    msg = json.loads(line.decode())
                    if "id" in msg:
                        msg_id = msg["id"]
                        entry = self._pending_requests.get(msg_id)
                        if entry:
                            future, floop = entry
                            if not future.done():
                                if "error" in msg:
                                    err = MCPBridgeError(msg["error"].get("message", "Unknown error"))
                                    floop.call_soon_threadsafe(future.set_exception, err)
                                else:
                                    floop.call_soon_threadsafe(future.set_result, msg.get("result", {}))
                    else:
                        # Notification from server (ignore for Phase 3)
                        pass
                except json.JSONDecodeError as e:
                    logger.warning(f"Non-JSON output from MCP server: {e}")
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in MCP read loop: {e}")
                    break
        finally:
            # Fail any pending requests on loop exit
            for future, floop in self._pending_requests.values():
                if not future.done():
                    floop.call_soon_threadsafe(future.set_exception, MCPBridgeError("MCP read loop exited"))
