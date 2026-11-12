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
