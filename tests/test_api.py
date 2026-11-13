import pytest
from fastapi.testclient import TestClient

from ptlabel import api
from ptlabel.library import Library
from ptlabel.printer import NetPrinter, PrinterError
from ptlabel.templates import TemplateStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "store", TemplateStore(tmp_path))
    fake = NetPrinter("192.0.2.1")          # TEST-NET address; status always fails
    monkeypatch.setattr(fake, "_status", lambda: (_ for _ in ()).throw(PrinterError("offline in tests")))
    monkeypatch.setattr(api, "printers", {"p950nw": fake})
    monkeypatch.setattr(api, "lib", Library(tmp_path / "test.db"))
    monkeypatch.delenv("PTLABEL_TOKEN", raising=False)
    return TestClient(api.app)


LABEL = {"tape_mm": 12, "items": [{"type": "text", "text": "HI"}]}


def test_schema(client):
    s = client.get("/schema").json()
    assert {"Label", "Job", "Template"} <= set(s)


def test_render_png_and_json(client):
    r = client.post("/render", json=LABEL)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:4] == b"\x89PNG" and r.headers["x-label-tape-mm"] == "12"
    j = client.post("/render?format=json", json={"labels": [LABEL, LABEL]}).json()
    assert j["labels"] == 2 and j["preview_png_base64"]


def test_render_without_tape_falls_back_offline(client):
    r = client.post("/render?format=json", json={"items": [{"type": "text", "text": "HI"}]})
    assert r.json()["tape_mm"] == 12


def test_validation_and_render_errors(client):
    assert client.post("/render", json={"items": [{"type": "nope"}]}).status_code == 422
    assert client.post("/render", json={"tape_mm": 12, "items": [{"type": "qr", "data": "x" * 400}]}).status_code == 422


def test_print_dry_run_and_offline(client):
    r = client.post("/print?dry_run=true", json=LABEL)
    assert r.status_code == 200 and r.json()["dry_run"]
    assert client.post("/print", json=LABEL).status_code == 503


def test_template_crud(client):
    t = {"name": "tag", "variables": {"id": {"example": "A1"}},
         "label": {"items": [{"type": "qr", "data": "{{id}}"}, {"type": "text", "text": "{{id}}"}]}}
    assert client.put("/templates/tag", json=t).status_code == 200
    assert [x["name"] for x in client.get("/templates").json()] == ["tag"]
    r = client.post("/templates/tag/render?format=json",
                    json={"rows": [{"id": "A1"}, {"id": "A2"}], "job": {"tape_mm": 12}})
    assert r.json()["labels"] == 2
    assert client.post("/templates/tag/render", json={"job": {"tape_mm": 12}}).status_code == 422
    bad = dict(t, label={"items": [{"type": "text", "text": "{{x}}"}]})
    assert client.put("/templates/tag", json=bad).status_code == 422
    assert client.delete("/templates/tag").status_code == 204
    assert client.get("/templates/tag").status_code == 404


def test_token(client, monkeypatch):
    monkeypatch.setenv("PTLABEL_TOKEN", "s3cret")
    assert client.get("/schema").status_code == 401
    assert client.get("/schema", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_render_error_points_at_item_and_spans(client):
    job = {"tape_mm": 12, "labels": [LABEL, {"items": [{"type": "text", "text": "x"},
                                                       {"type": "barcode", "symbology": "ean13", "data": "SKU"}]}]}
    r = client.post("/render?format=json", json=job)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "labels", 1, "items", 1]
    ok = client.post("/render?format=json", json={"tape_mm": 12, "labels": [LABEL, LABEL]}).json()
    spans = ok["label_spans_mm"]
    assert len(spans) == 2 and spans[0][1] == spans[1][0] and abs(spans[1][1] - ok["length_mm"]) < 0.5


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ptlabel" in r.text and r.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").status_code == 200


class FakePrinter(NetPrinter):
    """Prints succeed without hardware: status ok, nothing sent."""
    def __init__(self):
        super().__init__("192.0.2.2")

    def _status(self):
        from ptlabel.printer import Status
        return Status(12, "12mm", "idle", "00", True, [])

    def _send(self, parts, wait):
        return {"sent": sum(len(p.data) for p in parts)}


