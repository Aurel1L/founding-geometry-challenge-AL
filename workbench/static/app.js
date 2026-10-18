// The workbench page: a CodeMirror editor on one build123d script, a three-cad-viewer view of
// what it builds, and face/edge picking reported back to the server.
import { Viewer, Display } from "https://cdn.jsdelivr.net/npm/three-cad-viewer@3.6.3/dist/three-cad-viewer.esm.js";

const $ = (id) => document.getElementById(id);
const PART = "/Group/result";

let viewer = null;
let build = null;      // last successful build: mesh + face/edge descriptions
let savedText = null;  // editor content as last loaded or saved
let selection = [];    // [{kind, index, ...description}]
let overlays = [];     // highlight objects added to the scene

// ---- editor

const editor = CodeMirror.fromTextArea($("editor"), {
  mode: "python",
  lineNumbers: true,
  indentUnit: 4,
  extraKeys: { "Cmd-S": save, "Ctrl-S": save, Tab: (cm) => cm.execCommand("insertSoftTab") },
});
editor.on("change", () => {
  if (editor.getValue() !== savedText) setStatus("unsaved changes", "dirty");
});

function setStatus(text, kind = "") {
  $("status").textContent = text;
  $("status").className = kind;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
  $("error-close").hidden = false;
}

function hideError() {
  $("error").hidden = true;
  $("error-close").hidden = true;
}
$("error-close").onclick = hideError;

async function loadScript() {
  const script = await (await fetch("/api/script")).json();
  $("path").textContent = script.path;
  savedText = script.text;
  editor.setValue(script.text);
}

async function save() {
  savedText = editor.getValue();
  await fetch("/api/script", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: savedText }),
  });
  await refreshBuild();
}
$("save").onclick = save;

// ---- script picker: the server has one script open at a time; opening another
// moves the editor, the viewer and the agent's MCP tools together.

async function loadScripts() {
  const { current, scripts } = await (await fetch("/api/scripts")).json();
  $("script").replaceChildren(...scripts.map((f) => new Option(f, f, false, f === current)));
}

$("script").onchange = async (e) => {
  if (editor.getValue() !== savedText && !confirm("Discard unsaved changes?")) {
    await loadScripts();
    return;
  }
  await fetch("/api/open", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: e.target.value }),
  });
  await loadScript();
  await refreshBuild();
};

// The agent (or any other process) may rewrite the script: follow it, unless the
// user has unsaved edits in the editor.
new EventSource("/api/events").addEventListener("script", async () => {
  if (editor.getValue() !== savedText) {
    setStatus("the script changed on disk; save to overwrite or reload the page", "dirty");
    return;
  }
  await loadScript();
  await refreshBuild();
});

// ---- build and render

async function refreshBuild() {
  setStatus("building…");
  const result = await (await fetch("/api/build")).json();
  if (!result.ok) {
    console.error("[Build failed]", result.error || "build failed", result.output || "");
    showError(result.error + (result.output ? "\n" + result.output : ""));
    setStatus("build failed", "error");
    return;
  }
  if (result.output?.includes("WARNING:")) console.warn("[Build warning]", result.output);
  hideError();
  build = result;
  render(result.mesh);
  setSelection([]);
  const s = result.summary;
  setStatus(`built · ${s.faces} faces · ${s.edges} edges · volume ${s.volume} mm³${s.valid ? "" : " · INVALID SOLID"}`);
}

async function clearManualCommands() {
  if (editor.getValue() !== savedText) {
    setStatus("save the script before clearing manual edits", "dirty");
    return;
  }
  if (!confirm("Clear manual edits for this script? The Python source will not change.")) return;

  setStatus("clearing manual edits…");
  const response = await fetch("/api/manual-commands", { method: "DELETE" });
  const result = await response.json();
  if (!response.ok || !result.ok) {
    const message = result.detail || result.error || "could not clear manual edits";
    showError(message);
    setStatus("clean failed", "error");
    return;
  }

  hideError();
  build = result;
  render(result.mesh);
  await setSelection([]);
  const summary = result.summary;
  setStatus(`manual edits cleared · ${summary.faces} faces · ${summary.edges} edges · volume ${summary.volume} mm³${summary.valid ? "" : " · INVALID SOLID"}`);
}
$("clear-manual-commands").onclick = clearManualCommands;

