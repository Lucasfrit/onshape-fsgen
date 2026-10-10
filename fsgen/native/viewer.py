"""Interactive 3D viewer: one self-contained HTML file (three.js from a CDN, meshes embedded as base64 STL).

Scenes: "As printed" (the built parts in their print layout) and, with a fit file, "Assembled" (parts in their
assembled position plus the components). Drag to turn, scroll to zoom, hide parts from the legend, and cut the model
in half to look inside. Opens straight from disk; the same file can be published as a web page.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .meshing import stl_base64, to_mm, triangles

PALETTE = ["#e0893e", "#4f7cc0", "#3f9a8a", "#b98a3e", "#8b79c9", "#c4552f", "#6b8f3a", "#c0607e"]


def scene_parts(items) -> list[dict]:
    """items: (label, shape in mm, color or None) -> viewer parts with embedded meshes."""
    out = []
    for i, (label, shape, color) in enumerate(items):
        out.append({"label": label, "color": color or PALETTE[i % len(PALETTE)],
                    "stl": stl_base64(triangles(shape, deflection=0.03, angle=0.12))})
    return out


def write_viewer(path: Path, title: str, scenes: list[dict], note: str = "") -> Path:
    """scenes: [{"name": str, "parts": scene_parts(...)}]."""
    data = json.dumps({"title": title, "scenes": scenes}, separators=(",", ":"))
    page = TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__NOTE__", html.escape(note))
    page = page.replace("__DATA__", data.replace("</", "<\\/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
    return path


def printed_items(solids_m, names=None):
    names = names or {}
    return [(names.get(i, f"Part {i + 1}"), to_mm(s), None) for i, s in enumerate(solids_m)]


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<style>
/* Layout: a large 3D stage with the controls above it and the part list beside it (stacked on phones). */
:root {
  --bg: #eceff3; --panel: #f8f9fb; --stage: #dfe4ea; --fg: #1b232e; --muted: #5a6573; --line: #c9d0d9;
  --accent: #24579a; --accent-ink: #ffffff;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; --mono: ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #141a22; --panel: #1b222c; --stage: #0f141a; --fg: #e3e8ef; --muted: #98a3b1; --line: #2c3542;
  --accent: #7fa8e0; --accent-ink: #0f141a; color-scheme: dark; } }
:root[data-theme="dark"] { --bg: #141a22; --panel: #1b222c; --stage: #0f141a; --fg: #e3e8ef; --muted: #98a3b1;
  --line: #2c3542; --accent: #7fa8e0; --accent-ink: #0f141a; color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 var(--font); }
.wrap { max-width: 1240px; margin: 0 auto; padding-inline: 20px; padding-block: 20px 32px; display: grid; gap: 16px; }
h1 { font-size: 1.6rem; margin: 0; text-wrap: balance; }
.controls { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; }
.seg { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--panel); }
.seg button, .tool, .chip { font: 500 .9rem var(--font); color: var(--fg); cursor: pointer; }
.seg button { background: transparent; border: 0; padding: 7px 13px; border-right: 1px solid var(--line); }
.seg button:last-child { border-right: 0; }
.seg button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }
.tool { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; }
.tool[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.main { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 16px; align-items: start; }
@media (max-width: 860px) { .main { grid-template-columns: minmax(0, 1fr); } }
.stage { position: relative; background: var(--stage); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  aspect-ratio: 16 / 10; max-width: 100%; min-height: 300px; }
.stage canvas { display: block; width: 100%; height: 100%; touch-action: none; }
.hint { position: absolute; left: 12px; bottom: 10px; font-size: .8rem; color: var(--muted); }
.status { position: absolute; right: 12px; bottom: 10px; font: .8rem var(--mono); color: var(--muted); }
aside { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; display: grid; gap: 8px; min-width: 0; }
.chip { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; background: transparent;
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; }
.chip .sw { width: 14px; height: 14px; border-radius: 3px; flex: none; }
.chip[aria-pressed="false"] { opacity: .45; }
.chip[aria-pressed="false"] .name { text-decoration: line-through; }
.note { font-size: .85rem; color: var(--muted); margin: 6px 0 0; }
</style></head><body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="controls">
    <div class="seg" role="group" aria-label="Scene" id="scenes"></div>
    <div class="seg" role="group" aria-label="Cut axis" id="cuts">
      <button id="cut-none" data-axis="" aria-pressed="true">Whole</button>
      <button id="cut-x" data-axis="x" aria-pressed="false">Cut X</button>
      <button id="cut-y" data-axis="y" aria-pressed="false">Cut Y</button>
      <button id="cut-z" data-axis="z" aria-pressed="false">Cut Z</button>
    </div>
    <button class="tool" id="reset">Reset view</button>
  </div>
  <div class="main">
    <div class="stage" id="stage"><span class="hint">Drag to turn · scroll or pinch to zoom · right-drag to pan</span>
      <span class="status" id="status">Loading…</span></div>
    <aside><div id="legend" aria-label="Parts (click to hide or show)"></div><p class="note">__NOTE__</p></aside>
  </div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/STLLoader.js"></script>
<script>
const DATA = JSON.parse(document.getElementById("data").textContent);
const stage = document.getElementById("stage"), statusEl = document.getElementById("status");
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
if (!window.THREE) { statusEl.textContent = "Could not load three.js (needs an internet connection)."; throw new Error("no three"); }
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.localClippingEnabled = true;
stage.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1.6, 0.5, 5000);
camera.up.set(0, 0, 1);
const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.75));
const key = new THREE.DirectionalLight(0xffffff, 0.75); key.position.set(60, -90, 120); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.3); fill.position.set(-80, 70, 40); scene.add(fill);
const clip = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);
const loader = new THREE.STLLoader();
const geoms = new Map();
function geometry(b64) {
  if (!geoms.has(b64)) {
    const bin = atob(b64), buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    const g = loader.parse(buf.buffer); g.computeVertexNormals(); geoms.set(b64, g);
  }
  return geoms.get(b64);
}
let current = 0, cutAxis = "", group = null;
const hidden = new Set();
function show(i, fit) {
  current = i;
  if (group) scene.remove(group);
  group = new THREE.Group();
  DATA.scenes[i].parts.forEach((p) => {
    const m = new THREE.Mesh(geometry(p.stl), new THREE.MeshStandardMaterial({ color: new THREE.Color(p.color),
      roughness: 0.6, metalness: 0.05, side: THREE.DoubleSide }));
    m.userData = p; m.visible = !hidden.has(p.label); group.add(m);
  });
  scene.add(group); applyCut(); if (fit) fitView(); legend();
  document.querySelectorAll("#scenes button").forEach((b, k) => b.setAttribute("aria-pressed", String(k === i)));
  statusEl.hidden = true;
}
function fitView() {
  const box = new THREE.Box3().setFromObject(group);
  const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
  const dist = r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 0.95;
  camera.position.copy(c).add(new THREE.Vector3(0.7, -1.2, 0.9).normalize().multiplyScalar(dist));
  controls.target.copy(c); controls.update();
}
function applyCut() {
  if (!group) return;
  const box = new THREE.Box3().setFromObject(group), c = box.getCenter(new THREE.Vector3());
  if (cutAxis) {
    const n = { x: [-1, 0, 0], y: [0, -1, 0], z: [0, 0, -1] }[cutAxis];
    clip.normal.set(...n); clip.constant = c[cutAxis];
  }
  group.children.forEach((m) => { m.material.clippingPlanes = cutAxis ? [clip] : []; m.material.needsUpdate = true; });
}
function legend() {
  const el = document.getElementById("legend"); el.replaceChildren();
  const seen = new Map();
  DATA.scenes[current].parts.forEach((p) => { if (!seen.has(p.label)) seen.set(p.label, p.color); });
  seen.forEach((color, label) => {
    const b = document.createElement("button"); b.className = "chip"; b.setAttribute("aria-pressed", String(!hidden.has(label)));
    const sw = document.createElement("span"); sw.className = "sw"; sw.style.background = color;
    const nm = document.createElement("span"); nm.className = "name"; nm.textContent = label;
    b.append(sw, nm);
    b.addEventListener("click", () => {
      if (hidden.has(label)) hidden.delete(label); else hidden.add(label);
      b.setAttribute("aria-pressed", String(!hidden.has(label)));
      group.children.forEach((m) => { if (m.userData.label === label) m.visible = !hidden.has(label); });
    });
    el.append(b);
  });
}
const scenesEl = document.getElementById("scenes");
DATA.scenes.forEach((s, i) => { const b = document.createElement("button"); b.id = "scene-" + i; b.textContent = s.name;
  b.addEventListener("click", () => show(i, true)); scenesEl.append(b); });
document.getElementById("cuts").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return;
  cutAxis = b.dataset.axis; document.querySelectorAll("#cuts button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); applyCut(); });
document.getElementById("reset").addEventListener("click", fitView);
function applyTheme() { renderer.setClearColor(new THREE.Color(css("--stage"))); }
new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
function resize() { const w = stage.clientWidth, h = stage.clientHeight; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }
new ResizeObserver(resize).observe(stage);
resize(); applyTheme(); show(DATA.scenes.length > 1 ? 1 : 0, true);
(function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
</script></body></html>
"""
