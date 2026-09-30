from app.parameters.common import parameter_sort_key
from app.parameters.engine import HANDLERS, load_registry
from app.parameters.formula_simple import SIMPLE_LOGIC
from app.parameters.working import WORKING


def test_each_section_is_numbered_one_two_three_with_no_gaps():
    """TECH-01..TECH-22, ON-01..ON-20, OFF-01..OFF-09: no gaps, no sub-numbers."""
    specs = load_registry()
    for section, prefix in (("technical", "TECH"), ("on_page", "ON"), ("off_page", "OFF")):
        ids = [s["parameter_id"] for s in specs if s["section"] == section]
        assert ids == [f"{prefix}-{i:02d}" for i in range(1, len(ids) + 1)], section


def test_every_parameter_has_its_handler_logic_and_working():
    ids = {s["parameter_id"] for s in load_registry()}
    assert ids == set(HANDLERS) == set(SIMPLE_LOGIC)
    assert set(WORKING) <= ids


def test_fifty_one_parameters_and_handlers():
    specs = load_registry()
    assert len(specs) == 51
    sections = {s["section"] for s in specs}
    assert sections == {"technical", "on_page", "off_page"}
    assert sum(1 for s in specs if s["section"] == "technical") == 22
    assert sum(1 for s in specs if s["section"] == "on_page") == 20
    assert sum(1 for s in specs if s["section"] == "off_page") == 9
    missing = [s["parameter_id"] for s in specs if s["parameter_id"] not in HANDLERS]
    assert missing == []


def test_parameters_are_numbered_in_order():
    """Registry order is the order the report and the UI render in, so the file must already
    be in reading order. Compared on the numeric key, not as text, so a sub-numbered id
    (ON-5.1, before the renumbering) would still sort after ON-05."""
    specs = load_registry()
    for section in ("technical", "on_page", "off_page"):
        ids = [s["parameter_id"] for s in specs if s["section"] == section]
        assert ids == sorted(ids, key=parameter_sort_key)
