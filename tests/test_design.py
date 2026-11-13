from types import SimpleNamespace as NS

from ptlabel import design

JOB = {"tape_mm": 12, "labels": [{"items": [{"type": "text", "text": "Cumin\nbought 2026-11", "align": "center"}]}]}


class FakeClient:
    """Plays back: render_preview with a bad spec, render_preview, submit_design."""
    def __init__(self):
        turns = [("render_preview", {"job": {"labels": [{"items": [{"type": "nope"}]}]}}),
                 ("render_preview", {"job": JOB}),
                 ("submit_design", {"job": JOB, "summary": "spice tag"})]
        self.turns, self.seen = iter(turns), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.seen.append(kw["messages"][-1])
        name, inp = next(self.turns)
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t{len(self.seen)}", name=name, input=inp)])


def test_design_loop_feeds_errors_back_and_finishes():
    c = FakeClient()
    evs = list(design.design("spice tag", None, 12, client=c))
    kinds = [e["type"] for e in evs]
    assert kinds[-1] == "done" and "preview" in kinds
    done = evs[-1]
    assert done["job"]["labels"][0]["items"][0]["type"] == "text" and done["labels"] == 1
    first_result = c.seen[1]["content"][0]
    assert first_result["is_error"] and "Invalid Job" in first_result["content"]
    img = c.seen[2]["content"][0]["content"][1]
    assert img["type"] == "image"


def test_long_previews_are_wrapped_under_the_api_image_limit():
    from PIL import Image
    w, rows = design.wrap_for_model(Image.new("RGB", (40000, 200), "white"))
    assert rows == 23 and w.width <= design.ROW_PX and max(w.size) <= 8000
    huge, _ = design.wrap_for_model(Image.new("RGB", (400000, 200), "white"))   # last resort: shrink
    assert max(huge.size) <= design.MAX_PX
    small, rows = design.wrap_for_model(Image.new("RGB", (900, 200), "white"))
    assert rows == 1 and small.size == (900, 200)


def test_design_loop_sends_wrapped_image_for_long_jobs():
    import base64, io
    from PIL import Image
    long_job = {"tape_mm": 12, "labels": [{"items": [{"type": "text", "text": f"LEFT FOLLOW {i}"}]} for i in range(40)]}

    class Long(FakeClient):
        def __init__(self):
            super().__init__()
            self.turns = iter([("render_preview", {"job": long_job}), ("submit_design", {"job": long_job, "summary": "x"})])
    c = Long()
    list(design.design("flags", None, 12, client=c))
    img = c.seen[1]["content"][0]["content"][1]["source"]["data"]
    w, h = Image.open(io.BytesIO(base64.b64decode(img))).size
    assert max(w, h) <= 8000 and w <= design.ROW_PX
    assert "wrapped" in c.seen[1]["content"][0]["content"][0]["text"]


def test_designer_can_look_up_saved_labels_templates_and_history(tmp_path):
    import json as _json
    from ptlabel.design_tools import Lookups
    from ptlabel.library import Library
    from ptlabel.templates import Template, TemplateStore
    lib = Library(tmp_path / "t.db")
    spice = lib.save("Spice jars", {"labels": [{"items": [{"type": "text", "text": "Cumin", "font": "serif-bold"}]}]})
    lib.record(job=spice["job"], ok=True, printer=None, result={"labels": 1, "length_mm": 30})
    store = TemplateStore(tmp_path / "tpl")
    store.put(Template(name="tag", variables={"id": {"example": "A1"}},
                       label={"items": [{"type": "text", "text": "{{id}}"}]}))
    filled_rows = [{"id": "A-200"}, {"id": "A-201"}]

    class Lookup(FakeClient):
        def __init__(self):
            super().__init__()
            self.turns = iter([("search_saved_labels", {"query": "cumin"}),
                               ("get_saved_label", {"id": spice["id"]}),
                               ("fill_template", {"name": "tag", "rows": filled_rows}),
                               ("get_template", {"name": "nope"}),
                               ("search_print_history", {}),
                               ("submit_design", {"job": JOB, "summary": "x"})])
    c = Lookup()
    evs = list(design.design("like my spice jars", None, 12, client=c, lookups=Lookups(lib, store)))
    assert evs[-1]["type"] == "done"
    assert any(e.get("text") == "Looking through your saved labels…" for e in evs)
    found = _json.loads(c.seen[1]["content"][0]["content"])
    assert found[0]["name"] == "Spice jars" and "Cumin" in found[0]["text"]
    opened = _json.loads(c.seen[2]["content"][0]["content"])
    assert opened["job"]["labels"][0]["items"][0]["font"] == "serif-bold"
    filled = _json.loads(c.seen[3]["content"][0]["content"])
    assert [l["items"][0]["text"] for l in filled["labels"]] == ["A-200", "A-201"]
    missing = c.seen[4]["content"][0]
    assert missing["is_error"] and "not found" in missing["content"]
    hist = _json.loads(c.seen[5]["content"][0]["content"])
    assert hist[0]["summary"] == "Cumin"


def test_lookup_tools_offered_only_when_there_is_something_to_read(tmp_path):
    from ptlabel.design_tools import Lookups
    from ptlabel.library import Library
    assert Lookups(None, None).tools == []
    names = {t["name"] for t in Lookups(Library(tmp_path / "x.db"), None).tools}
    assert "search_saved_labels" in names and "list_templates" not in names
