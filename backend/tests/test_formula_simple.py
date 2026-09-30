from app.parameters.engine import load_registry
from app.parameters.formula_simple import SIMPLE_LOGIC


def test_every_parameter_has_plain_words_logic():
    assert {p["parameter_id"] for p in load_registry()} == set(SIMPLE_LOGIC)


def test_the_logic_is_plain_words_for_any_site():
    for pid, (checks, scoring) in SIMPLE_LOGIC.items():
        assert checks.strip() and scoring.strip(), pid
        text = (checks + scoring).lower()
        for banned in ("evoke", "nvent", "×", "÷", "=", "→"):
            assert banned not in text, (pid, banned)
