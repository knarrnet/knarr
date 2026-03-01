"""CLI subcommands for knarr group management (via Cockpit API)."""
import json
import sys
import urllib.request
import urllib.error


def _api_request(cockpit_url: str, token: str, method: str, path: str, data: dict = None):
    url = f"{cockpit_url}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req_body = None
    if data:
        req_body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            print(f"API Error ({e.code}): {body}", file=sys.stderr)
        except Exception:
            print(f"API Error ({e.code})", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"API Connection Error: {e}\nIs the node running and Cockpit enabled?", file=sys.stderr)
        sys.exit(1)


def cmd_group(args):
    """Dispatch group subcommands."""
    # Resolve cockpit URL from config
    import os
    from .config import load_config
    config_path = getattr(args, "config", None)
    config_dir = os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()
    config = load_config(config_dir)
    port = config.get("dashboard", {}).get("port", 8080)
    token = config.get("dashboard", {}).get("token", "")
    cockpit_url = f"http://127.0.0.1:{port}"

    json_output = getattr(args, "json", False)

    if args.group_command == "list":
        res = _api_request(cockpit_url, token, "GET", "/api/groups")
        if json_output:
            print(json.dumps(res, indent=2))
        elif not res:
            print("No groups found.")
        else:
            print(f"{'GROUP':<30} {'TYPE':<12} {'MEMBERS':<8}")
            for g in res:
                print(f"{g.get('name', ''):<30} {g.get('type',''):<12} {g.get('members',0):<8}")

    elif args.group_command == "members":
        res = _api_request(cockpit_url, token, "GET", f"/api/groups/{args.name}/members")
        if json_output:
            print(json.dumps(res, indent=2))
        elif not res:
            print(f"No members in group '{args.name}'.")
        else:
            for m in res:
                print(m)

    elif args.group_command == "add":
        res = _api_request(cockpit_url, token, "POST",
                           f"/api/groups/{args.name}/members",
                           {"action": "add", "node_id": args.node_id})
        if json_output:
            print(json.dumps(res, indent=2))
        elif res.get("status") == "ok":
            print(f"Added. Group '{args.name}' now has {res.get('member_count', 0)} members.")
        else:
            print(f"Error: {res.get('error', 'Unknown')}", file=sys.stderr)
            sys.exit(1)

    elif args.group_command == "remove":
        res = _api_request(cockpit_url, token, "POST",
                           f"/api/groups/{args.name}/members",
                           {"action": "remove", "node_id": args.node_id})
        if json_output:
            print(json.dumps(res, indent=2))
        elif res.get("status") == "ok":
            print(f"Removed. Group '{args.name}' now has {res.get('member_count', 0)} members.")
        else:
            print(f"Error: {res.get('error', 'Unknown')}", file=sys.stderr)
            sys.exit(1)

    elif args.group_command == "refresh":
        data = {"name": args.name} if getattr(args, "name", None) else {}
        res = _api_request(cockpit_url, token, "POST", "/api/groups/refresh", data)
        print("Groups refreshed.")
