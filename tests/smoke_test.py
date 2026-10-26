#!/usr/bin/env python3
"""End-to-end smoke test: run with  python3 tests/smoke_test.py
Uses a throwaway data folder, never touches your real ledger."""
import io
import os
import sys
import tempfile

os.environ["GREASEBOOK_DATA"] = tempfile.mkdtemp(prefix="greasebook-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app, item_status, q  # noqa: E402

c = app.test_client()


def ok(r):
    assert r.status_code in (200, 302), (r.status_code, r.get_data(as_text=True)[:200])
    return r


ok(c.post("/vehicle/save", data={"name": "Test car", "vtype": "Car",
                                  "odometer_km": "100000", "add_templates": "1"}))
sup = c.post("/api/suppliers", data={"name": "Parts Ltd"}).get_json()
part = c.post("/api/parts", data={"name": "Oil filter", "part_number": "OF-1",
                                   "supplier_id": sup["id"], "unit_price": "8.5"}).get_json()
with app.app_context():
    item = q("SELECT * FROM maint_items WHERE name LIKE 'Engine oil%'")[0]

# session covering an item resets its life bar (derived baseline)
ok(c.post("/vehicle/1/sessions/save", data={
    "date": "2026-07-10", "odometer_km": "105000", "title": "Oil service",
    "labor_cost": "20", "line_part": [str(part["id"])], "line_qty": ["1"],
    "line_price": ["8.50"], "items": [str(item["id"])],
    "attachments": (io.BytesIO(b"invoice"), "invoice.pdf")},
    content_type="multipart/form-data"))
with app.app_context():
    assert item_status(item, 105000)["last_km"] == 105000
print("derived baseline OK")

# phantom ids must give a clean JSON error, not an HTML 500
r = c.post("/api/parts", data={"name": "Ghost", "supplier_id": "999"})
assert r.status_code == 400 and "application/json" in r.content_type
print("foreign-key guard OK")

# filters, exports
assert b"Oil service" in ok(c.get("/vehicle/1/sessions?q=OF-1")).data
assert b"Oil service" not in ok(c.get("/vehicle/1/sessions?q=nothing-matches")).data
pdf = ok(c.get("/vehicle/1/export/report.pdf")).data
assert pdf.startswith(b"%PDF") and b"WinAnsiEncoding" in pdf
assert b"OF-1" in ok(c.get("/vehicle/1/export/sessions.csv")).data
print("filters + exports OK")

# deleting the session rolls the life bar back
with app.app_context():
    sid = q("SELECT id FROM sessions")[0]["id"]
ok(c.post(f"/sessions/{sid}/delete"))
with app.app_context():
    assert item_status(q("SELECT * FROM maint_items WHERE id=?", (item["id"],))[0],
                       105000)["last_km"] == 100000
print("recalculation after delete OK")

# job guides: import a bundle, run it, finish it into a session
import json  # noqa: E402
bundle = {"format": "jg-bundle/1", "to": "TEST-JG-01", "title": "TEST JOB", "short": "Test job",
          "change": 0, "chapter": "00", "section": "00", "date": "today", "time": "1 h",
          "applicability": "all", "sources": "none",
          "input": {"required": [["cold", "touch"]], "personnel": "1", "equipment": [["spanner", "10 mm", "[SP]"]],
                    "supplies": [["gasket", "P/N", "1"]], "safety": [["warning", "hot"]]},
          "figures": {"f1": {"title": "Fig", "callouts": [["1", "bolt"]],
                             "svg": '<svg onload="alert(1)"><script>alert(2)</script><circle class="k" r="5"/></svg>'}},
          "records": [["R-01.1", "Torque used"]],
          "paras": [{"n": "9.1", "title": "Do it", "fig": "f1",
                     "steps": [{"n": "1", "text": "Undo bolt", "record": "R-01.1"}, {"n": "2", "text": "Refit"}]}],
          "followon": ["none"], "trace": [["step 1", "[SP]"]]}
ok(c.post("/jobs/import", data={"bundle": (io.BytesIO(json.dumps(bundle).encode()), "t.jgb.json")},
          content_type="multipart/form-data"))
r = ok(c.post("/jobs/import", data={"bundle": (io.BytesIO(b"not json"), "x.json")},
              content_type="multipart/form-data"))
with app.app_context():
    g_ = q("SELECT * FROM job_guides")[0]
    assert "<script" not in g_["data"] and "onload" not in g_["data"], "svg not sanitised"
ok(c.post("/vehicle/1/jobs/start", data={"guide_id": str(g_["id"])}))
assert b"Undo bolt" in ok(c.get("/runs/1")).data
assert c.post("/api/runs/1/step", json={"step": "1", "done": True}).get_json()["done"] == 1
assert c.post("/api/runs/1/step", json={"step": "99", "done": True}).status_code == 400
assert c.post("/api/runs/1/record", json={"field": "R-01.1", "value": "7 Nm"}).status_code == 200
assert c.post("/api/runs/1/record", json={"field": "R-99", "value": "x"}).status_code == 400
ok(c.post("/runs/1/finish", data={"odometer_km": "106000"}))
with app.app_context():
    run = q("SELECT * FROM job_runs WHERE id=1")[0]
    sess = q("SELECT * FROM sessions WHERE id=?", (run["session_id"],))[0]
    assert run["finished"] and sess["title"] == "Test job" and "7 Nm" in sess["notes"]
    assert q("SELECT odometer_km FROM vehicles WHERE id=1")[0]["odometer_km"] == 106000
print("job guides OK")

# 3D figures: a view must name a model in the bundle; the run page loads the viewer
model = {"format": "jg-model/1", "parts": [{"id": "p", "type": "box", "size": [1, 1, 1]}],
         "bolts": [{"id": "b", "spec": "10 N·m", "at": [[0, 0, 0]]}]}
b3d = dict(bundle, to="TEST-JG-3D", models={"m": model},
           figures={"f1": {"title": "3D", "callouts": [], "view": {"model": "m", "torque": ["b"]}}})
bad = dict(b3d, figures={"f1": {"title": "3D", "callouts": [], "view": {"model": "nope"}}})
for b, want in ((bad, 0), (b3d, 1)):
    ok(c.post("/jobs/import", data={"bundle": (io.BytesIO(json.dumps(b).encode()), "t.jgb.json")},
              content_type="multipart/form-data"))
    with app.app_context():
        assert len(q("SELECT id FROM job_guides WHERE to_number='TEST-JG-3D'")) == want
with app.app_context():
    gid = q("SELECT id FROM job_guides WHERE to_number='TEST-JG-3D'")[0]["id"]
r3 = ok(c.post("/vehicle/1/jobs/start", data={"guide_id": str(gid)}))
page = ok(c.get(r3.headers["Location"])).get_data(as_text=True)
assert 'class="jg3d"' in page and "jg3d.js" in page and '"jg-model/1"' in page
assert c.get("/static/vendor/three/three.module.min.js").status_code == 200
print("3D job guide figures OK")

# wiring: gzip bundle with a diagram, a board and a module; svg sanitised; pages render
import gzip  # noqa: E402
wd = {"format": "wd-bundle/1", "set": "TEST", "title": "Test car", "change": 2, "date": "today",
      "model": {"units": [{"id": "B", "name": "Board", "zone": "Z1", "system": "S1", "board": "b"},
                          {"id": "M", "name": "Module", "zone": "Z1", "system": "S1"}],
                "connectors": [{"id": "B-J1", "unit": "B", "ref": "J1", "name": "Out", "kind": "connector", "part": "hdr",
                                "mate": "hdr", "pins": [{"pin": "1", "net": "SIG"}]},
                               {"id": "M-H", "unit": "M", "ref": "H", "name": "Pins", "kind": "connector", "part": "hdr",
                                "mate": "hdr", "pins": [{"pin": "1", "net": "SIG"}]}],
                "harnesses": [{"id": "H1", "name": "Lead", "system": "S1", "cable": "c", "length": "1 m",
                               "wires": [{"id": "H1-01", "a": "B-J1.1", "b": "M-H.1", "net": "SIG", "size": "0.5", "colour": "RD"}]}],
                "systems": [{"id": "S1", "to": "WD-1", "title": "Sys", "units": ["B", "M"]}]},
      "figs": {"WD-1": '<svg onload="alert(1)"><g data-w="H1-01"><path d="M0,0 L9,0"/></g><script>x</script></svg>'},
      "oem": {"model": {"format": "oem-model/1", "source": {"title": "Manual", "code": "X", "section": "Z", "edition": "test"},
                        "harness_codes": {"F": "Front"}, "anchors": {"dash": [0, 0, 500]},
                        "connectors": [{"id": "C-01", "code": "C-01", "name": "Cluster", "harness": "F", "faces": {"F": [["B/Y", "*"]]},
                                        "pins": [{"pin": "B/Y", "colour": "B/Y"}], "loc": {"anchor": "dash", "text": "dash"}, "systems": ["C"]}],
                        "systems": [{"id": "C", "title": "Clusters", "z": ["Z-40"], "pdf": [849], "conns": ["C-01"],
                                     "nets": [{"name": "IG", "colour": "B/Y", "segs": [["GND①", "C-01.B/Y", "B/Y", "F"]]}], "fig": "fig/C.svg"}],
                        "grounds": []},
              "figs": {"C": '<svg><g data-c="C-01"/><script>z</script></svg>'}},
      "boards": {"B": {"status": "ok", "title": "Board", "rev": "A",
                       "sheets": [{"title": "Root", "svg": "<svg><rect/><script>y</script></svg>"}],
                       "layout": [{"title": "Perf", "img": "javascript:alert(1)"}], "pcb": [],
                       "bom": [["Ref", "Value"], ["R1", "10k"]]}}}
ok(c.post("/wiring/import", data={"bundle": (io.BytesIO(gzip.compress(json.dumps(wd).encode())), "w.wdb.json.gz")},
          content_type="multipart/form-data"))
with app.app_context():
    ws = q("SELECT * FROM wiring_sets")[0]
    bd = q("SELECT data FROM wiring_boards")[0]["data"]
    assert "<script" not in ws["data"] and "onload" not in ws["data"] and "<script" not in bd
    assert "javascript:" not in bd, "only png data URIs may pass"
sid = ws["id"]
assert b"H1-01" in ok(c.get(f"/wiring/{sid}/wd/S1")).data
assert b"Root" in ok(c.get(f"/wiring/{sid}/unit/B")).data
assert b"10k" in ok(c.get(f"/wiring/{sid}/unit/B?tab=bom")).data
assert b"wdcard" in ok(c.get(f"/wiring/{sid}/unit/M")).data
assert c.get(f"/wiring/{sid}/unit/NOPE").status_code == 404
assert b"Cluster" in ok(c.get(f"/wiring/{sid}/oem")).data
page = ok(c.get(f"/wiring/{sid}/oem/sys/C")).get_data(as_text=True)
assert "data-c" in page and "<script>z" not in page
assert b"oemface" in ok(c.get(f"/wiring/{sid}/oem/c/C-01")).data
assert c.get(f"/wiring/{sid}/oem/c/NOPE").status_code == 404
print("wiring OK")

for page in ["/vehicle/1", "/vehicle/1/sessions", "/vehicle/1/sessions/new",
             "/vehicle/1/fuel", "/catalog", "/vehicle/1/export",
             "/jobs", "/vehicle/1/jobs", "/runs/1", "/wiring"]:
    assert c.get(page).status_code == 200, page
print("all pages render OK")
print("SMOKE TEST PASS")
