import os
import sys
from pathlib import Path
from string import Template

KNARR_TOML_TEMPLATE = Template("""[node]
port = $port
host = "0.0.0.0"
storage = "node.db"
# wallet = ""              # Solana address (optional)
# jurisdiction = ["eu.se"] # Node-level jurisdiction (inherited by skills)

[network]
bootstrap = ["$bootstrap"]

[cockpit]
port = 8090
# tls = "auto"  # "auto" (HTTPS, default), "off" (HTTP), "both" (HTTP + HTTPS on port+1)

# [token]
# mint = ""                  # $$KNARR SPL mint address
# rpc_url = ""               # Custom Solana RPC (default: mainnet-beta public)

# URI taxonomy categories (convention, not enforced):
#   compute/   — GPU-heavy: LLM, image, audio, embed, vision
#   knowledge/ — maintained info: legal, medical, financial
#   tools/     — utilities: web, document, communication, dev
#   workflow/  — orchestration: pipeline, council, routing
#   gateway/   — external bridges: email, telegram, webhook
#   meta/      — skills about skills: classify, validate, monitor

[skills.echo]
uri = "knarr:///tools/dev/echo@1.0"
handler = "skills/echo.py:handle"
description = "Echoes input text back"
tags = ["example"]
input_schema = {text = "string"}
output_schema = {text = "string"}
""")

ECHO_PY_TEMPLATE = """\"\"\"Echo skill - returns input text unchanged.\"\"\"


async def handle(input_data: dict) -> dict:
    \"\"\"Handle an echo request.

    Args:
        input_data: dict with key "text" (string)

    Returns:
        dict with key "text" containing the echoed input
    \"\"\"
    return {"text": input_data["text"]}
"""

def init_project(directory: str, port: int = 9000, bootstrap: str = "bootstrap1.knarr.network:9000") -> str:
    """Scaffolds a new Knarr provider project."""
    target_path = Path(directory)
    
    if target_path.exists() and any(target_path.iterdir()):
        print(f"Error: Directory '{directory}' is not empty.", file=sys.stderr)
        print("Please choose a new directory or an empty one.", file=sys.stderr)
        sys.exit(1)
        
    # Create structure
    skills_path = target_path / "skills"
    skills_path.mkdir(parents=True, exist_ok=True)
    
    # Write files
    with open(target_path / "knarr.toml", "w") as f:
        f.write(KNARR_TOML_TEMPLATE.substitute(port=port, bootstrap=bootstrap))
        
    with open(skills_path / "echo.py", "w") as f:
        f.write(ECHO_PY_TEMPLATE)
        
    return (
        f"Created project in {directory}/\n\n"
        f"  knarr.toml      Node configuration\n"
        f"  skills/echo.py  Example skill handler\n\n"
        f"To start your node:\n"
        f"  cd {directory}\n"
        f"  knarr serve"
    )