def test_saved_labels_crud(client):
    r = client.post("/labels?tape_mm=12", json={"name": "Cumin", "job": {"labels": [LABEL]}, "origin": "manual"})
    assert r.status_code == 201
    lab = r.json()
    assert lab["name"] == "Cumin" and lab["labels"] == 1 and lab["job"]["labels"][0]["items"][0]["text"] == "HI"
    assert client.get(f"/labels/{lab['id']}/thumb.png").content[:4] == b"\x89PNG"
    assert [x["name"] for x in client.get("/labels?q=hi").json()] == ["Cumin"]
    assert client.get("/labels?q=nothing").json() == []
    upd = client.put(f"/labels/{lab['id']}", json={"name": "Cumin jar"}).json()
    assert upd["name"] == "Cumin jar" and upd["job"] == lab["job"]
    assert client.post("/labels", json={"name": "bad", "job": {"items": [{"type": "nope"}]}}).status_code == 422
    assert client.delete(f"/labels/{lab['id']}").status_code == 204
    assert client.get(f"/labels/{lab['id']}").status_code == 404


def test_history_records_prints_failures_and_reprints(client, monkeypatch):
    lab = client.post("/labels", json={"name": "x", "job": LABEL}).json()
    assert client.post("/print", json=LABEL).status_code == 503            # offline printer: logged as failed
    p = FakePrinter()
    monkeypatch.setattr(api, "printers", {"p950nw": p})
    ok = client.post(f"/print?label_id={lab['id']}", json=LABEL).json()
    assert ok["history_id"] and ok["labels"] == 1
    hist = client.get("/history").json()
    assert [h["ok"] for h in hist] == [True, False]
    assert hist[0]["label_id"] == lab["id"] and hist[0]["summary"] == "HI" and hist[0]["model"] == "PT-P950NW"
    assert "offline" in hist[1]["error"]
    assert client.get(f"/history/{hist[0]['id']}/thumb.png").status_code == 200
    again = client.post(f"/history/{hist[0]['id']}/reprint").json()
    entry = client.get(f"/history/{again['history_id']}").json()
    assert entry["source"] == "reprint" and entry["reprint_of"] == hist[0]["id"] and entry["job"]["labels"]
    assert client.get("/history?ok=false").json()[0]["ok"] is False
    stats = client.get("/history/stats").json()
    assert stats["prints"] == 2 and stats["saved_labels"] == 1 and stats["tape_used_mm"] > 0


def test_template_prints_record_template_and_values(client, monkeypatch):
    monkeypatch.setattr(api, "printers", {"p950nw": FakePrinter()})
    t = {"name": "tag", "variables": {"id": {"example": "A1"}},
         "label": {"items": [{"type": "text", "text": "{{id}}"}]}}
    client.put("/templates/tag", json=t)
    client.post("/templates/tag/print", json={"rows": [{"id": "A1"}, {"id": "A2"}]})
    h = client.get("/history?q=A2").json()[0]
    assert h["template"] == "tag" and h["vars"] == [{"id": "A1"}, {"id": "A2"}] and h["labels"] == 2


def test_delete_is_restorable_and_search_is_literal(client):
    a = client.post("/labels", json={"name": "50% off", "job": LABEL}).json()
    b = client.post("/labels", json={"name": "Other", "job": {"items": [{"type": "qr", "data": "https://x.test/font"}]}}).json()
    assert [x["name"] for x in client.get("/labels", params={"q": "%"}).json()] == ["50% off"]
    assert client.get("/labels", params={"q": "_"}).json() == []
    assert client.get("/labels", params={"q": "font"}).json()[0]["name"] == "Other"   # data is searchable...
    assert [x["name"] for x in client.get("/labels", params={"q": "type"}).json()] == []  # ...JSON keys aren't
    assert client.delete(f"/labels/{a['id']}").status_code == 204
    assert client.get(f"/labels/{a['id']}").status_code == 404
    assert client.post(f"/labels/{a['id']}/restore").json()["id"] == a["id"]
    assert client.post(f"/labels/{b['id']}/restore").status_code == 404                # not deleted
    assert client.get("/history/stats").json()["saved_labels"] == 2


def test_history_summary_prefers_text_over_code_data(client, monkeypatch):
    monkeypatch.setattr(api, "printers", {"p950nw": FakePrinter()})
    client.post("/print", json={"items": [{"type": "qr", "data": "https://x.test/1"}, {"type": "text", "text": "Rack B"}]})
    assert client.get("/history").json()[0]["summary"] == "Rack B"


def test_status_is_cached_but_print_asks_fresh(monkeypatch):
    calls = []
    p = FakePrinter()
    orig = p._status
    monkeypatch.setattr(p, "_status", lambda: calls.append(1) or orig())
    p.status(); p.status()
    assert len(calls) == 1
    p.print(api.as_job(LABEL))
    assert len(calls) == 2


