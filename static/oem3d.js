/* Factory connector locator (3D).
   The car is the procedural MX-5 NA body from miata-hw review/visuals/car (lofted from the
   published dimensions, ISO 8855 frame: x forward from the front axle, y left, z up, mm).
   Each factory connector is a dot at its estimated anchor; dots that share an anchor are
   spread on a small ring. Tap a dot for its code and name, then open its page.
   Data: {anchors: {name: [x, y, z]}, markers: [{id, code, name, anchor, i, n, harness, focus}]} */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const CAR = { WB: 2265, L: 3970, H: 1235, TF: 1410, TR: 1430, FOH: 860, tyreD: 14 * 25.4 + 2 * 0.6 * 185, tyreW: 185 };
const XF = CAR.FOH, XR = -(CAR.L - CAR.FOH);
const HW = [[XF, 520], [XF - 60, 640], [XF - 200, 760], [500, 815], [200, 837], [-700, 832], [-1300, 818], [-1900, 830], [-2400, 837], [-2800, 825], [XR + 120, 760], [XR, 640]];
const ZB = [[XF, 260], [XF - 120, 170], [600, 140], [0, 135], [-2400, 135], [XR + 260, 150], [XR + 80, 250], [XR, 330]];
const ZT = [[XF, 455], [XF - 60, 540], [XF - 220, 620], [300, 680], [-300, 720], [-830, 785], [-1950, 800], [-2150, 835], [-2850, 845], [XR + 100, 800], [XR, 640]];
const COCK = [-860, -1960];
const HCOL = { F: 0x2f63d6, E: 0xd0261d, I: 0x2e9a4a, R: 0x8a3fc4, INJ: 0xf08a24, AC: 0x2aa6b8, DR1: 0x7a4a22, DR2: 0x7a4a22 };
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function interp(tbl, x) {
  if (x >= tbl[0][0]) return tbl[0][1];
  if (x <= tbl[tbl.length - 1][0]) return tbl[tbl.length - 1][1];
  for (let i = 0; i < tbl.length - 1; i++) {
    const [x0, v0] = tbl[i], [x1, v1] = tbl[i + 1];
    if (x <= x0 && x >= x1) { const t = (x0 - x) / (x0 - x1), s = t * t * (3 - 2 * t); return v0 + (v1 - v0) * s; }
  }
}

function body() {
  const NS = 120, NP = 64, N = 2.8, pos = [], idx = [], open = [];
  const cockpit = x => x < COCK[0] && x > COCK[1];
  for (let i = 0; i <= NS; i++) {
    const x = XF + (XR - XF) * i / NS, w = interp(HW, x), zb = interp(ZB, x), zt = interp(ZT, x), zc = (zb + zt) / 2, hz = (zt - zb) / 2;
    for (let j = 0; j < NP; j++) {
      const th = j / NP * Math.PI * 2, c = Math.cos(th), s = Math.sin(th);
      const y = w * Math.sign(c) * Math.pow(Math.abs(c), 2 / N), z = zc + hz * Math.sign(s) * Math.pow(Math.abs(s), 2 / N);
      pos.push(x, y, z);
      const endR = Math.min(x - COCK[1], COCK[0] - x), rail = 90 + Math.max(0, 180 - endR) * 1.2;
      open.push(cockpit(x) && s > 0 && Math.abs(y) < w - rail);
    }
  }
  for (let i = 0; i < NS; i++) for (let j = 0; j < NP; j++) {
    const a = i * NP + j, b = i * NP + (j + 1) % NP, c = (i + 1) * NP + j, d = (i + 1) * NP + (j + 1) % NP;
    if (open[a] || open[b] || open[c] || open[d]) continue;
    idx.push(a, c, b, b, c, d);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); g.setIndex(idx); g.computeVertexNormals();
  const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: 0xb3202a, roughness: .35, metalness: .15, side: THREE.DoubleSide,
    transparent: true, opacity: .16, depthWrite: false }));
  m.renderOrder = 10;
  return m;
}

