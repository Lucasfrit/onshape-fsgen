"""Rebuild an FS-Lite file on every save and show it in a live browser viewer."""
from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

from .fslite.runner import describe, export, run_source

VIEWER = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>fsgen watch</title>
<style>
 body { margin: 0; font: 13px system-ui, sans-serif; background: #1d2026; color: #e6e6e6; }
 #info { position: absolute; top: 0; left: 0; right: 0; padding: 8px 12px; background: rgba(0,0,0,.55); white-space: pre-wrap; }
 #info.err { color: #ff8a80; }
</style>
<script type="importmap">{ "imports": {
  "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/" } }</script>
</head><body><div id="info">loading...</div>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight); document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x1d2026);
const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 0.1, 10000);
camera.up.set(0, 0, 1); camera.position.set(150, -200, 150);
const controls = new OrbitControls(camera, renderer.domElement);
scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.2));
const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(1, -2, 3); scene.add(sun);
const grid = new THREE.GridHelper(200, 20, 0x555555, 0x333333); grid.rotation.x = Math.PI / 2; scene.add(grid);
scene.add(new THREE.AxesHelper(30));
let mesh = null, version = -1, first = true;
const info = document.getElementById("info");
async function poll() {
  try {
    const s = await (await fetch("/status")).json();
    if (s.version !== version) {
      version = s.version;
      info.textContent = s.text; info.className = s.ok ? "" : "err";
      if (s.ok) {
        const geo = new STLLoader().parse(await (await fetch("/part.stl?v=" + version)).arrayBuffer());
        geo.computeVertexNormals();
        if (mesh) scene.remove(mesh);
        mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x8fb3e0, metalness: 0.1, roughness: 0.6 }));
        scene.add(mesh);
        if (first) { geo.computeBoundingSphere(); const c = geo.boundingSphere.center, r = geo.boundingSphere.radius;
          controls.target.copy(c); camera.position.set(c.x + 2*r, c.y - 2.5*r, c.z + 1.8*r); first = false; }
      }
    }
  } catch (e) { info.textContent = "viewer lost connection"; info.className = "err"; }
  setTimeout(poll, 500);
}
poll();
addEventListener("resize", () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
(function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
</script></body></html>"""


def watch(path: Path, port: int = 8765, params: dict | None = None):
    out = Path("out") / "watch" / path.stem
    state = {"version": 0, "ok": False, "text": "building..."}

    def build():
        t0 = time.time()
        res = run_source(path.read_text(), params=params)
        if res.ok:
            export(res, out, "part", formats=("stl", "step"))
        text = f"{path}  ({(time.time() - t0) * 1000:.0f} ms)\n" + describe(res)
        state.update(version=state["version"] + 1, ok=res.ok, text=text)
        print(text, flush=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                body, ctype = VIEWER.encode(), "text/html"
            elif self.path.startswith("/status"):
                body, ctype = json.dumps(state).encode(), "application/json"
            elif self.path.startswith("/part.stl"):
                body, ctype = (out / "part.stl").read_bytes(), "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    build()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"viewer: http://127.0.0.1:{port}  (edit {path}; Ctrl-C to stop)", flush=True)
    mtime = path.stat().st_mtime
    try:
        while True:
            time.sleep(0.3)
            m = path.stat().st_mtime
            if m != mtime:
                mtime = m
                build()
    except KeyboardInterrupt:
        srv.shutdown()