async function runManualCommand(name) {
  const multiFaceRemoval = name === "remove_fillet" || name === "remove_chamfer";
  const expectedKind = name === "offset" || name === "remove_fillet" || name === "remove_chamfer" ? "face" : "edge";
  if (
    !selection.length ||
    (!multiFaceRemoval && selection.length !== 1) ||
    selection.some((item) => item.kind !== expectedKind) ||
    !build
  ) return;
  const target = multiFaceRemoval ? selection : selection[0];
  const command = {
    name,
    selection: target,
    build_id: build.build_id,
    bounds: build.summary.bounding_box,
  };
  if (name !== "remove_fillet" && name !== "remove_chamfer") {
    const distance = $("offset-distance");
    const value = Number(distance.value);
    const threshold = name === "offset" ? Math.abs(value) : value;
    if (!distance.value || !Number.isFinite(value) || !distance.checkValidity() || threshold <= 0.01) {
      distance.setCustomValidity(
        name === "offset"
          ? "Offset magnitude must be greater than 0.01 mm"
          : "Distance must be greater than 0.01 mm",
      );
      distance.reportValidity();
      distance.setCustomValidity("");
      return;
    }
    command.distance = value;
  }

  setStatus("applying manual edit…");
  const response = await fetch("/api/manual-command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  const result = await response.json();
  if (result.output?.includes("WARNING:")) console.warn("[Build warning]", result.output);
  else if (result.output) console.log("[Python]", result.output);
  if (!response.ok) {
    console.error("[Manual command failed]", result.detail || "manual edit failed");
    showError(result.detail || "manual edit failed");
    setStatus("manual edit failed", "error");
    return;
  }

  hideError();
  build = result;
  render(result.mesh);
  await setSelection([]);
  const s = result.summary;
  setStatus(`manual edit applied · ${s.faces} faces · ${s.edges} edges · volume ${s.volume} mm³${s.valid ? "" : " · INVALID SOLID"}`);
}
$("offset-feature").onclick = () => runManualCommand("offset");
$("chamfer-feature").onclick = () => runManualCommand("chamfer");
$("fillet-feature").onclick = () => runManualCommand("fillet");
$("remove-fillet-feature").onclick = () => runManualCommand("remove_fillet");
$("remove-chamfer-feature").onclick = () => runManualCommand("remove_chamfer");

function viewerSize() {
  const r = $("viewer").getBoundingClientRect();
  return [Math.floor(r.width), Math.floor(r.height)];
}

function render(mesh) {
  const [width, height] = viewerSize();
  let viewerOptions = { up: "Z" };
  if (viewer) {
    // Keep the user's camera across rebuilds.
    viewerOptions = {
      ...viewerOptions,
      position: viewer.getCameraPosition(),
      quaternion: viewer.getCameraQuaternion(),
      target: viewer.getCameraTarget(),
      zoom: viewer.getCameraZoom(),
    };
    viewer.clear();
  } else {
    const displayOptions = {
      cadWidth: width, height, treeWidth: 200, glass: true, tools: true, theme: "light",
      measureTools: false, selectTool: false, explodeTool: false,
    };
    viewer = new Viewer(new Display($("viewer"), displayOptions), displayOptions, () => {});
    viewer.setPickHandler(false);
    listenForPicks(viewer.renderer.domElement);
  }
  viewer.render(mesh, { edgeColor: 0x404040, defaultOpacity: 0.5 }, viewerOptions);
  fit();
}

// The viewer's toolbar sits above its canvas: give the canvas what is left.
function fit() {
  if (!viewer) return;
  const [width, height] = viewerSize();
  const toolbar = viewer.renderer.domElement.getBoundingClientRect().top - $("viewer").getBoundingClientRect().top;
  viewer.resizeCadView(width, 200, height - toolbar, true);
}
window.addEventListener("resize", fit);

// ---- picking: a click hits a triangle or an edge segment of the mesh; the
// per-face and per-edge counts of the mesh turn that into a face or edge index.

function listenForPicks(canvas) {
  let down = null;
  canvas.addEventListener("pointerdown", (e) => (down = [e.clientX, e.clientY]));
  canvas.addEventListener("pointerup", (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    const hit = pick(e.clientX, e.clientY);
    if (!hit) return e.shiftKey ? null : setSelection([]);
    const same = (s) => s.kind === hit.kind && s.index === hit.index;
    if (e.shiftKey) {
      setSelection(selection.some(same) ? selection.filter((s) => !same(s)) : [...selection, hit]);
    } else {
      setSelection([hit]);
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setSelection([]);
  });
}

function bucket(counts, i) {
  let acc = 0;
  for (let k = 0; k < counts.length; k++) {
    acc += counts[k];
    if (i < acc) return k;
  }
  return null;
}

function offset(counts, index) {
  let acc = 0;
  for (let k = 0; k < index; k++) acc += counts[k];
  return acc;
}

function pick(x, y) {
  const THREE = window.THREE; // exposed by three-cad-viewer
  const group = viewer?.nestedGroup?.groups[PART];
  if (!build || !group) return null;
  const rect = viewer.renderer.domElement.getBoundingClientRect();
  const ray = new THREE.Raycaster();
  ray.setFromCamera(
    new THREE.Vector2(((x - rect.left) / rect.width) * 2 - 1, -((y - rect.top) / rect.height) * 2 + 1),
    viewer.camera.getCamera(),
  );
  ray.params.Line2 = { threshold: 8 };
  const targets = [group.types.front, group.types.edges].filter(Boolean);
  const hits = ray.intersectObjects(targets, false);
  const face = hits.find((h) => h.object === group.types.front);
  const edge = hits.find((h) => h.object === group.types.edges);
  const size = Math.max(...build.summary.bounding_box.size);
  const useEdge = edge && (!face || edge.distance <= face.distance + size * 0.01);
  const shape = build.mesh.parts[0].shape;
  const kind = useEdge ? "edge" : face ? "face" : null;
  if (!kind) return null;
  const counts = kind === "edge" ? shape.segments_per_edge : shape.triangles_per_face;
  const index = bucket(counts, (useEdge ? edge : face).faceIndex);
  if (index === null) return null;
  const description = kind === "edge" ? build.edges[index] : build.faces[index];
  return { kind, index, ...description };
}

// ---- selection: shown under the viewer, highlighted in the scene, sent to the server

async function setSelection(items) {
  selection = items;
  $("offset-feature").disabled = items.length !== 1 || items[0].kind !== "face";
  $("chamfer-feature").disabled = items.length !== 1 || items[0].kind !== "edge";
  $("fillet-feature").disabled = items.length !== 1 || items[0].kind !== "edge";
  $("remove-fillet-feature").disabled =
    !items.length || items.some((item) => item.kind !== "face" || !["cylinder", "torus"].includes(item.type));
  $("remove-chamfer-feature").disabled =
    !items.length || items.some((item) => item.kind !== "face" || item.type !== "plane");
  $("selection-list").textContent = items.length
    ? items.map((s) => `${s.kind} ${s.index}: ${JSON.stringify({ ...s, kind: undefined, index: undefined })}`).join("\n")
    : "nothing selected";
  highlight();
  await fetch("/api/selection", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
}

function highlight() {
  const THREE = window.THREE;
  if (!viewer?.scene || !build) return;
  for (const o of overlays) {
    viewer.scene.remove(o);
    o.geometry.dispose();
    o.material.dispose();
  }
  overlays = [];
  const shape = build.mesh.parts[0].shape;
  for (const s of selection) {
    let object;
    if (s.kind === "face") {
      const start = offset(shape.triangles_per_face, s.index);
      const count = shape.triangles_per_face[s.index];
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(shape.vertices, 3));
      geometry.setIndex(Array.from(shape.triangles).slice(start * 3, (start + count) * 3));
      object = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
        color: 0xff7a00, side: THREE.DoubleSide, transparent: true, opacity: 0.8,
        polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
      }));
    } else {
      // A tube along the edge's segments: a 1 px line is invisible over the edge itself.
      const start = offset(shape.segments_per_edge, s.index);
      const count = shape.segments_per_edge[s.index];
      const points = shape.edges.flat(2).slice(start * 6, (start + count) * 6);
      const path = new THREE.CurvePath();
      for (let i = 0; i < points.length; i += 6) {
        path.add(new THREE.LineCurve3(new THREE.Vector3(...points.slice(i, i + 3)), new THREE.Vector3(...points.slice(i + 3, i + 6))));
      }
      const radius = Math.max(...build.summary.bounding_box.size) * 0.006;
      object = new THREE.Mesh(
        new THREE.TubeGeometry(path, Math.max(count * 2, 8), radius, 8, false),
        new THREE.MeshBasicMaterial({ color: 0xff3d00 }),
      );
    }
    viewer.scene.add(object);
    overlays.push(object);
  }
  viewer.update(true);
}

// ---- start

await loadScripts();
await loadScript();
await refreshBuild();

