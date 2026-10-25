/* GreaseBook client helpers — no framework, no build step. */

/* ---- mobile drawer */
function toggleDrawer(force) {
  const open = force === undefined ? !document.body.classList.contains("drawer-open")
                                   : force;
  document.body.classList.toggle("drawer-open", open);
  const b = document.querySelector(".burger");
  if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") toggleDrawer(false);
});

/* Open a <dialog>, prefill its form from the clicked button's data-attrs.
   data-action overrides the form action (new vs edit share one dialog). */
function openDialog(id, btn) {
  const dlg = document.getElementById(id);
  const form = dlg.querySelector("form");
  form.reset();
  form.querySelectorAll("input[type=hidden]").forEach(el => (el.value = ""));
  if (btn && btn.dataset) {
    for (const [k, v] of Object.entries(btn.dataset)) {
      if (k === "action") { form.action = v; continue; }
      const el = form.elements[k];
      if (!el) continue;
      if (el.type === "checkbox") el.checked = v === "1";
      else el.value = v;
    }
    // hide "new only" extras (e.g. template checkbox) when editing
    const isEdit = !!btn.dataset.id;
    form.querySelectorAll(".new-only").forEach(el => {
      el.style.display = isEdit ? "none" : "";
      if (isEdit) el.querySelectorAll("input").forEach(i => (i.checked = false));
    });
  }
  dlg.showModal();
}

/* ---- session form: parts line editor */
function wireSelect(sel) {
  sel.addEventListener("change", () => {
    const opt = sel.selectedOptions[0];
    const price = sel.closest("tr").querySelector("input[name=line_price]");
    if (opt && opt.dataset.price !== undefined && opt.dataset.price !== "")
      price.value = parseFloat(opt.dataset.price).toFixed(2);
  });
}

function addLine() {
  const tpl = document.getElementById("line-tpl");
  const row = tpl.content.cloneNode(true);
  document.querySelector("#lines tbody").appendChild(row);
  const tr = document.querySelector("#lines tbody tr:last-child");
  wireSelect(tr.querySelector("select"));
}

document.querySelectorAll("#lines select.part-select").forEach(wireSelect);

/* ---- inline creation of parts / suppliers from the session form */
async function apiCreate(ev, kind) {
  ev.preventDefault();
  const form = ev.target;
  try {
    const res = await fetch("/api/" + kind, {
      method: "POST",
      body: new FormData(form),
    });

    // The server may hand back an HTML page instead of JSON (a 500 page, or
    // a redirect to /login when the session expired). Detect that BEFORE
    // trying to parse it, so the user gets a real message instead of
    // "Unexpected token '<'".
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
      if (res.redirected && res.url.includes("/login")) {
        alert("Your session expired. Reload the page and log in again.");
      } else {
        alert("Server error " + res.status + ".\n\n" +
              "Run this on the server to see why:\n" +
              "  journalctl -u greasebook -n 30");
      }
      return false;
    }

    const j = await res.json();
    if (!res.ok) { alert(j.error || "Could not save."); return false; }

    if (kind === "parts") {
      document.querySelectorAll("select.part-select").forEach(sel => {
        const o = document.createElement("option");
        o.value = j.id;
        o.textContent = j.label;
        o.dataset.price = j.price ?? "";
        sel.appendChild(o);
      });
      const tsel = document.getElementById("line-tpl")
        .content.querySelector("select");
      const o = document.createElement("option");
      o.value = j.id; o.textContent = j.label; o.dataset.price = j.price ?? "";
      tsel.appendChild(o);
      addLine();
      const tr = document.querySelector("#lines tbody tr:last-child");
      const sel = tr.querySelector("select");
      sel.value = j.id;
      sel.dispatchEvent(new Event("change"));
    } else {
      const sel = document.getElementById("part-supplier");
      if (sel) {
        const o = document.createElement("option");
        o.value = j.id; o.textContent = j.name;
        sel.appendChild(o);
        sel.value = j.id;
      }
    }
    form.reset();
    form.closest("dialog").close();
  } catch (e) {
    alert("Could not reach the server: " + e.message);
  }
  return false;
}
