"""MCP server that lets a coding agent work in the workbench.

    uv run python -m workbench.mcp_server     (stdio; see .mcp.json)

It is a thin client of the running workbench (`uv run workbench ...`): the agent
reads and writes the same script the editor shows, sees the same build and
the same selection the user made in the viewer.
"""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

URL = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8765")

mcp = MCPServer(
    "workbench",
    instructions=(
        "You work on the build123d script open in the user's editor and 3D viewer. The "
        "script assigns its final shape to `result` or calls show() on it. Read it, edit it, "
        "then call build to check what it produces. get_selection returns the faces and "
        "edges the user clicked in the viewer."
    ),
)


def _call(method: str, path: str, **kwargs) -> dict:
    try:
        response = httpx.request(method, URL + path, timeout=180, **kwargs)
    except httpx.ConnectError:
        raise RuntimeError(f"the workbench is not running at {URL}; start it with `uv run python -m workbench.server`")
    response.raise_for_status()
    return response.json()


@mcp.tool()
def read_script() -> str:
    """Return the content of the open script."""
    return _call("GET", "/api/script")["text"]


@mcp.tool()
def write_script(text: str) -> str:
    """Replace the whole content of the open script. The editor and viewer update on their own."""
    version = _call("PUT", "/api/script", json={"text": text})["version"]
    return f"Script written (version {version}). Call build to check the result."


@mcp.tool()
def build() -> str:
    """Run the open script and return errors or a summary of the part: volume, bounding box,
    and a description of every face and edge, indexed as in the viewer."""
    result = _call("GET", "/api/build", params={"mesh": False})
    return json.dumps(result)


@mcp.tool()
def get_selection() -> str:
    """Return the faces and edges the user has selected in the viewer, with their
    geometry. `version` is the script version the selection was made on."""
    return json.dumps(_call("GET", "/api/selection"))


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