def test_expired_status_is_served_stale_and_refreshed_in_background(monkeypatch):
    import time as _t
    calls = []
    p = FakePrinter()
    orig = p._status
    monkeypatch.setattr(p, "_status", lambda: calls.append(1) or orig())
    monkeypatch.setattr(p, "STATUS_TTL", 0.01)
    p.status(); _t.sleep(0.02)
    assert p.status().ok                              # answered from cache, refresh runs in a thread
    for _ in range(50):
        if len(calls) == 2:
            break
        _t.sleep(0.01)
    assert len(calls) == 2                            # background refresh happened
    p.status(max_age=0)
    assert len(calls) == 3                            # fresh=True waits for the printer


def test_names_are_made_unique_on_save_and_rename(client):
    a = client.post("/labels", json={"name": "Spice jars", "job": LABEL}).json()
    b = client.post("/labels", json={"name": "Spice jars", "job": LABEL}).json()
    c = client.post("/labels", json={"name": "Other", "job": LABEL}).json()
    assert b["name"] == "Spice jars (2)"
    assert client.put(f"/labels/{c['id']}", json={"name": "Spice jars"}).json()["name"] == "Spice jars (3)"
    assert client.put(f"/labels/{a['id']}", json={"name": "Spice jars"}).json()["name"] == "Spice jars"  # own name ok


def test_preview_can_collapse_copies(client):
    job = {"tape_mm": 12, "copies": 3, "labels": [LABEL, LABEL]}
    full = client.post("/render?format=json", json=job).json()
    one = client.post("/render?format=json&collapse_copies=true", json=job).json()
    assert full["labels"] == one["labels"] == 6 and full["length_mm"] == one["length_mm"]
    assert one["copies"] == 3 and one["preview_copies"] == 1 and len(one["label_spans_mm"]) == 2
    assert full["preview_copies"] == 3 and len(full["label_spans_mm"]) == 6


def test_render_reports_cuts_and_label_boxes(client):
    job = {"tape_mm": 12, "labels": [LABEL, dict(LABEL, cut_after="full_cut"), LABEL]}
    r = client.post("/render?format=json", json=job).json()
    assert r["cuts"] == ["half_cut", "full_cut"]
    assert len(r["label_boxes"]) == 3 and r["label_boxes"][2][1] > 0.5


def test_copies_print_whole_sets_and_count_pieces(client):
    a, b = dict(LABEL, items=[{"type": "text", "text": "A"}]), dict(LABEL, items=[{"type": "text", "text": "B"}], cut_after="full_cut")
    job = {"tape_mm": 12, "copies": 2, "copy_cut": "full_cut", "labels": [a, b]}
    r = client.post("/render?format=json&collapse_copies=true", json=job).json()
    assert r["labels"] == 4 and r["pieces"] == 2          # A B | A B: a full cut between the copies
    assert r["cuts"] == ["half_cut"] and len(r["label_boxes"]) == 2
    from ptlabel.spec import Job
    assert [l.items[0].text for l in Job.model_validate(job).expanded_labels()] == ["A", "B", "A", "B"]


def test_cut_between_copies_is_a_job_setting(client):
    a, b = dict(LABEL), dict(LABEL, cut_after="full_cut")      # the last label's own cut has no effect
    r = client.post("/render?format=json&collapse_copies=true", json={"tape_mm": 12, "copies": 3, "labels": [a, b]}).json()
    assert r["pieces"] == 1 and r["copy_cut"] == "half_cut"
    r = client.post("/render?format=json&collapse_copies=true",
                    json={"tape_mm": 12, "copies": 3, "copy_cut": "full_cut", "labels": [a, b]}).json()
    assert r["pieces"] == 3 and r["copy_cut"] == "full_cut" and r["preview_copies"] == 1


def test_preview_only_collapses_copies_when_every_set_cuts_the_same(client):
    job = {"tape_mm": 12, "copies": 2, "full_cut_every": 2, "labels": [LABEL, LABEL, LABEL]}
    r = client.post("/render?format=json&collapse_copies=true", json=job).json()
    assert r["preview_copies"] == 2 and len(r["label_boxes"]) == 6      # AB | CA | BC: shown in full
    job["full_cut_every"] = 3
    r = client.post("/render?format=json&collapse_copies=true", json=job).json()
    assert r["preview_copies"] == 1 and len(r["label_boxes"]) == 3      # ABC | ABC: one set stands for both
