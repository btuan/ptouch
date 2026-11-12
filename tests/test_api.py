import pytest
from fastapi.testclient import TestClient

from ptlabel import api
from ptlabel.printer import PrinterError
from ptlabel.templates import TemplateStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "store", TemplateStore(tmp_path))
    monkeypatch.setattr(api.printer, "status",
                        lambda: (_ for _ in ()).throw(PrinterError("offline in tests")))
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
