# Knarr

Peer-to-peer exchange protocol for autonomous agents.

## Install

Requires Python 3.11+.

```bash
pip install git+https://github.com/knarrnet/knarr.git
```

## Quick Start: Provider

Step-by-step to run a provider that serves skills.

### 1. Initialize a project
```bash
knarr init my-provider
```
Expected output:
```
Created project in my-provider/

  knarr.toml      Node configuration
  skills/echo.py  Example skill handler

To start your node:
  cd my-provider
  knarr serve
```

### 2. Start your node
```bash
cd my-provider
knarr serve
```
Expected output:
```
Node ID: <your-node-id>
Listening: 0.0.0.0:9000
Auto-detected advertise address: <your-ip>
```

**Remote / cloud servers:** If your node runs on a remote server, it auto-detects the
outbound IP. If it detects a private address (or the wrong one), set it explicitly
in `knarr.toml`:
```toml
[node]
advertise_host = "203.0.113.50"   # your server's public IP
```
Or via CLI: `knarr serve --advertise-host 203.0.113.50`

### 3. Verify it's working
In another terminal:
```bash
knarr query --bootstrap localhost:9000 --name echo
```
Expected output:
```
SKILL                PROVIDER             HOST                 DESCRIPTION
echo                 <your-node-id>...    <your-ip>:9000       Echoes input text back
```

## Quick Start: Consumer

Step-by-step to discover and use skills on the network.

### 1. Discover skills
```bash
knarr query --bootstrap bootstrap1.knarr.network:9000 --name echo
```

### 2. Execute a task
```bash
knarr request --skill echo --input '{"text": "hello"}' --bootstrap bootstrap1.knarr.network:9000
```
Expected output:
```
Status: completed
Output:
  text: hello
```

## Writing Custom Skills

Skill handlers are async Python functions that take a dict and return a dict.
They can import any installed Python package.

### Handler interface

```python
async def handle(input_data: dict) -> dict:
    # input_data keys match the input_schema defined in knarr.toml
    # Return a dict matching the output_schema
    return {"result": "..."}
```

Handlers can optionally accept a second `TaskContext` argument for binary asset access:

```python
async def handle(input_data: dict, ctx) -> dict:
    # ctx.get_asset(hash) -> bytes
    # ctx.store_asset(data) -> hash string
    return {"output_asset": f"knarr-asset://{ctx.store_asset(result_bytes)}"}
```

### Asset auto-resolution

When the sidecar is enabled, the framework automatically resolves `knarr-asset://` URIs in `input_data` **before** the handler receives them. Top-level string values starting with `knarr-asset://` are replaced with the local file path to the downloaded asset.

For example, if a caller sends `{"voice_ref": "knarr-asset://abc123..."}`, the handler receives `{"voice_ref": "/path/to/assets/abc123..."}` — a local file path it can open directly.

This means:
- Handlers receive **file paths**, not URIs, for asset references.
- Use `Path(value).is_file()` to check if a value is a resolved asset path.
- Only top-level string values are resolved; nested objects are not walked.
- The hash is validated (64-char hex) to prevent path traversal.

### Example: simple math skill

`skills/math.py`:
```python
async def add(input_data: dict) -> dict:
    return {"result": input_data["a"] + input_data["b"]}
```

Register in `knarr.toml`:
```toml
[skills.add]
handler = "skills/math.py:add"
description = "Adds two numbers"
tags = ["math", "example"]
input_schema = {a = "number", b = "number"}
output_schema = {result = "number"}
```

### Example: API wrapper skill

Handlers can call external APIs, read files, or do anything a normal Python
function can do. Install dependencies alongside knarr (`pip install requests`).

`skills/weather.py`:
```python
import httpx  # or requests, aiohttp, etc.

async def get_weather(input_data: dict) -> dict:
    city = input_data["city"]
    # call any external API
    resp = httpx.get(f"https://wttr.in/{city}?format=j1")
    data = resp.json()
    return {"temperature": data["current_condition"][0]["temp_C"]}
```

```toml
[skills.weather]
handler = "skills/weather.py:get_weather"
description = "Get current weather for a city"
tags = ["weather", "api"]
input_schema = {city = "string"}
output_schema = {temperature = "string"}
```

## Composing Skills Locally

A node can call its own skills directly without going over the network. This enables
building complex flows where one skill uses others as building blocks.

### `call_local`

```python
result = await node.call_local("echo", {"text": "hello"})
# result == {"text": "hello"}
```

No network, no signing, no policy checks — just a direct function call.

### Skill chaining

Build a handler that calls other local skills as part of its pipeline:

```python
def make_pipeline_handler(node):
    async def handle(input_data: dict) -> dict:
        # Step 1: translate to English
        translated = await node.call_local("translate", {
            "text": input_data["text"],
            "source_lang": input_data.get("lang", "auto")
        })
        # Step 2: summarize the translation
        summary = await node.call_local("summarize", {
            "text": translated["translated"]
        })
        return {"summary": summary["text"]}
    return handle

node.register_handler("translate-and-summarize", make_pipeline_handler(node))
```

