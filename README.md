# Founding Geometry Engineer: take-home challenge

**Start with [CHALLENGE.md](CHALLENGE.md)**: it explains the problem, what we ask, and how we review it.

This README covers the workbench: how to run it, how to connect a coding agent, and what is in the repository.

---

## Getting started

You need [uv](https://docs.astral.sh/uv/). Python 3.12 and every dependency are installed by the first command.

```bash
uv sync
uv run python -m workbench.server examples/ttt-ppp0101.py
```

Open http://127.0.0.1:8765. Edit the script, save with Cmd/Ctrl+S, and the part rebuilds. Click a face or an edge to select it, shift+click to add to the selection, Esc to clear. The first build takes a few seconds while OpenCascade loads.

The workbench has one script open at a time; the menu at the top opens another one from `examples/` or `workspace/`. A script either assigns its final shape to a variable named `result`, or calls `show()` on it as it would with OCP CAD Viewer.

### Plugging in an agent

`.mcp.json` declares the MCP server for Claude Code: start Claude Code at the root of this repo while the workbench runs, and it gets four tools:

| Tool | What it does |
|---|---|
| `read_script` | returns the open script |
| `write_script` | replaces the open script; the editor and the viewer follow |
| `build` | runs the script and returns errors, or the volume, the bounding box and a description of every face and edge |
| `get_selection` | returns the faces and edges the user selected in the viewer |

Any other MCP client works the same way: the server is `uv run python -m workbench.mcp_server`, over stdio.

## What is in the repo

```
workbench/
  execute.py     runs a script in its own process; mesh plus face and edge descriptions
  server.py      HTTP API: script, build, selection, change events
  mcp_server.py  MCP tools, a thin client of the HTTP API
  static/        the page: CodeMirror editor, three-cad-viewer, picking
examples/        twelve build123d parts from the build123d documentation
workspace/       a playground with a small example
```

Faces and edges are numbered in the order ocp-tessellate meshes them, so index *i* in the viewer is entry *i* in the build output. That numbering changes with every rebuild.

Everything here is a starting point, not a constraint: change any part of it, including the API and the MCP tools.
