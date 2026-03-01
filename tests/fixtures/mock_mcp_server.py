#!/usr/bin/env python3
"""Mock MCP server for testing the Knarr MCP bridge."""
import sys, json, argparse, time

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-on-call", action="store_true")
    parser.add_argument("--hang-on-call", action="store_true")
    args_parsed = parser.parse_args()

    for line in sys.stdin:
        try:
            msg = json.loads(line.strip())
        except json.JSONDecodeError:
            continue

        if "id" not in msg:
            continue  # notification, no response needed

        method = msg.get("method")
        msg_id = msg["id"]

        if method == "initialize":
            respond(msg_id, {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0.0"}
            })
        elif method == "tools/list":
            respond(msg_id, {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echoes input text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]
                        }
                    },
                    {
                        "name": "add_numbers",
                        "description": "Adds two numbers",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "number"},
                                "b": {"type": "number"}
                            },
                            "required": ["a", "b"]
                        }
                    },
                    {
                        "name": "error_tool",
                        "description": "A tool that returns an error",
                        "inputSchema": {"type": "object", "properties": {}}
                    }
                ]
            })
        elif method == "tools/call":
            if args_parsed.crash_on_call:
                sys.exit(1)
            if args_parsed.hang_on_call:
                time.sleep(100)
                continue

            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            if name == "echo":
                respond(msg_id, {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False})
            elif name == "add_numbers":
                try:
                    res = float(args.get("a", 0)) + float(args.get("b", 0))
                    respond(msg_id, {"content": [{"type": "text", "text": str(res)}], "isError": False})
                except Exception as e:
                    respond(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            elif name == "error_tool":
                respond(msg_id, {"content": [{"type": "text", "text": "Something went wrong"}], "isError": True})
            else:
                respond_error(msg_id, -32602, f"Unknown tool: {name}")

def respond(msg_id, result):
    msg = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

def respond_error(msg_id, code, message):
    msg = json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
