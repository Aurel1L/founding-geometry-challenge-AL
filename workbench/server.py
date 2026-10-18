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
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from workbench.manual_edits import (
    GEOMETRIC_SELECTOR,
    LEGACY_SELECTOR,
    MANUAL_JOURNAL_SCHEMA_VERSION,
    TOPOLOGIC_SELECTOR,
    ManualEditJournal,
    command_from_operation,
    operation_from_command,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "workbench" / "static"
STATE = ROOT / ".workbench"
RUN_TIMEOUT = 120


class Workbench:
    def __init__(self, script: Path):
        self._lock = asyncio.Lock()
        STATE.mkdir(exist_ok=True)
        self.selection_file = STATE / "selection.json"
        self.manual_commands_file = STATE / "manual_commands.json"
        self._journal = ManualEditJournal(self.manual_commands_file, self._source_version)
        self._journal.load()
        self._manual_commands_by_script = self._journal.scripts
        self.open(script)

    @staticmethod
    def _source_version(script: Path) -> str | None:
        if not script.is_file():
            return None
        return hashlib.sha256(script.read_text().encode()).hexdigest()[:12]

    @classmethod
    def _operation_from_command(cls, command: dict) -> dict:
        return operation_from_command(command)

    def _load_manual_journal(self, raw_journal: dict) -> tuple[dict[str, dict], bool]:
        return self._journal.parse(raw_journal)

    def _persist_manual_journal(self) -> None:
        self._journal.scripts = self._manual_commands_by_script
        self._journal.persist()

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

    def manual_commands(self) -> list[dict]:
        return self._journal.commands(self.script)

    def manual_operations(self) -> list[dict]:
        return self._journal.operations(self.script)

    @staticmethod
    def _command_from_operation(operation: dict) -> dict:
        return command_from_operation(operation)

    def _build_commands(self, operations: list[dict]) -> list[dict]:
        return [self._command_from_operation(operation) for operation in operations]

    async def clear_manual_commands(self) -> dict:
        async with self._lock:
            result = await self._run_build([])
            if not result["ok"]:
                return result
            self._journal.scripts = self._manual_commands_by_script
            self._journal.clear(self.script)
            self._manual_commands_by_script = self._journal.scripts
            build_id = self.build_id([])
            result["version"] = self.version()
            result["build_id"] = build_id
            self._build = (build_id, result)
            return result

    def build_id(self, operations: list[dict] | None = None) -> str:
        current_operations = self.manual_operations() if operations is None else operations
        payload = json.dumps(
            {"script": str(self.script), "version": self.version(), "operations": current_operations},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    async def build(self) -> dict:
        """Run the script once per version; later calls reuse the result."""
        async with self._lock:
            operations = self.manual_operations()
            build_id = self.build_id(operations)
            if self._build and self._build[0] == build_id:
                return self._build[1]
            result = await self._run_build(self._build_commands(operations))
            result["version"] = self.version()
            result["build_id"] = build_id
            self._build = (build_id, result)
            return result

    async def apply_manual_command(self, command: dict, expected_build_id: str) -> dict:
        async with self._lock:
            operations = self.manual_operations()
            if expected_build_id != self.build_id(operations):
                raise ValueError("The displayed part changed; select the face again")

            updated_operations = [*operations, self._operation_from_command(command)]
            result = await self._run_build(self._build_commands(updated_operations))
            if not result["ok"]:
                return result

            self._journal.scripts = self._manual_commands_by_script
            self._journal.set_operations(self.script, self.version(), updated_operations)
            self._manual_commands_by_script = self._journal.scripts
            build_id = self.build_id(updated_operations)
            result["version"] = self.version()
            result["build_id"] = build_id
            self._build = (build_id, result)
            return result

    async def _run_build(self, commands: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "build.json"
            commands_file = Path(tmp) / "manual_commands.json"
            commands_file.write_text(json.dumps(commands))
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "workbench.execute", str(self.script), str(out), str(commands_file)],
                    cwd=self.script.parent,
                    env={**os.environ, "PYTHONPATH": str(ROOT)},
                    capture_output=True,
                    text=True,
                    timeout=RUN_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "output": "", "error": f"the build took more than {RUN_TIMEOUT} s"}
            if out.exists():
                return json.loads(out.read_text())
            return {"ok": False, "output": proc.stdout, "error": proc.stderr or "the build crashed"}


class Script(BaseModel):
    text: str


class Selection(BaseModel):
    items: list[dict]


class Open(BaseModel):
    path: str


class ManualCommand(BaseModel):
    name: str
    selection: dict | list[dict]
    build_id: str
    distance: float | None = None
    bounds: dict | None = None


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

    @app.post("/api/manual-command")
    async def post_manual_command(command: ManualCommand):
        try:
            result = await bench.apply_manual_command(
                {
                    "name": command.name,
                    "selection": command.selection,
                    "distance": command.distance,
                    "bounds": command.bounds,
                },
                command.build_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result["ok"]:
            detail = result.get("error", "manual command failed")
            if result.get("output"):
                detail += "\n\nCaptured Python output:\n" + result["output"]
            raise HTTPException(422, detail)
        return result

    @app.delete("/api/manual-commands")
    async def delete_manual_commands():
        result = await bench.clear_manual_commands()
        if not result["ok"]:
            raise HTTPException(422, result.get("error", "build failed after clearing manual edits"))
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
