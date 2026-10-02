"""The workbench web server: one script at a time, an editor and a viewer.

    uv run python -m workbench.server [SCRIPT] [--port 8765]

Everything the page and the MCP server know goes through this HTTP API, so
both always see the same script, the same build and the same selection.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "workbench" / "static"
STATE = ROOT / ".workbench"
RUN_TIMEOUT = 120


class Workbench:
    def __init__(self, script: Path):
        self._lock = asyncio.Lock()
        STATE.mkdir(exist_ok=True)
        self.selection_file = STATE / "selection.json"
        self.open(script)

    def open(self, script: Path):
        self.script = script.resolve()
        self._build: tuple[str, dict] | None = None

    def scripts(self) -> list[Path]:
        """The scripts of the repo's workspace/ and examples/, plus the open one."""
        found = {p.resolve() for base in ("workspace", "examples") for p in (ROOT / base).glob("*.py")}
        return sorted(found | {self.script})

    def text(self) -> str:
        return self.script.read_text() if self.script.exists() else ""

    def version(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()[:12]

    async def build(self) -> dict:
        """Run the script once per version; later calls reuse the result."""
        async with self._lock:
            version = self.version()
            if self._build and self._build[0] == version:
                return self._build[1]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "build.json"
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        [sys.executable, "-m", "workbench.execute", str(self.script), str(out)],
                        cwd=self.script.parent,
                        env={**os.environ, "PYTHONPATH": str(ROOT)},
                        capture_output=True,
                        text=True,
                        timeout=RUN_TIMEOUT,
                    )
                except subprocess.TimeoutExpired:
                    result = {"ok": False, "output": "", "error": f"the build took more than {RUN_TIMEOUT} s"}
                else:
                    if out.exists():
                        result = json.loads(out.read_text())
                    else:
                        result = {"ok": False, "output": proc.stdout, "error": proc.stderr or "the build crashed"}
            result["version"] = version
            self._build = (version, result)
            return result


class Script(BaseModel):
    text: str


class Selection(BaseModel):
    items: list[dict]


class Open(BaseModel):
    path: str


def create_app(bench: Workbench) -> FastAPI:
    app = FastAPI(title="build123d workbench")

    @app.middleware("http")
    async def revalidate(request, call_next):
        # The page changes as people hack on it: never let a browser serve a stale copy.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    def label(script: Path) -> str:
        return str(script.relative_to(ROOT)) if script.is_relative_to(ROOT) else str(script)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/script")
    def get_script():
        return {"path": str(bench.script), "text": bench.text(), "version": bench.version()}

    @app.put("/api/script")
    def put_script(script: Script):
        bench.script.write_text(script.text)
        return {"version": bench.version()}

    @app.get("/api/scripts")
    def get_scripts():
        return {"current": label(bench.script), "scripts": [label(s) for s in bench.scripts()]}

    @app.put("/api/open")
    async def put_open(request: Open):
        target = {label(s): s for s in bench.scripts()}.get(request.path)
        if target is None:
            raise HTTPException(404, f"unknown script {request.path}")
        async with bench._lock:
            bench.open(target)
        return {"current": label(bench.script)}

    @app.get("/api/build")
    async def get_build(mesh: bool = True):
        result = await bench.build()
        if not mesh:
            result = {k: v for k, v in result.items() if k != "mesh"}
        return result

    @app.get("/api/selection")
    def get_selection():
        if not bench.selection_file.exists():
            return {"items": [], "version": None}
        return json.loads(bench.selection_file.read_text())

    @app.put("/api/selection")
    def put_selection(selection: Selection):
        if not all(item.get("kind") in ("face", "edge") for item in selection.items):
            raise HTTPException(400, "each item needs a kind: face or edge")
        data = {"items": selection.items, "version": bench.version()}
        bench.selection_file.write_text(json.dumps(data, indent=2))
        return data

    @app.get("/api/events")
    async def events():
        """Server-sent events: one `script` event each time the open script changes on disk."""

        async def stream():
            last = bench.version()
            while True:
                await asyncio.sleep(0.5)
                current = bench.version()
                if current != last:
                    last = current
                    yield f"event: script\ndata: {current}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="build123d workbench")
    parser.add_argument("script", nargs="?", default="workspace/model.py", help="the build123d script to open")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    script = Path(args.script)
    if not script.is_file():
        sys.exit(f"{script} not found")
    print(f"workbench on http://127.0.0.1:{args.port}  ({script.resolve()})")
    # The page keeps an event stream open forever: without a timeout, stopping the
    # server would wait for the browser to hang up.
    uvicorn.run(
        create_app(Workbench(script)), host="127.0.0.1", port=args.port, log_level="warning",
        timeout_graceful_shutdown=1,
    )


if __name__ == "__main__":
    main()
