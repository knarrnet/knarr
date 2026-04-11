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
#   compute/   -GPU-heavy: LLM, image, audio, embed, vision
#   knowledge/ -maintained info: legal, medical, financial
#   tools/     -utilities: web, document, communication, dev
#   workflow/  -orchestration: pipeline, council, routing
#   gateway/   -external bridges: email, telegram, webhook
#   meta/      -skills about skills: classify, validate, monitor

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

_PROFILE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "profiles"


def _available_profiles() -> list[str]:
    if not _PROFILE_ROOT.is_dir():
        return []
    return sorted(path.name for path in _PROFILE_ROOT.iterdir() if path.is_dir())


def _copy_profile(profile: str, target_path: Path, port: int, bootstrap: str) -> str:
    profile_dir = _PROFILE_ROOT / profile
    if not profile_dir.is_dir():
        available = ", ".join(_available_profiles()) or "(none)"
        print(
            f"Error: Unknown init profile '{profile}'. Available profiles: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    target_path.mkdir(parents=True, exist_ok=False)
    written = []
    for template_path in sorted(profile_dir.rglob("*")):
        if template_path.is_dir():
            continue
        rel_path = template_path.relative_to(profile_dir)
        dest_path = target_path / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = Template(template_path.read_text(encoding="utf-8")).safe_substitute(
            port=port,
            bootstrap=bootstrap,
            directory=target_path.name,
        )
        dest_path.write_text(rendered, encoding="utf-8")
        written.append(str(rel_path))

    return (
        f"Created project in {target_path}/ from profile '{profile}'\n\n"
        + "\n".join(f"  {path}" for path in written)
        + "\n\nTo start your node:\n"
        + f"  cd {target_path}\n"
        + "  knarr serve"
    )


def init_project(directory: str, port: int = 9000, bootstrap: str = "bootstrap1.knarr.network:9000",
                 profile: str = "") -> str:
    """Scaffolds a new Knarr provider project."""
    target_path = Path(directory)
    
    if target_path.exists():
        print(f"Error: Directory '{directory}' already exists.", file=sys.stderr)
        print("Choose a new directory name; init refuses to overwrite existing paths.", file=sys.stderr)
        sys.exit(1)

    if profile:
        return _copy_profile(profile, target_path, port, bootstrap)
        
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
