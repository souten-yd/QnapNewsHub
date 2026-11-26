"""目的: C3c 曖昧な条件、候補外ID、偽の測定を拒否。MEASUREMENT.md C3b。"""
from dataclasses import replace
import pytest
from qnapnewshub.measure_selection import selection_request, validate_selection
from qnapnewshub.measure_spans import Candidate, Span, extract_explicit


def test_candidate_selection_and_rejection():
    src = "Foo decode 60 tok/s on RTX 4070 at Q4_K_M."
    original = extract_explicit(src, "Foo")[0]
    draft = replace(original, conditions=())
    payload, candidates = selection_request(src, draft)
    assert "RTX 4070" in [c["text"] for c in payload["candidates"]]
    ids = [i for i, c in enumerate(candidates) if c.key == "hw"]
    assert len(ids) == 1
    chosen = validate_selection(src, draft, {"selected": ids})
    assert any(c.key == "hw" and c.span.text == "RTX 4070" for c in chosen.conditions)
    for bad in ({"selected": [-1]}, {"selected": [True]},
                {"selected": [99]}, {"selected": [ids[0], ids[0]]}, {"selected": ["0"]}):
        with pytest.raises(ValueError):
            validate_selection(src, draft, bad)


def test_no_arbitrary_selection_from_ambiguous_conditions():
    assert not extract_explicit("Foo decode 60 tok/s on RTX 4070 or RTX 4090.", "Foo")
    src = "Foo decode 60 tok/s on RTX 4070 and RTX 4090 at Q4_K_M."
    draft = extract_explicit(src, "Foo")[0]
    assert draft.conditions == ()
    assert selection_request(src, draft)[1] == ()
    with pytest.raises(ValueError):
        validate_selection(src, draft, {"selected": [0]})
    forged = replace(draft, conditions=(Candidate("hw", Span(src.index("RTX 4070"), src.index("RTX 4070")+8, "RTX 4070")),))
    with pytest.raises(ValueError):
        selection_request(src, forged)
