from app.parameters.engine import HANDLERS, load_registry


def test_sixty_two_parameters_and_handlers():
    specs = load_registry()
    assert len(specs) == 62
    sections = {s["section"] for s in specs}
    assert sections == {"technical", "on_page", "off_page"}
    assert sum(1 for s in specs if s["section"] == "technical") == 22
    assert sum(1 for s in specs if s["section"] == "on_page") == 22
    assert sum(1 for s in specs if s["section"] == "off_page") == 18
    missing = [s["parameter_id"] for s in specs if s["parameter_id"] not in HANDLERS]
    assert missing == []