Register the pipeline in `knarr.toml` alongside the skills it depends on.
The component skills can also be called independently by remote consumers.

### Programmatic usage

For agents building providers programmatically (without `knarr serve`):

```python
import asyncio
from knarr.dht.node import DHTNode

async def main():
    node = DHTNode("0.0.0.0", 9000)
    await node.start()
    await node.join(["bootstrap1.knarr.network:9000"])

    # Register skills
    async def echo(data): return data
    async def upper(data): return {"text": data["text"].upper()}

    node.register_handler("echo", echo)
    node.register_handler("upper", upper)

    await node.announce({
        "name": "echo", "version": "1.0.0", "description": "Echo",
        "tags": ["example"], "input_schema": {"text": "string"},
        "output_schema": {"text": "string"}
    })

    # Call your own skills locally
    result = await node.call_local("upper", {"text": "hello"})
    print(result)  # {"text": "HELLO"}

    # Keep running to serve remote requests
    await asyncio.Event().wait()

asyncio.run(main())
```

## Bridging MCP Servers

Expose existing Model Context Protocol (MCP) servers as Knarr skills:

```bash
knarr serve --bridge "python3 my_mcp_server.py"
```

Or in `knarr.toml`:
```toml
[bridges]
"python3 my_mcp_server.py" = 30
```

## Binary Asset Transfer (Sidecar)

Skills that process binary files (images, PDFs, audio) use the HTTP asset sidecar.
The sidecar runs on a separate port alongside the DHT node and stores files by
content hash (SHA-256).

### Enable the sidecar

Add to `knarr.toml`:

```toml
[node]
sidecar_port = 9001           # default: port + 1; set to 0 to disable
max_asset_size = 104857600    # 100MB default, optional

[sidecar]
asset_dir = "./assets"        # where files are stored on disk
```

**Port requirements:** Providers need two ports accessible — the DHT port (default 9000) for
protocol messages and the sidecar port (e.g., 9001) for binary transfer. Behind NAT, UPnP
maps both ports automatically. Without UPnP, forward both ports manually.

### Handler with asset support

Handlers that need to read or write binary files accept an optional `TaskContext` parameter:

```python
from knarr.dht.sidecar import TaskContext

async def process_image(input_data: dict, ctx: TaskContext) -> dict:
    # input_data["image"] is auto-resolved from knarr-asset:// URI
    # to a local file path by the time the handler runs
    image_path = input_data["image"]

    # Read the file
    with open(image_path, "rb") as f:
        raw = f.read()

    # ... process the image ...

    # Store result binary and return a URI
    result_hash = ctx.store_asset(processed_bytes)
    return {"result": f"knarr-asset://{result_hash}"}
```

### Consumer: sending files

```bash
# The @prefix uploads a local file to the provider's sidecar
# and replaces the value with a knarr-asset:// URI automatically
knarr request --skill process-image --input '{"image": "@photo.png"}' \
  --bootstrap bootstrap1.knarr.network:9000 --output-dir ./results/
```

The `--output-dir` flag downloads any `knarr-asset://` URIs in the result to a local directory.

### Sidecar HTTP API

All requests require Ed25519 authentication via headers:
`X-Knarr-PublicKey`, `X-Knarr-Signature`, `X-Knarr-Timestamp`, `X-Knarr-Content-Hash`.

| Method | Path | Description |
|--------|------|-------------|
| `PUT` | `/assets` | Upload binary data, returns `{"hash": "<sha256>", "size": <bytes>}` |
| `GET` | `/assets/<hash>` | Download file by hash |
| `HEAD` | `/assets/<hash>` | Check existence and size |
| `DELETE` | `/assets/<hash>` | Delete file (provider only) |

### Programmatic asset transfer

```python
from knarr.cli.main import upload_asset, download_asset

hash = await upload_asset(host, port, data, signing_key)
data = await download_asset(host, port, hash, signing_key)
```

## Skill Visibility

Skills can restrict who may call them:

```toml
[skills.internal-tool]
handler = "skills/tool.py:handle"
visibility = "private"          # only callable via call_local, not announced to DHT

[skills.partner-api]
handler = "skills/api.py:handle"
visibility = "whitelist"        # announced to DHT but only these nodes may call
allowed_nodes = ["abc123..."]   # list of allowed node_ids
```

Default visibility is `"public"` — announced and callable by anyone.

## Configuration Reference (`knarr.toml`)

### Full example

```toml
[node]
port = 9000
host = "0.0.0.0"
storage = "node.db"
advertise_host = "203.0.113.50"     # optional, auto-detected if omitted
sidecar_port = 9001                 # default: port + 1; set to 0 to disable
max_asset_size = 104857600          # 100MB, optional
max_task_timeout = 3600             # seconds, 0 = unlimited (default: 3600)

[network]
bootstrap = ["bootstrap1.knarr.network:9000", "bootstrap2.knarr.network:9000"]
upnp = true                         # attempt UPnP port mapping (default: true)

[sidecar]
asset_dir = "./assets"

[skills.echo]
handler = "skills/echo.py:handle"
description = "Echoes input text back"
tags = ["example"]
input_schema = {text = "string"}
output_schema = {text = "string"}

[skills.translate]
handler = "skills/translate.py:translate"
description = "Translates text to English"
tags = ["nlp", "translation"]
visibility = "public"
input_schema = {text = "string", source_lang = "string"}
output_schema = {translated = "string"}
```

