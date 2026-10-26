/* GreaseBook wiring viewer: zoom, tap a wire / connector / frame in a pin-to-pin
   diagram (SVG from miata-hw tools/wd_draw.py), circuit trace through wires and
   identical nets on one unit. No framework. */
(function () {
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const mEl = document.getElementById("wd-model");
  const M = mEl ? JSON.parse(mEl.textContent) : null;
  const root = document.getElementById("wd");

  /* ---- zoom: Fit / 2x / 4x of the scroll box width */
  document.querySelectorAll("[data-zoom]").forEach(b => b.addEventListener("click", () => {
    const box = b.closest(".panel").querySelector(".wd-scroll");
    const el = box && box.querySelector("svg, img");
    if (!el) return;
    const z = +b.dataset.zoom;
    el.style.width = z === 1 ? "100%" : (box.clientWidth * z) + "px";
    el.style.maxWidth = z === 1 ? "" : "none";
  }));

  if (!M) return;
  const conn = {}, unit = {}, pinWires = {}, wireHar = {};
  M.connectors.forEach(c => conn[c.id] = c);
  M.units.forEach(u => unit[u.id] = u);
  M.harnesses.forEach(h => h.wires.forEach(w => {
    wireHar[w.id] = h;
    [w.a, w.b].forEach(e => (pinWires[e] = pinWires[e] || []).push(w));
  }));
  const cOf = k => conn[k.slice(0, k.indexOf("."))];
  const pinOf = k => { const c = cOf(k); return c && c.pins.find(p => p.pin === k.slice(k.indexOf(".") + 1)); };
  const label = k => { const c = cOf(k); return c ? `${c.unit} ${c.ref}.${k.slice(k.indexOf(".") + 1)}` : k; };
  const unitUrl = (u, anchor) => root && root.dataset.unitUrl ? root.dataset.unitUrl.replace("__U__", encodeURIComponent(u)) + (anchor ? "#" + anchor : "") : "#";
  const sysUrl = s => root.dataset.sysUrl.replace("__S__", encodeURIComponent(s));
  const info = document.getElementById("wd-info");

  function trace(start) {   // same rule as miata-hw wd.js
    const seen = new Map([[start, {via: "start"}]]), q = [start];
    while (q.length) {
      const k = q.shift(), c = cOf(k), p = pinOf(k);
      for (const w of pinWires[k] || []) {
        const o = w.a === k ? w.b : w.a;
        if (!seen.has(o)) { seen.set(o, {via: "wire", w, from: k}); q.push(o); }
      }
      if (!p || p.net === "NC" || c.kind === "links") continue;
      for (const c2 of M.connectors) if (c2.unit === c.unit && c2.kind !== "links")
        for (const p2 of c2.pins) {
          const k2 = `${c2.id}.${p2.pin}`;
          if (p2.net === p.net && !seen.has(k2)) { seen.set(k2, {via: "board", from: k}); q.push(k2); }
        }
    }
    return [...seen.entries()];
  }

  function highlight(hot) {
    const svg = document.querySelector("#wd .wd-scroll svg");
    if (!svg) return;
    svg.classList.toggle("tracing", !!hot);
    svg.querySelectorAll("[data-w],[data-c]").forEach(n => n.classList.toggle("tr", !!hot && hot.has(n.dataset.w || n.dataset.c)));
  }

  function show(html) {
    if (!info) return;
    info.innerHTML = `<button type="button" class="btn mini wd-x" aria-label="Close">✕</button>${html}`;
    info.hidden = false;
    info.querySelector(".wd-x").onclick = () => { info.hidden = true; highlight(null); };
    info.scrollIntoView({block: "nearest", behavior: "smooth"});
  }

  const pinLink = k => { const c = cOf(k); return c ? `<a href="${unitUrl(c.unit, "c-" + c.id)}">${esc(label(k))}</a>` : esc(k); };

  function wireInfo(id) {
    const h = wireHar[id], w = h && h.wires.find(x => x.id === id);
    if (!w) return;
    highlight(new Set([id, cOf(w.a).id, cOf(w.b).id]));
    show(`<h3 class="mono">${esc(w.id)}</h3><table class="tbl wd-tbl"><tbody>
      <tr><td class="dim">From</td><td>${pinLink(w.a)}</td></tr><tr><td class="dim">To</td><td>${pinLink(w.b)}</td></tr>
      <tr><td class="dim">Net</td><td><button type="button" class="wd-net" data-trace="${esc(w.a)}">${esc(w.net)}</button></td></tr>
      <tr><td class="dim">Size · colour</td><td>${esc(w.size)} · ${esc(w.colour)}</td></tr>
      <tr><td class="dim">Harness</td><td><a href="${sysUrl(h.system)}#h-${esc(h.id)}">${esc(h.id)} — ${esc(h.name)}</a><br><span class="dim">${esc(h.cable)} · ${esc(h.length)}</span></td></tr>
      </tbody></table>`);
  }

  function connInfo(id) {
    const c = conn[id];
    if (!c) return;
    highlight(new Set([id]));
    const rows = c.pins.map(p => {
      const k = `${c.id}.${p.pin}`, ws = pinWires[k] || [];
      const far = ws.map(w => `<span class="mono">${esc(w.id)}</span> → ${pinLink(w.a === k ? w.b : w.a)}`).join("<br>") || '<span class="dim">—</span>';
      const net = p.net === "NC" ? '<span class="dim">NC</span>' : `<button type="button" class="wd-net" data-trace="${esc(k)}">${esc(p.net)}</button>`;
      return `<tr><td class="mono strong">${esc(p.pin)}</td><td>${net}</td><td>${far}</td></tr>`;
    }).join("");
    show(`<h3>${esc(c.unit)} ${esc(c.ref)} — ${esc(c.name)}</h3><p class="hint">Part: ${esc(c.part)} · mates with: ${esc(c.mate)}</p>
      <div class="tscroll"><table class="tbl wd-tbl"><thead><tr><th>Pin</th><th>Net</th><th>Wire → far end</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p><a class="btn mini" href="${unitUrl(c.unit, "c-" + c.id)}">Open ${esc((unit[c.unit] || {}).short || c.unit)} ›</a></p>`);
  }

  function traceInfo(start) {
    const path = trace(start);
    highlight(new Set([...path.map(([k]) => cOf(k).id), ...path.filter(([, v]) => v.w).map(([, v]) => v.w.id)]));
    show(`<h3>Circuit trace from ${esc(label(start))}</h3><p class="hint">Every pin joined to the start pin through wires or the same net on one unit. Highlighted in the diagram.</p>
      <div class="tscroll"><table class="tbl wd-tbl"><thead><tr><th>#</th><th>Pin</th><th>Net</th><th>Reached via</th></tr></thead><tbody>${path.map(([k, v], i) =>
      `<tr><td class="mono">${i + 1}</td><td>${pinLink(k)}</td><td>${esc((pinOf(k) || {}).net || "")}</td><td class="dim">${v.via === "start" ? "start" : v.via === "wire" ? "wire " + esc(v.w.id) : "same net on " + esc(cOf(k).unit)}</td></tr>`).join("")}</tbody></table></div>`);
  }

  document.addEventListener("click", e => {
    const t = e.target.closest("[data-trace]");
    if (t) { e.preventDefault(); if (info) traceInfo(t.dataset.trace); else location.hash = "p-" + t.dataset.trace; return; }
    if (!root || !root.dataset.unitUrl || !e.target.closest(".wd-scroll svg")) return;
    const w = e.target.closest("[data-w]"), c = e.target.closest("[data-c]"), f = e.target.closest("[data-u]");
    if (w) wireInfo(w.dataset.w);
    else if (c) connInfo(c.dataset.c);
    else if (f) location.href = unitUrl(f.dataset.u);
  });
})();
