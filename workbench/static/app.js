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
    $("error").textContent = result.error + (result.output ? "\n" + result.output : "");
    $("error").hidden = false;
    setStatus("build failed", "error");
    return;
  }
  $("error").hidden = true;
  build = result;
  render(result.mesh);
  setSelection([]);
  const s = result.summary;
  setStatus(`built · ${s.faces} faces · ${s.edges} edges · volume ${s.volume} mm³${s.valid ? "" : " · INVALID SOLID"}`);
}

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

