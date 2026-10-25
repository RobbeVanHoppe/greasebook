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

for page in ["/vehicle/1", "/vehicle/1/sessions", "/vehicle/1/sessions/new",
             "/vehicle/1/fuel", "/catalog", "/vehicle/1/export"]:
    assert c.get(page).status_code == 200, page
print("all pages render OK")
print("SMOKE TEST PASS")
