"""目的: C3b 候補番号以外の出力・確定条件の上書きを拒否する。根拠: MEASUREMENT.md C3b。"""
import pytest
from qnapnewshub.measure_selection import selection_request, validate_selection
from qnapnewshub.measure_spans import extract_explicit


def test_candidate_selection_and_rejection():
    src = "Foo decode 60 tok/s on RTX 4070 or RTX 4090 at Q4_K_M."
    draft = extract_explicit(src, "Foo")[0]
    payload, candidates = selection_request(src, draft)
    assert "RTX 4070" in [c["text"] for c in payload["candidates"]]
    assert "hw" not in {c.key for c in draft.conditions}
    ids = [i for i, c in enumerate(candidates) if c.key == "hw"]
    chosen = validate_selection(src, draft, {"selected": [ids[0]]})
    assert any(c.key == "hw" and c.span.text == "RTX 4070" for c in chosen.conditions)
    for bad in ({"selected": ids}, {"selected": [-1]}, {"selected": [True]},
                {"selected": [99]}, {"selected": [ids[0], ids[0]]}, {"selected": ["0"]}):
        with pytest.raises(ValueError):
            validate_selection(src, draft, bad)