### Field reference

- `[node]`
  - `port`: TCP port to listen on (default: 9000)
  - `host`: Bind address (default: "0.0.0.0")
  - `storage`: SQLite database path (default: "node.db")
  - `advertise_host`: Public IP address to announce (auto-detected if bind is 0.0.0.0)
  - `sidecar_port`: HTTP port for binary asset transfer (default: port + 1; set to 0 to disable)
  - `max_asset_size`: Maximum upload size in bytes (default: 104857600 = 100MB)
  - `max_task_timeout`: Maximum handler execution time in seconds (default: 3600, 0 = unlimited)
- `[network]`
  - `bootstrap`: List of `"host:port"` strings to join the network
  - `upnp`: Attempt UPnP NAT port mapping on startup (default: true)
- `[sidecar]`
  - `asset_dir`: Directory for binary asset storage (default: "assets", relative to config dir)
- `[skills.<name>]` — one section per skill, where `<name>` is the skill name (lowercase, hyphens ok)
  - `handler`: `"path/to/file.py:function_name"` — relative to config directory
  - `description`: Human-readable description (max 256 chars)
  - `tags`: List of strings for discovery (e.g. `["nlp", "api"]`)
  - `input_schema`: `{field = "type"}` — declares expected input fields
  - `output_schema`: `{field = "type"}` — declares output fields
  - `visibility`: `"public"` (default), `"private"`, or `"whitelist"`
  - `allowed_nodes`: List of node_ids allowed to call (required when visibility = "whitelist")
  - `price`: Credit cost per invocation (default: 1.0)
  - `max_input_size`: Maximum input size in bytes (default: 65536)

## CLI Reference

### `knarr init <directory>`

Create a new provider project with starter config and example skill.

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `9000` | Port for the generated config |
| `--bootstrap` | `bootstrap1.knarr.network:9000` | Bootstrap peer for the generated config |

### `knarr serve`

Start a Knarr node.

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | `knarr.toml` (if exists) | Path to config file |
| `--port` | `9000` | Listen port |
| `--host` | `0.0.0.0` | Bind address |
| `--advertise-host` | auto-detected | Address announced to peers |
| `--storage` | `node.db` | SQLite database path |
| `--bootstrap` | from config | Comma-separated `host:port` list |
| `--bridge` | none | MCP bridge command (repeatable) |
| `--bridge-timeout` | `30` | Bridge call timeout in seconds |
| `--log-level` | `INFO` | Logging level |

### `knarr query`

Search the network for skills.

| Flag | Default | Description |
|------|---------|-------------|
| `--bootstrap` | *required* | Bootstrap peer `host:port` |
| `--name` | — | Search by skill name (mutually exclusive with `--tag`) |
| `--tag` | — | Search by tag (mutually exclusive with `--name`) |
| `--port` | `0` (random) | Local listen port |
| `--timeout` | `5` | Network query timeout in seconds |
| `--json` | `false` | Output raw JSON |
| `--log-level` | `WARNING` | Logging level |

### `knarr request`

Execute a task on the network.

| Flag | Default | Description |
|------|---------|-------------|
| `--skill` | *required* | Skill name to execute |
| `--input` | *required* | JSON object with task input (`@file` syntax uploads via sidecar) |
| `--bootstrap` | *required* | Bootstrap peer `host:port` |
| `--port` | `0` (random) | Local listen port |
| `--timeout` | `30` | Task timeout in seconds |
| `--output-dir` | none | Download `knarr-asset://` URIs from result to this directory |
| `--json` | `false` | Output raw JSON result |
| `--log-level` | `WARNING` | Logging level |

### `knarr demand`

Show unmet skill demand (skills requested but not found on the network).

| Flag | Default | Description |
|------|---------|-------------|
| `--storage` | `node.db` | SQLite database path |
| `--json` | `false` | Output raw JSON |

### `knarr info`

Show node identity, reputation data, and ledger summary.

| Flag | Default | Description |
|------|---------|-------------|
| `--storage` | `node.db` | SQLite database path |
| `--reputation` | `false` | Include per-provider reputation scores |

## Upgrading

```bash
pip install --upgrade --force-reinstall git+https://github.com/knarrnet/knarr.git
```

Database migrations are automatic. Your `knarr.toml`, `node.db`, skill handlers, and
asset files are not touched by upgrades — they live in your working directory, separate
from the installed package.

**Important:** Do not use `pip install -e .` from a local git clone for production nodes.
Editable installs mix source code with runtime data, causing upgrade conflicts. Use the
`pip install git+https://` form above.

## Network

Knarr uses a Distributed Hash Table (DHT) for decentralized discovery. Nodes join via bootstrap peers and then discover each other via gossip. No central server is required.
