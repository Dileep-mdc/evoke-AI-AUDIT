from app.parameters.common import parameter_sort_key
from app.parameters.engine import HANDLERS, load_registry


def test_sixty_parameters_and_handlers():
    specs = load_registry()
    assert len(specs) == 60
    sections = {s["section"] for s in specs}
    assert sections == {"technical", "on_page", "off_page"}
    assert sum(1 for s in specs if s["section"] == "technical") == 22
    assert sum(1 for s in specs if s["section"] == "on_page") == 20
    assert sum(1 for s in specs if s["section"] == "off_page") == 18
    missing = [s["parameter_id"] for s in specs if s["parameter_id"] not in HANDLERS]
    assert missing == []


def test_parameters_are_numbered_in_order():
    """Registry order is the order the report and the UI render in, so the file must already
    be in reading order. Compared on the numeric key, not as text: ON-5.1 belongs after
    ON-05, which plain string sorting would not give."""
    specs = load_registry()
    for section in ("technical", "on_page", "off_page"):
        ids = [s["parameter_id"] for s in specs if s["section"] == section]
        assert ids == sorted(ids, key=parameter_sort_key)