function details(group) {
  const M = (c, o = {}) => new THREE.MeshStandardMaterial(Object.assign({ color: c, roughness: .6 }, o));
  const box = (w, d, h, m, x, y, z) => { const g = new THREE.Mesh(new THREE.BoxGeometry(w, d, h), m); g.position.set(x, y, z); return g; };
  const ghost = M(0x6c7176, { transparent: true, opacity: .35, depthWrite: false });
  [[0, CAR.TF / 2], [0, -CAR.TF / 2], [-CAR.WB, CAR.TR / 2], [-CAR.WB, -CAR.TR / 2]].forEach(([x, y]) => {
    const r = CAR.tyreD / 2, t = new THREE.Mesh(new THREE.CylinderGeometry(r, r, CAR.tyreW, 32), M(0x1b1c1e, { roughness: .9 }));
    t.position.set(x, y, r); group.add(t);
  });
  group.add(box(170, 1360, 230, ghost, -1020, 0, 690));            // dash
  group.add(box(1500, 270, 300, ghost, -1150, 0, 300));            // tunnel
  group.add(box(620, 480, 460, ghost, -60, 0, 520));               // engine
  group.add(box(800, 260, 240, ghost, -800, 0, 330));              // gearbox
  group.add(box(260, 1050, 430, ghost, -2120, 0, 520));            // tank
  [330, -330].forEach(y => { group.add(box(480, 460, 110, ghost, -1680, y, 260)); group.add(box(110, 460, 560, ghost, -1930, y, 560)); });
  const fw = box(30, 1300, 500, M(0x4b5156, { transparent: true, opacity: .18, depthWrite: false }), -760, 0, 600); // firewall
  group.add(fw);
}

export function mount(el, data, opts = {}) {
  const W = () => el.clientWidth, H = () => el.clientHeight;
  const R = new THREE.WebGLRenderer({ antialias: true });
  R.setPixelRatio(Math.min(devicePixelRatio, 2)); el.appendChild(R.domElement);
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe1e6e5);
  const cam = new THREE.PerspectiveCamera(35, 1, 20, 40000); cam.up.set(0, 0, 1);
  const ctl = new OrbitControls(cam, R.domElement); ctl.enableDamping = true; ctl.dampingFactor = .12;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x707a80, .9));
  const sun = new THREE.DirectionalLight(0xffffff, .8); sun.position.set(3000, 4000, 6000); scene.add(sun);
  const grid = new THREE.GridHelper(7000, 14, 0x7f9095, 0xaab7ba); grid.rotation.x = Math.PI / 2; grid.position.set(-1100, 0, 0); scene.add(grid);
  scene.add(body());
  const g = new THREE.Group(); details(g); scene.add(g);

  const pick = [], focus = [];
  for (const m of data.markers) {
    const a = data.anchors[m.anchor]; if (!a) continue;
    const ang = m.n > 1 ? (m.i / m.n) * Math.PI * 2 : 0, r = m.n > 1 ? 30 + 6 * m.n : 0;
    const p = new THREE.Vector3(a[0] + Math.cos(ang) * r, a[1] + Math.sin(ang) * r, a[2] + (m.i % 3) * 14);
    const s = new THREE.Mesh(new THREE.SphereGeometry(m.focus ? 34 : 18, 18, 12),
      new THREE.MeshStandardMaterial({ color: m.focus ? 0xff9a1a : (HCOL[m.harness] || 0x50585e), emissive: m.focus ? 0x803000 : 0, roughness: .4 }));
    s.position.copy(p); s.userData.m = m; s.renderOrder = 20; scene.add(s); pick.push(s);
    if (m.focus) focus.push(p);
  }
  const target = focus[0] || new THREE.Vector3(-900, 0, 500);
  const k = focus.length ? .55 : 1;
  cam.position.set(target.x + 2600 * k, target.y + 2400 * k, target.z + 2000 * k); ctl.target.copy(target); ctl.update();

  const ray = new THREE.Raycaster(), v = new THREE.Vector2();
  let down = null;
  R.domElement.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
  R.domElement.addEventListener("pointerup", e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6) return;
    const r = R.domElement.getBoundingClientRect();
    v.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(v, cam);
    const hit = ray.intersectObjects(pick)[0];
    if (!hit || !opts.info) return;
    const m = hit.object.userData.m;
    const same = data.markers.filter(x => x.anchor === m.anchor);
    opts.info.hidden = false;
    opts.info.innerHTML = `<strong>${esc(m.code)}</strong> ${esc(m.name)} <a class="btn mini" href="${opts.url.replace("__C__", encodeURIComponent(m.id))}">Open ›</a>`
      + (same.length > 1 ? `<div class="dim sm">Same place: ${same.filter(x => x.id !== m.id).map(x =>
        `<a href="${opts.url.replace("__C__", encodeURIComponent(x.id))}">${esc(x.code)}</a>`).join(" · ")}</div>` : "");
  });
  const size = () => { const w = W(), h = H(); R.setSize(w, h, false); cam.aspect = w / h; cam.fov = w < 560 ? 50 : 35; cam.updateProjectionMatrix(); };
  new ResizeObserver(size).observe(el); size();
  (function loop() { ctl.update(); R.render(scene, cam); requestAnimationFrame(loop); })();
}
