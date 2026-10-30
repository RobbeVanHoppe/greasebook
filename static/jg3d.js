/* 3D job-guide figures (model format jg-model/1).
   A model is plain data: primitives, toothed and V pulleys, belts routed over
   them, and bolt groups with their torque. Bundles never carry code. A view
   (one per figure, optionally overridden per step) picks the camera and what
   is hidden, ghosted, highlighted, moved apart and tagged.
   Every figure on a page shares one WebGL renderer and draws into its own 2D
   canvas, only when something changed.
   Master copy: greasebook/static/jg3d.js — copied to miata-hw/to/jg-web/jg3d.js. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const D2R = Math.PI / 180, TAU = Math.PI * 2, V3 = THREE.Vector3, ZERO = new V3(), FOV = 30;
const COARSE = matchMedia("(pointer: coarse)").matches;
const HI = new THREE.Color("#ff9a1a");
const EDGE = new THREE.LineBasicMaterial({ color: 0x06080b, transparent: true, opacity: .42 });
const EDGE_G = new THREE.LineBasicMaterial({ color: 0x06080b, transparent: true, opacity: .07, depthWrite: false });
const ARROW = new THREE.MeshBasicMaterial({ color: HI });
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let R = null, raf = 0, io = null, ro = null;
const scenes = new Map(), views = new Set(), variants = new Map();

// ------------------------------------------------------------------ geometry

function path2d(spec, P) {           // [[x,y],…] | {c:[x,y,r]} | {rr:[x0,y0,x1,y1,radius]}
  const p = new P();
  if (Array.isArray(spec)) {
    spec.forEach(([x, y], i) => i ? p.lineTo(x, y) : p.moveTo(x, y));
    p.closePath();
  } else if (spec.c) {
    const [x, y, r] = spec.c;
    p.absarc(x, y, r, 0, TAU, false);
  } else if (spec.rr) {
    const [x0, y0, x1, y1, r] = spec.rr, H = Math.PI / 2;
    p.moveTo(x0 + r, y0); p.lineTo(x1 - r, y0); p.absarc(x1 - r, y0 + r, r, -H, 0, false);
    p.lineTo(x1, y1 - r); p.absarc(x1 - r, y1 - r, r, 0, H, false);
    p.lineTo(x0 + r, y1); p.absarc(x0 + r, y1 - r, r, H, 2 * H, false);
    p.lineTo(x0, y0 + r); p.absarc(x0 + r, y0 + r, r, 2 * H, 3 * H, false);
  }
  return p;
}

const alongZ = g => g.rotateX(Math.PI / 2);         // three's Y-axis solids → our Z axis
const lathe = (pts, seg) => alongZ(new THREE.LatheGeometry(pts.map(([r, z]) => new THREE.Vector2(r, z)), seg || 64));

function ext(p) {
  const s = path2d(p.shape, THREE.Shape), b = p.bevel || 0;
  (p.holes || []).forEach(h => s.holes.push(path2d(h, THREE.Path)));
  return new THREE.ExtrudeGeometry(s, { depth: p.depth - 2 * b, bevelEnabled: b > 0, bevelThickness: b,
    bevelSize: b, bevelOffset: -b, bevelSegments: 2, curveSegments: 24 }).translate(0, 0, (p.z0 || 0) + b);
}

function shell(p) {                  // moulded cover: front plate + rim, outline is a rounded rectangle
  const [x0, y0, x1, y1, r] = p.rr, t = p.t || 3;
  const front = path2d({ rr: p.rr }, THREE.Shape);
  (p.holes || []).forEach(h => front.holes.push(path2d(h, THREE.Path)));
  const rim = path2d({ rr: p.rr }, THREE.Shape);
  rim.holes.push(path2d({ rr: [x0 + t, y0 + t, x1 - t, y1 - t, Math.max(r - t, 1)] }, THREE.Path));
  const o = { bevelEnabled: false, curveSegments: 20 };
  return [{ g: new THREE.ExtrudeGeometry(front, { ...o, depth: t }).translate(0, 0, p.z0 + p.depth - t) },
          { g: new THREE.ExtrudeGeometry(rim, { ...o, depth: p.depth - t }).translate(0, 0, p.z0) }];
}

function sprocket(p) {               // toothed belt pulley; r = pitch radius
  const s = new THREE.Shape(), n = p.teeth, ro = p.r, ri = p.r - 3.2, pitch = TAU / n;
  for (let i = 0; i < n; i++) {
    const a = i * pitch;
    [[ro, a], [ro, a + .48 * pitch], [ri, a + .6 * pitch], [ri, a + .88 * pitch]].forEach(([rr, aa], k) => {
      const x = rr * Math.cos(aa), y = rr * Math.sin(aa);
      i || k ? s.lineTo(x, y) : s.moveTo(x, y);
    });
  }
  s.closePath();
  const hr = p.hub || ro * .34, hubLen = p.hubLen ?? 6;
  if (p.holes) {
    const rm = (hr + ri) / 2, rh = Math.min((ri - hr) * .33, rm * Math.sin(Math.PI / p.holes) * .62);
    for (let k = 0; k < p.holes; k++) {
      const a = k * TAU / p.holes + Math.PI / 2;
      s.holes.push(path2d({ c: [rm * Math.cos(a), rm * Math.sin(a), rh] }, THREE.Path));
    }
  }
  const out = [{ g: new THREE.ExtrudeGeometry(s, { depth: p.w, bevelEnabled: false, curveSegments: 6 }).translate(0, 0, p.z0) },
               { g: alongZ(new THREE.CylinderGeometry(hr, hr, p.w + hubLen, 40)).translate(0, 0, p.z0 + (p.w + hubLen) / 2) }];
  if (p.flange) for (const z of [p.z0 - .8, p.z0 + p.w + .8])
    out.push({ g: alongZ(new THREE.CylinderGeometry(ro + 3, ro + 3, 1.6, 72)).translate(0, 0, z) });
  return out;
}

function vpulley(p) {                // plain or V-groove pulley turned about Z
  const n = p.grooves || 0, b = p.bore || 3, z0 = p.z0, w = p.w, pts = [[b, z0], [p.r - 1, z0], [p.r, z0 + 1]];
  const gw = n ? w / n : w, d = Math.min(6, gw * .42);
  for (let i = 0; i < n; i++) {
    const z = z0 + i * gw;
    pts.push([p.r, z + gw * .14], [p.r - d, z + gw * .5], [p.r, z + gw * .86]);
  }
  pts.push([p.r, z0 + w - 1], [p.r - 1, z0 + w], [b, z0 + w], [b, z0]);
  return [{ g: lathe(pts, 72) }];
}

function beltPath(p, S) {            // closed loop, route listed counter-clockwise seen from +Z
  const t = p.t || 4;
  const P = p.route.map(([id, s]) => {
    const q = S.parts[id], R0 = (q.br ?? q.r) + (s < 0 ? t : 0);
    return { c: new THREE.Vector2(q.pos[0], q.pos[1]), rho: s * R0, R: R0, s };
  });
  const n = P.length, tan = [];
  for (let i = 0; i < n; i++) {      // tangent with the same right-hand normal at both pulleys
    const a = P[i], b = P[(i + 1) % n], d = b.c.clone().sub(a.c), L = d.length(), al = (a.rho - b.rho) / L;
    const dh = d.divideScalar(L), nr = new THREE.Vector2(dh.y, -dh.x);
    const nv = dh.clone().multiplyScalar(al).add(nr.multiplyScalar(Math.sqrt(Math.max(0, 1 - al * al))));
    tan.push([a.c.clone().addScaledVector(nv, a.rho), b.c.clone().addScaledVector(nv, b.rho)]);
  }
  const pts = [];
  for (let j = 0; j < n; j++) {
    const q = P[j], pin = tan[(j - 1 + n) % n][1], pout = tan[j][0];
    const a0 = Math.atan2(pin.y - q.c.y, pin.x - q.c.x), a1 = Math.atan2(pout.y - q.c.y, pout.x - q.c.x);
    const sw = q.s > 0 ? ((a1 - a0) % TAU + TAU) % TAU : -(((a0 - a1) % TAU + TAU) % TAU);
    const k = Math.max(2, Math.ceil(Math.abs(sw) / (4 * D2R)));
    for (let i = 0; i <= k; i++) {
      const a = a0 + sw * i / k;
      pts.push(new THREE.Vector2(q.c.x + q.R * Math.cos(a), q.c.y + q.R * Math.sin(a)));
    }
  }
  return pts.filter((q, i) => q.distanceTo(pts[(i + 1) % pts.length]) > .05);
}

function belt(p, S) {
  const pts = beltPath(p, S), n = pts.length, t = p.t || 4, z0 = p.z0, z1 = z0 + p.w;
  const nr = pts.map((q, i) => {
    const d = pts[(i + 1) % n].clone().sub(pts[(i - 1 + n) % n]).normalize();
    return new THREE.Vector2(d.y, -d.x);           // points out of the loop
  });
  const pos = [], idx = [];
  const strip = f => {
    const base = pos.length / 3;
    for (let i = 0; i < n; i++) pos.push(...f(i, 0), ...f(i, 1));
    for (let i = 0; i < n; i++) {
      const a = base + 2 * i, c = base + 2 * ((i + 1) % n);
      idx.push(a, c, a + 1, a + 1, c, c + 1);
    }
  };
  const at = (i, out, z) => [pts[i].x + nr[i].x * out, pts[i].y + nr[i].y * out, z];
  strip((i, k) => at(i, 0, k ? z1 : z0));
  strip((i, k) => at(i, t, k ? z1 : z0));
  strip((i, k) => at(i, k ? t : 0, z0));
  strip((i, k) => at(i, k ? t : 0, z1));
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx); g.computeVertexNormals();
  const out = [{ g, edges: false }];
  if (p.pitch) {                     // teeth on the inside of the loop
    const mats = [], m = new THREE.Matrix4(), q = new THREE.Quaternion(), Z = new V3(0, 0, 1);
    let acc = 0;
    for (let i = 0; i < n; i++) {
      const a = pts[i], b = pts[(i + 1) % n], L = a.distanceTo(b);
      while (acc <= L) {
        const f = acc / L, x = a.x + (b.x - a.x) * f, y = a.y + (b.y - a.y) * f;
        q.setFromAxisAngle(Z, Math.atan2(b.y - a.y, b.x - a.x));
        mats.push(m.clone().compose(new V3(x - nr[i].x * 1.1, y - nr[i].y * 1.1, (z0 + z1) / 2), q, new V3(1, 1, 1)));
        acc += p.pitch;
      }
      acc -= L;
    }
    out.push({ g: new THREE.BoxGeometry(p.pitch * .46, 2.2, p.w - .4), inst: mats });
  }
  return out;
}

function pads(p) {                    // PCB through-hole pads: one flat ring per [x, y, z, outer r, thickness]
  const ri = p.ri ?? .55, g = new THREE.LatheGeometry([[ri, -.5], [1, -.5], [1, .5], [ri, .5], [ri, -.5]]
    .map(([r, y]) => new THREE.Vector2(r, y)), 20), q = new THREE.Quaternion();
  return [{ g, edges: false, inst: p.at.map(([x, y, z, r, h]) => new THREE.Matrix4().compose(new V3(x, y, z), q, new V3(r, h || 1, r))) }];
}

const BUILD = {
  box: p => [{ g: new THREE.BoxGeometry(...p.size) }],
  cyl: p => [{ g: alongZ(new THREE.CylinderGeometry(p.r2 ?? p.r, p.r, p.h, p.seg || 48)) }],
  lathe: p => [{ g: lathe(p.pts, p.seg) }],
  ext: p => [{ g: ext(p) }],
  tube: p => [{ g: new THREE.TubeGeometry(new THREE.CatmullRomCurve3(p.pts.map(a => new V3(...a))), 64, p.r, 18, false), edges: false }],
  shell, sprocket, pulley: vpulley, belt, pads,
};

const boltGeo = new Map();
function boltParts(af, hh) {
  const k = af + ":" + hh;
  if (!boltGeo.has(k)) {
    const rc = af / Math.sqrt(3), wr = af * .95;
    boltGeo.set(k, [alongZ(new THREE.CylinderGeometry(rc, rc, hh, 6)).translate(0, 0, 1.6 + hh / 2),
                    alongZ(new THREE.CylinderGeometry(wr, wr, 1.6, 32)).translate(0, 0, .8)]);
  }
  return boltGeo.get(k);
}

// ------------------------------------------------------------------ scene

function material(m) {
  return new THREE.MeshStandardMaterial({ color: m.c || "#8a8f96", metalness: m.m ?? .2, roughness: m.r ?? .6,
    side: THREE.DoubleSide, envMapIntensity: m.env ?? 1, emissive: m.e || "#000000" });
}

function variant(m, kind) {
  let v = variants.get(m);
  if (!v) {
    const hi = m.clone(); hi.emissive = HI.clone(); hi.emissiveIntensity = .5;
    const ghost = m.clone(); ghost.transparent = true; ghost.opacity = .12; ghost.depthWrite = false;
    variants.set(m, v = { hi, ghost });
  }
  return v[kind];
}

function build(model) {
  const scene = new THREE.Scene(), mats = {}, roots = [], byId = {}, parts = {};
  for (const [k, m] of Object.entries(model.mats || {})) mats[k] = material(m);
  const mat = k => mats[k] || (mats[k] = material({}));
  model.parts.forEach(p => parts[p.id] = p);
  const reg = (root, def, kind) => {
    root.userData = { id: def.id, def, kind, hidden: !!def.hidden, p0: root.position.clone(),
                      grps: new Set([def.id, kind === "bolt" ? "bolts" : "parts", ...[].concat(def.grp || [])]) };
    scene.add(root); roots.push(root); byId[def.id] = root;
    root.updateMatrixWorld(true);
    root.userData.c0 = new THREE.Box3().setFromObject(root).getCenter(new V3());
  };
  const add = (root, g, m, edges, shadow = true) => {
    const mesh = new THREE.Mesh(g, m);
    mesh.castShadow = mesh.receiveShadow = shadow;
    mesh.userData.m0 = m;
    if (edges) { const l = new THREE.LineSegments(new THREE.EdgesGeometry(g, 30), EDGE); mesh.add(l); }
    root.add(mesh);
  };
  for (const p of model.parts) {
    const make = BUILD[p.type];
    if (!make) continue;
    const root = new THREE.Group();
    for (const e of make(p, { parts })) {
      const m = mat(e.mat || p.mat);
      if (e.inst) {
        const im = new THREE.InstancedMesh(e.g, m, e.inst.length);
        e.inst.forEach((x, i) => im.setMatrixAt(i, x));
        im.castShadow = true; im.userData.m0 = m; root.add(im);
      } else add(root, e.g, m, p.edges !== false && e.edges !== false);
    }
    if (p.pos && p.type !== "belt") root.position.fromArray(p.pos);
    if (p.rot) root.rotation.set(...p.rot.map(a => a * D2R));
    reg(root, p, "part");
  }
  const Z = new V3(0, 0, 1);
  for (const b of model.bolts || []) {
    const root = new THREE.Group(), af = b.head || 12, [hex, wash] = boltParts(af, b.hh || +(af * .62).toFixed(1));
    const dir = new V3(...(b.dir || [0, 0, 1])).normalize(), q = new THREE.Quaternion().setFromUnitVectors(Z, dir);
    const pts = [];
    for (const at of b.at) {
      const one = new THREE.Group();
      add(one, hex, mat(b.mat || "bolt"), true);
      add(one, wash, mat(b.wmat || b.mat || "bolt"), false);
      one.position.fromArray(at); one.quaternion.copy(q);
      root.add(one); pts.push(new V3(...at).addScaledVector(dir, 4));
    }
    reg(root, b, "bolt");
    root.userData.pts = pts;
  }
  const lo = new THREE.Box3().setFromObject(scene);
  scene.add(new THREE.HemisphereLight(0xe4ecff, 0x2b2620, .55));
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(520, 950, 900); key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048); key.shadow.bias = -.0004; key.shadow.normalBias = .6;
  Object.assign(key.shadow.camera, { left: -700, right: 700, top: 800, bottom: -700, near: 200, far: 3200 });
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xc6d6ff, .55);
  fill.position.set(-800, 250, 500); scene.add(fill);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(4000, 4000), new THREE.ShadowMaterial({ opacity: .28 }));
  ground.rotation.x = -Math.PI / 2; ground.position.y = model.ground ?? lo.min.y - 2; ground.receiveShadow = true;
  scene.add(ground);
  const pm = new THREE.PMREMGenerator(R);
  scene.environment = pm.fromScene(new RoomEnvironment(R), .04).texture;
  pm.dispose();
  return { scene, roots, byId, model };
}

// ------------------------------------------------------------------ views

const sels = s => [].concat(s ?? []);
const match = (o, sel) => sels(sel).some(s => s === "*" || o.userData.grps.has(s));

function stateFor(S, view) {
  const st = new Map(), off = new Map();
  for (const o of S.roots) {
    let k = o.userData.hidden && !match(o, view.show) ? "hide" : undefined;
    if (match(o, view.hide)) k = "hide";
    if (k !== "hide") {
      if (match(o, view.ghost)) k = "ghost";
      if (match(o, view.hi)) k = "hi";
    }
    if (k) st.set(o, k);
    for (const [sel, d] of Object.entries(view.move || {})) if (match(o, sel)) off.set(o, new V3(...d));
  }
  return { st, off };
}

function apply(v) {
  for (const o of v.S.roots) {
    const k = o === v.picked ? "hi" : v.st.get(o);
    o.visible = k !== "hide";
    if (!o.visible) continue;
    o.position.copy(o.userData.p0).add(v.off.get(o) || ZERO);
    o.traverse(c => {
      if (c.isMesh) { c.material = k === "hi" || k === "ghost" ? variant(c.userData.m0, k) : c.userData.m0; c.castShadow = k !== "ghost"; }
      else if (c.isLineSegments) c.material = k === "ghost" ? EDGE_G : EDGE;
    });
  }
}

function annotations(view) {
  const g = new THREE.Group();
  for (const a of view.rot || []) {          // turning arrow about +Z at a point
    const r = a.r || 40, arc = 1.45 * Math.PI, w = Math.max(2.2, r * .07), one = new THREE.Group();
    one.add(new THREE.Mesh(new THREE.TorusGeometry(r, w, 10, 64, arc), ARROW));
    const head = new THREE.Mesh(new THREE.ConeGeometry(w * 3, w * 7, 20), ARROW);
    head.position.set(r * Math.cos(arc), r * Math.sin(arc), 0); head.rotation.z = arc;
    one.add(head);
    one.position.fromArray(a.at); one.rotation.z = (a.start || 0) * D2R;
    if (a.dir === "cw") one.scale.x = -1;
    g.add(one);
  }
  for (const a of view.arrows || []) {       // straight arrow, e.g. "pull forward"
    const from = new V3(...a.from), to = new V3(...a.to), d = to.clone().sub(from), L = d.length(), w = a.w || 3;
    const one = new THREE.Group(), shaft = new THREE.Mesh(new THREE.CylinderGeometry(w, w, L - w * 6, 16), ARROW);
    shaft.position.y = (L - w * 6) / 2; one.add(shaft);
    const head = new THREE.Mesh(new THREE.ConeGeometry(w * 2.6, w * 6, 20), ARROW);
    head.position.y = L - w * 3; one.add(head);
    one.position.copy(from); one.quaternion.setFromUnitVectors(new V3(0, 1, 0), d.normalize());
    g.add(one);
  }
  return g;
}

function anchor(v, at) {
  if (Array.isArray(at)) return new V3(...at);
  const [id, k] = String(at).split("#"), o = v.S.byId[id];
  if (!o || v.st.get(o) === "hide") return null;
  const off = v.off.get(o) || ZERO;
  return (o.userData.pts ? o.userData.pts[+k || 0] : o.userData.c0).clone().add(off);
}

function boltLabel(o) {
  const d = o.userData.def, n = d.at.length;
  return d.spec + (n > 1 ? ` ×${n}` : "");
}

function buildTags(v) {
  const view = v.view, list = [];
  for (const [n, at, dx = 0, dy = 0] of view.callouts || []) list.push({ kind: "co", text: n, at, dx, dy });
  let tq = sels(view.torque);
  if (tq.includes("*")) tq = v.S.roots.filter(o => o.userData.kind === "bolt" && o.userData.def.spec).map(o => o.userData.id);
  for (const e of tq) {
    const [id, dx = 16, dy = -20] = [].concat(e), o = v.S.byId[id];
    if (o && o.userData.def.spec) list.push({ kind: "tq", text: boltLabel(o), at: id, o, dx, dy });
  }
  for (const [text, at, dx = 0, dy = 0] of view.labels || []) list.push({ kind: "lb", text, at, dx, dy });
  v.tagsEl.innerHTML = '<svg class="jg3d-lines" aria-hidden="true"></svg>';
  v.svg = v.tagsEl.firstChild;
  v.tags = list.map(t => {
    const pin = document.createElement("div");
    pin.className = "jg3d-pin";
    const b = document.createElement(t.o ? "button" : "span");
    b.className = "jg3d-" + t.kind; b.textContent = t.text;
    if (t.o && t.o.userData.def.short) b.insertAdjacentHTML("afterbegin", `<span class="nm">${esc(t.o.userData.def.short)} · </span>`);
    if (t.o) {
      b.type = "button"; b.onclick = () => showInfo(v, t.o);
      b.setAttribute("aria-label", `${t.o.userData.def.name || t.o.userData.id}: ${t.text}`);
    }
    if (t.kind !== "co" || t.dx || t.dy) {
      b.style.left = t.dx + "px"; b.style.top = t.dy + "px";
      if (t.dx < 0) b.classList.add("l");
      if (t.kind !== "lb" || t.dx || t.dy) pin.insertAdjacentHTML("afterbegin", '<i class="jg3d-dot"></i>');
      t.line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      t.line.setAttribute("class", t.kind);
      v.svg.appendChild(t.line);
    }
    pin.appendChild(b); v.tagsEl.appendChild(pin);
    return { ...t, pin };
  });
}

function placeTags(v) {
  const { w, h } = v.css, p = new V3();
  for (const t of v.tags) {
    const a = anchor(v, t.at);
    if (a) p.copy(a).project(v.cam);
    const on = a && v.showTags && p.z < 1;
    t.pin.hidden = !on;
    if (t.line) t.line.style.display = on && (t.dx || t.dy) ? "" : "none";
    if (!on) continue;
    const x = (p.x + 1) / 2 * w, y = (1 - p.y) / 2 * h;
    t.pin.style.transform = `translate(${x.toFixed(1)}px,${y.toFixed(1)}px)`;
    if (t.line) { t.line.setAttribute("x1", x); t.line.setAttribute("y1", y); t.line.setAttribute("x2", x + t.dx); t.line.setAttribute("y2", y + t.dy); }
  }
}

function showInfo(v, o) {
  const d = o.userData.def, bolt = o.userData.kind === "bolt";
  const meta = [d.head && `${d.head} mm socket`, bolt && d.at.length > 1 && `${d.at.length} off`, d.note].filter(Boolean);
  v.info.innerHTML = `<button type="button" class="x" aria-label="Close">×</button><div class="nm">${esc(d.name || d.id)}</div>` +
    (d.spec ? `<div class="sp">${esc(d.spec)}</div>` : "") + (meta.length ? `<div class="mt">${meta.map(esc).join(" · ")}</div>` : "");
  v.info.hidden = false;
  v.info.querySelector(".x").onclick = () => { v.info.hidden = true; v.picked = null; v.dirty = true; kick(); };
  v.picked = o; v.dirty = true; kick();
}

function setView(v, view, animate = true) {
  v.view = view;
  const { st, off } = stateFor(v.S, view);
  v.st = st; v.picked = null; v.info.hidden = true;
  v.from = { p: v.cam.position.clone(), t: v.ctrl.target.clone(), off: new Map(v.off) };
  v.to = { p: new V3(...(view.cam || [420, 330, 760])), t: new V3(...(view.look || [0, 170, 0])), off };
  if (v.annot) v.annot.removeFromParent();
  v.annot = annotations(view);
  buildTags(v);
  v.tw = { t0: performance.now(), dur: animate ? 650 : 0 };
  v.dirty = true; kick();
}

function stepTween(v, now) {
  const k = v.tw.dur ? Math.min(1, (now - v.tw.t0) / v.tw.dur) : 1, e = k < .5 ? 2 * k * k : 1 - (2 - 2 * k) ** 2 / 2;
  v.cam.position.lerpVectors(v.from.p, v.to.p, e);
  v.ctrl.target.lerpVectors(v.from.t, v.to.t, e);
  const off = new Map();
  for (const o of new Set([...v.from.off.keys(), ...v.to.off.keys()]))
    off.set(o, new V3().lerpVectors(v.from.off.get(o) || ZERO, v.to.off.get(o) || ZERO, e));
  v.off = off;
  if (k >= 1) v.tw = null;
}

// ------------------------------------------------------------------ render loop

const kick = () => { if (!raf) raf = requestAnimationFrame(tick); };

function tick(now) {
  raf = 0;
  let more = false;
  for (const v of views) {
    if (v.tw) { stepTween(v, now); v.dirty = more = true; }
    if (v.ctrl.update()) v.dirty = more = true;
    if (v.dirty && v.on && v.px.w && v.px.h) { draw(v); v.dirty = false; }
  }
  if (more) kick();
}

const tmp = new THREE.Vector2();
function draw(v) {
  const { w, h } = v.px;
  R.getSize(tmp);
  if (tmp.x < w || tmp.y < h) R.setSize(Math.max(tmp.x, w), Math.max(tmp.y, h), false);
  const H = R.getSize(tmp).y;
  R.setViewport(0, 0, w, h); R.setScissor(0, 0, w, h); R.setScissorTest(true);
  v.cam.aspect = w / h;               // keep at least the 4:3 horizontal field on tall screens
  v.cam.fov = v.cam.aspect >= 4 / 3 ? FOV : 2 * Math.atan(Math.tan(FOV * D2R / 2) * (4 / 3) / v.cam.aspect) / D2R;
  v.cam.updateProjectionMatrix();
  apply(v);
  v.S.scene.add(v.annot);
  R.setClearColor(0, 0); R.clear();
  R.render(v.S.scene, v.cam);
  v.S.scene.remove(v.annot);
  v.ctx.clearRect(0, 0, w, h);
  v.ctx.drawImage(R.domElement, 0, H - h, w, h, 0, 0, w, h);
  placeTags(v);
}

function resize(v) {
  const dpr = Math.min(devicePixelRatio || 1, 2), w = v.el.clientWidth, h = v.el.clientHeight;
  v.css = { w, h }; v.px = { w: Math.round(w * dpr), h: Math.round(h * dpr) };
  v.canvas.width = v.px.w; v.canvas.height = v.px.h;
  v.dirty = true; kick();
}

// ------------------------------------------------------------------ mounting

const CSS = `
.jg3d{container-type:inline-size;position:relative;width:100%;aspect-ratio:4/3;overflow:hidden;border-radius:4px;user-select:none;-webkit-user-select:none;
  background:var(--jg3d-bg,radial-gradient(ellipse at 50% 30%,#3b424c 0%,#1b1f25 70%,#121418 100%));color:#eef1f4}
.jg3d canvas{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:pan-y}
.jg3d.jg3d-full{position:fixed;inset:0;z-index:1000;aspect-ratio:auto;border-radius:0;height:100%}
html.jg3d-lock,html.jg3d-lock body{overflow:hidden}
.jg3d-tags{position:absolute;inset:0;pointer-events:none}
.jg3d-lines{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.jg3d-lines line{stroke:var(--jg3d-tq,#ffb547);stroke-width:1.5}
.jg3d-lines line.co,.jg3d-lines line.lb{stroke:#fff;stroke-width:1.2;filter:drop-shadow(0 0 1px #000)}
.jg3d-pin{position:absolute;left:0;top:0;width:0;height:0}
.jg3d-pin>*{position:absolute;pointer-events:auto;white-space:nowrap}
.jg3d-co{width:24px;height:24px;margin:-12px 0 0 -12px;display:grid;place-items:center;border-radius:50%;
  background:#fff;color:#111;border:2px solid #111;font:700 12px/1 ui-monospace,Consolas,monospace;box-shadow:0 1px 3px #0009}
.jg3d-dot{width:9px;height:9px;margin:-4.5px 0 0 -4.5px;border-radius:50%;background:var(--jg3d-tq,#ffb547);border:2px solid #111}
.jg3d-co+.jg3d-dot,.jg3d-dot:has(+.jg3d-co){background:#fff}
.jg3d-tq,.jg3d-lb{transform:translateY(-50%);padding:4px 7px;border-radius:3px;font:700 12px/1.1 ui-monospace,Consolas,monospace;box-shadow:0 1px 4px #000a}
.jg3d-tq{background:var(--jg3d-tq,#ffb547);color:#14161a;border:1px solid #14161a;cursor:pointer}
.jg3d-lb{background:#14161ae0;color:#eef1f4;border:1px solid #fff4;font-weight:500}
.jg3d-tq.l,.jg3d-lb.l{transform:translate(-100%,-50%)}
.jg3d-tq .nm{font-weight:500}
@container (max-width:560px){.jg3d-tq .nm{display:none}}
.jg3d-ui{position:absolute;top:6px;right:6px;display:flex;gap:6px}
.jg3d-ui button{min-width:38px;height:38px;padding:0 8px;border-radius:4px;border:1px solid #fff5;background:#0b0d10b3;color:#fff;
  font:600 12px system-ui,sans-serif;cursor:pointer}
.jg3d-ui button[aria-pressed="false"]{opacity:.55}
.jg3d-hint{position:absolute;left:8px;bottom:6px;right:52px;font:11.5px system-ui,sans-serif;color:var(--jg3d-dim,#ffffffb0);pointer-events:none;text-shadow:var(--jg3d-shadow,0 1px 2px #000)}
.jg3d-note{position:absolute;left:8px;top:8px;font:600 10.5px ui-monospace,Consolas,monospace;letter-spacing:.06em;color:var(--jg3d-dim,#ffffff90);pointer-events:none}
.jg3d-info{position:absolute;left:8px;right:8px;bottom:8px;max-width:420px;padding:9px 34px 9px 11px;border-radius:6px;
  background:#0c0e12ed;border:1px solid #fff3;color:#eef1f4;font:13px/1.35 system-ui,sans-serif}
.jg3d-info .nm{font-weight:600}
.jg3d:has(.jg3d-info:not([hidden])) .jg3d-hint{display:none}
.jg3d-info .sp{font:700 19px ui-monospace,Consolas,monospace;color:var(--jg3d-tq,#ffb547);margin-top:2px}
.jg3d-info .mt{color:#ffffffb0;font-size:12.5px;margin-top:2px}
.jg3d-info .x{position:absolute;top:4px;right:4px;width:28px;height:28px;border:0;background:none;color:#fff;font-size:20px;cursor:pointer}
.jg3d-msg{position:absolute;inset:0;display:grid;place-items:center;padding:16px;text-align:center;font:13px system-ui,sans-serif;color:#ffffffc0}
`;

function css() {
  if (document.getElementById("jg3d-css")) return;
  const s = document.createElement("style");
  s.id = "jg3d-css"; s.textContent = CSS;
  document.head.appendChild(s);
}

function setFull(v, on) {
  if (on && !v.full) {               // escape sticky/transformed ancestors: park the viewer on <body>
    v.home = document.createComment("jg3d");
    v.el.replaceWith(v.home); document.body.appendChild(v.el);
  } else if (!on && v.full && v.home) { v.home.replaceWith(v.el); v.home = null; }
  v.full = on;
  v.el.classList.toggle("jg3d-full", on);
  document.documentElement.classList.toggle("jg3d-lock", on);
  v.ctrl.enabled = on || !COARSE;
  v.ctrl.enableZoom = on;
  v.canvas.style.touchAction = v.ctrl.enabled ? "none" : "pan-y";
  v.fullBtn.textContent = on ? "✕" : "⤢";
  v.fullBtn.setAttribute("aria-label", on ? "Close full screen" : "Full screen");
  if (on) v.fullBtn.focus({ preventScroll: true });
  v.hint.textContent = on ? (COARSE ? "Drag to turn · pinch to zoom · two fingers to pan · tap a part" : "Drag to turn · scroll to zoom · right-drag to pan · click a part")
                          : (COARSE ? "Tap to open in 3D · tap an orange tag for its torque" : "Drag to turn · right-drag to pan · ⤢ for full screen · click a part");
  resize(v);
}

function pick(v, cx, cy) {
  const r = v.canvas.getBoundingClientRect(), ray = new THREE.Raycaster();
  ray.setFromCamera(new THREE.Vector2((cx - r.left) / r.width * 2 - 1, -(cy - r.top) / r.height * 2 + 1), v.cam);
  apply(v);
  const live = v.S.roots.filter(o => o.visible && v.st.get(o) !== "ghost");
  for (const h of ray.intersectObjects(live, true)) {
    if (!h.object.isMesh) continue;
    let o = h.object;
    while (o && !live.includes(o)) o = o.parent;
    if (o) return o;
  }
  return null;
}

function mount(el, models) {
  let view;
  try { view = JSON.parse(el.dataset.view); } catch (e) { view = null; }
  const model = view && models[view.model];
  if (!model) { el.innerHTML = '<div class="jg3d-msg">3D model missing from this guide.</div>'; return; }
  if (!scenes.has(view.model)) scenes.set(view.model, build(model));
  el.innerHTML = `<canvas></canvas><div class="jg3d-tags"></div>
    <div class="jg3d-note">${esc(model.note || "")}</div>
    <div class="jg3d-ui"><button type="button" data-a="tags" aria-pressed="true" aria-label="Show tags">N·m</button>
    <button type="button" data-a="reset" aria-label="Reset view">⟲</button><button type="button" data-a="full"></button></div>
    <div class="jg3d-hint"></div><div class="jg3d-info" hidden></div>`;
  const canvas = el.querySelector("canvas"), cam = new THREE.PerspectiveCamera(30, 4 / 3, 5, 9000);
  const v = { el, canvas, cam, S: scenes.get(view.model), ctx: canvas.getContext("2d"), off: new Map(), st: new Map(),
              tags: [], showTags: true, on: false, css: { w: 0, h: 0 }, px: { w: 0, h: 0 },
              tagsEl: el.querySelector(".jg3d-tags"), info: el.querySelector(".jg3d-info"), hint: el.querySelector(".jg3d-hint"),
              fullBtn: el.querySelector('[data-a="full"]') };
  v.ctrl = new OrbitControls(cam, canvas);
  Object.assign(v.ctrl, { enableDamping: true, dampingFactor: .12, rotateSpeed: .8, minDistance: 150, maxDistance: 2600, screenSpacePanning: true });
  v.ctrl.addEventListener("change", () => { v.dirty = true; kick(); });
  el._v = v; views.add(v);
  el.querySelector('[data-a="reset"]').onclick = () => setView(v, v.view);
  el.querySelector('[data-a="tags"]').onclick = e => {
    v.showTags = !v.showTags; e.currentTarget.setAttribute("aria-pressed", v.showTags); placeTags(v);
  };
  v.fullBtn.onclick = () => setFull(v, !v.full);
  el.addEventListener("keydown", e => { if (e.key === "Escape" && v.full) setFull(v, false); });
  let down = null;
  canvas.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
  canvas.addEventListener("pointerup", e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6) return;
    if (COARSE && !v.full) { setFull(v, true); return; }
    const o = pick(v, e.clientX, e.clientY);
    if (o) showInfo(v, o);
    else if (!v.info.hidden) { v.info.hidden = true; v.picked = null; v.dirty = true; kick(); }
  });
  el.jg3d = { setView: (vw, animate) => setView(v, vw, animate), full: on => setFull(v, on) };
  io.observe(el); ro.observe(el);
  setView(v, view, false);
  setFull(v, false);
}

export function mountAll(root, models) {
  const els = [...root.querySelectorAll(".jg3d[data-view]")].filter(el => !el._v);
  if (!els.length) return;
  css();
  try {
    if (!R) {
      R = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      R.setPixelRatio(1);
      R.outputColorSpace = THREE.SRGBColorSpace;
      R.toneMapping = THREE.ACESFilmicToneMapping; R.toneMappingExposure = 1.15;
      R.shadowMap.enabled = true; R.shadowMap.type = THREE.PCFSoftShadowMap;
      io = new IntersectionObserver(es => es.forEach(e => { const v = e.target._v; v.on = e.isIntersecting; if (v.on) { v.dirty = true; kick(); } }), { rootMargin: "200px" });
      ro = new ResizeObserver(es => es.forEach(e => resize(e.target._v)));
    }
  } catch (e) {
    els.forEach(el => el.innerHTML = '<div class="jg3d-msg">This browser cannot show 3D (WebGL is off). The callout table below still lists every item.</div>');
    return;
  }
  els.forEach(el => mount(el, models));
}

export function unmountAll(root) {
  for (const v of [...views]) {
    if (!root.contains(v.el) && !(v.home && root.contains(v.home))) continue;
    if (v.full) setFull(v, false);
    io.unobserve(v.el); ro.unobserve(v.el); v.ctrl.dispose(); views.delete(v);
    v.el._v = null;
  }
}
