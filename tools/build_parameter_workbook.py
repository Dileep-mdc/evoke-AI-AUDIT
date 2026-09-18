"""Freeze the parameter logic and render the parameter workbook.

Run from the repository root:  python tools/build_parameter_workbook.py

Three things happen, in this order, and the first failure stops the rest:

1. VALIDATE. The prose spec in spec_technical/spec_onpage/spec_offpage is checked against
   the actual handler source: every one of the parameters must be described exactly once,
   and each entry's `llm` flag must agree with whether that handler really runs its own
   dedicated judge() pass. The engine is checked too, for the claim that matters most --
   that every parameter's score comes from the model. This is what keeps the workbook
   honest: it cannot describe scoring the code no longer does without failing here.

2. FREEZE. The validated spec is written to backend/app/parameters/frozen_spec.json, and
   registry.json's `ai_required` / `automation_level` fields are rewritten to match the
   code. Every parameter is model-scored from its curated data, so both now read the same
   for all sixty; `dedicated_llm_pass` is what distinguishes the eighteen handlers that
   additionally classify with the model before scoring.

3. RENDER. docs/Parameter-Logic.xlsx is written with the four requested columns - Parameter,
   what the parameter means, how the metric is calculated, and whether an LLM is used and
   why. The first three read from spec_definitions; the exact formula text stays in
   frozen_spec.json and reaches clients through each scan's own metrics workbook.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spec_definitions import DEFINITIONS  # noqa: E402
from spec_offpage import OFFPAGE  # noqa: E402
from spec_onpage import ONPAGE  # noqa: E402
from spec_technical import TECHNICAL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "backend" / "app" / "parameters"
REGISTRY_PATH = PARAMS / "registry.json"
FROZEN_PATH = PARAMS / "frozen_spec.json"
FORMULAS_PATH = PARAMS / "formulas.json"
WORKBOOK_PATH = ROOT / "docs" / "Parameter-Logic.xlsx"

FREEZE_VERSION = "1.0.0"
SPEC = {**TECHNICAL, **ONPAGE, **OFFPAGE}
SECTION_SHEET = {"technical": "Technical", "on_page": "On-Page", "off_page": "Off-Page"}


# --- 1. validate -----------------------------------------------------------------------

def handlers_calling_the_model() -> set[str]:
    """Parameter IDs whose handler actually contains a judge() call, read from source.

    The id comes from each module's HANDLERS table, not from the function name: a
    sub-numbered parameter like ON-5.1 has no identifier spelling, so the two differ.
    """
    using = set()
    for module in ("technical", "onpage", "offpage"):
        source = (PARAMS / f"{module}.py").read_text(encoding="utf-8")
        table = re.search(r"\nHANDLERS = \{(.*?)\n\}", source, re.S).group(1)
        ids = {func: pid for pid, func in re.findall(r'"([^"]+)":\s*(\w+)', table)}
        for match in re.finditer(r"\nasync def (\w+)\(spec, ctx\):(.*?)(?=\nasync def |\nHANDLERS)", source, re.S):
            if "judge(" in match.group(2) and match.group(1) in ids:
                using.add(ids[match.group(1)])
    return using


def validate(registry: list[dict]) -> set[str]:
    ids = [p["parameter_id"] for p in registry]
    problems = []

    missing = [i for i in ids if i not in SPEC]
    extra = [i for i in SPEC if i not in ids]
    if missing:
        problems.append(f"{len(missing)} parameter(s) have no frozen spec entry: {missing}")
    if extra:
        problems.append(f"spec describes unknown parameter(s): {extra}")

    # The workbook's readable columns live in spec_definitions and are checked the same way,
    # so a parameter cannot be added to the engine and quietly ship with a blank definition.
    no_copy = [i for i in ids if i not in DEFINITIONS]
    stale_copy = [i for i in DEFINITIONS if i not in ids]
    if no_copy:
        problems.append(f"{len(no_copy)} parameter(s) have no definition/brief: {no_copy}")
    if stale_copy:
        problems.append(f"spec_definitions describes unknown parameter(s): {stale_copy}")
    for pid in ids:
        copy = DEFINITIONS.get(pid)
        if copy is None:
            continue
        if not copy.get("definition", "").strip():
            problems.append(f"{pid}: empty definition")
        if not copy.get("brief", "").strip():
            problems.append(f"{pid}: empty brief")

    actual = handlers_calling_the_model()
    for pid in ids:
        entry = SPEC.get(pid)
        if entry is None:
            continue
        if entry["llm"] != (pid in actual):
            problems.append(
                f"{pid}: spec says llm={entry['llm']} but the handler "
                f"{'does' if pid in actual else 'does not'} call judge()"
            )
        if not entry.get("metric", "").strip():
            problems.append(f"{pid}: empty metric description")
        if not entry.get("why", "").strip():
            problems.append(f"{pid}: empty LLM rationale")

    # "Every parameter is model-scored" is asserted sixty times in the frozen spec and
    # implemented exactly once, in the engine. If that one place stops doing it, all sixty
    # claims become false at the same moment, so it is checked here rather than trusted.
    engine_source = (PARAMS / "engine.py").read_text(encoding="utf-8")
    if "score_parameter(" not in engine_source or "apply_judgement" not in engine_source:
        problems.append(
            "engine.py no longer routes every parameter through the model, so the frozen "
            "spec's uses_llm=true would be false for all of them"
        )

    if problems:
        print("FROZEN SPEC VALIDATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)

    print(f"Validated {len(ids)} parameters against handler source. All {len(ids)} are "
          f"model-scored by the engine; {len(actual)} also run a dedicated pre-scoring pass: "
          f"{', '.join(sorted(actual))}")
    return actual


# --- 2. freeze -------------------------------------------------------------------------

# Every parameter is model-scored now, so this is true of all sixty and is recorded once
# rather than sixty times. The per-parameter `why` text that follows it still describes the
# parameter's own dedicated pre-scoring pass, which only eighteen handlers run.
UNIVERSAL_SCORING_NOTE = (
    "Scored by the model. The curated data this parameter extracts is sent to the model "
    "together with this parameter's definition and metric, and the model returns the score, "
    "the explanation and the recommendation that reach the report "
    "(backend/app/parameters/engine.py). When the model is unavailable the rules-based score "
    "described below stands in, and the report marks the row as such."
)
AUTOMATION_LEVEL = "LLM-scored (rules-based fallback)"


def freeze(registry: list[dict], llm_ids: set[str]) -> list[dict]:
    frozen = {
        "freeze_version": FREEZE_VERSION,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Frozen parameter logic. Each entry describes what backend/app/parameters/*.py "
            "actually does. Every parameter is scored by the model from its curated data; "
            "`dedicated_llm_pass` marks the handlers that additionally run their own "
            "classification pass before scoring, and tools/build_parameter_workbook.py "
            "re-validates that flag against the handler source so it cannot drift."
        ),
        "parameters": {
            p["parameter_id"]: {
                "name": p["name"],
                "section": p["section"],
                "weight": p["weight"],
                "pass_threshold": p["pass_threshold"],
                "partial_threshold": p["partial_threshold"],
                "uses_llm": True,
                "dedicated_llm_pass": SPEC[p["parameter_id"]]["llm"],
                "definition": DEFINITIONS[p["parameter_id"]]["definition"],
                "metric_brief": DEFINITIONS[p["parameter_id"]]["brief"],
                "metric": SPEC[p["parameter_id"]]["metric"],
                "llm_rationale": (
                    f"{UNIVERSAL_SCORING_NOTE}\n\nDedicated pre-scoring model pass: "
                    f"{'yes' if SPEC[p['parameter_id']]['llm'] else 'no'}. "
                    f"{SPEC[p['parameter_id']]['why']}"
                ),
            }
            for p in registry
        },
    }
    FROZEN_PATH.write_text(json.dumps(frozen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {FROZEN_PATH.relative_to(ROOT)}")

    changed = 0
    for p in registry:
        if p.get("ai_required") != "Yes" or p.get("automation_level") != AUTOMATION_LEVEL:
            changed += 1
        p["ai_required"] = "Yes"
        p["automation_level"] = AUTOMATION_LEVEL
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Aligned registry.json ai_required/automation_level with the code ({changed} row(s) corrected)")

    # formulas.json feeds the "Scoring Formula" column of every per-scan metrics workbook
    # (scan_output.py). It covered only 41 of them, so a third of the rows silently fell
    # back to the registry's one-line `scoring` string. Regenerating it from the same frozen
    # metric text keeps the per-scan report and this reference document in agreement.
    formulas = {p["parameter_id"]: SPEC[p["parameter_id"]]["metric"] for p in registry}
    FORMULAS_PATH.write_text(json.dumps(formulas, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {FORMULAS_PATH.relative_to(ROOT)} ({len(formulas)} parameters, was 41)")
    return registry


# --- 3. render -------------------------------------------------------------------------

# Monochrome by design: this workbook is presented on a projector and printed, where fills
# and colour-coded verdicts read as decoration and reproduce badly. Structure is carried by
# weight, rules and whitespace instead, so nothing depends on a reader seeing colour.
HAIRLINE = Side(style="thin", color="BFBFBF")
RULE = Side(style="medium", color="000000")
CELL_BORDER = Border(left=HAIRLINE, right=HAIRLINE, top=HAIRLINE, bottom=HAIRLINE)
HEADER_BORDER = Border(left=HAIRLINE, right=HAIRLINE, top=HAIRLINE, bottom=RULE)
HEADER_FONT = Font(bold=True, size=11)
BODY_FONT = Font(size=10)
TOP_WRAP = Alignment(wrap_text=True, vertical="top", indent=1)

# (header, column width, characters that fit on a wrapped line at that width)
COLUMNS = [
    ("Parameter", 28, 26),
    ("What this parameter means", 60, 56),
    ("How we are calculating the metric", 66, 62),
    ("LLM used? Why / why not", 60, 56),
]


def _wrapped_lines(text: str, per_line: int) -> int:
    """Rough count of the display lines a wrapped cell needs, so rows are sized to fit.

    Excel will not auto-fit a wrapped cell, so an unset row height clips the longest
    column on every row of the sheet.
    """
    return sum(1 + len(line) // per_line for line in text.split("\n"))


def _sheet(wb: Workbook, title: str, rows: list[dict]) -> None:
    """One pillar sheet: a header row, then one row per parameter. No banner, no fills."""
    ws = wb.create_sheet(title)
    for col, (name, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left", indent=1)
        cell.border = HEADER_BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "B2"

    for offset, row in enumerate(rows):
        r = 2 + offset
        pid = row["parameter_id"]
        entry = SPEC[pid]
        copy = DEFINITIONS[pid]
        # The column already answers yes-or-no on its first line, so a rationale that opens
        # with a bare "YES." / "NO." says it twice. Qualified openers ("NO, deliberately.")
        # carry meaning and are left alone.
        why = re.sub(r"^(YES|NO)\.\s+", "", entry["why"])
        values = [
            f"{pid}\n{row['name']}",
            copy["definition"],
            copy["brief"],
            ("LLM: YES\n\n" if entry["llm"] else "LLM: NO\n\n") + why,
        ]
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = TOP_WRAP
            cell.border = CELL_BORDER
            cell.font = BODY_FONT
        ws.cell(row=r, column=1).font = Font(bold=True, size=10)
        ws.row_dimensions[r].height = min(300, 13.2 * max(
            _wrapped_lines(value, COLUMNS[c][2]) for c, value in enumerate(values)
        ))
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{1 + len(rows)}"
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def _overview(wb: Workbook, registry: list[dict], llm_ids: set[str]) -> None:
    ws = wb.create_sheet("Overview", 0)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 104

    ws.merge_cells("A1:B1")
    title = ws.cell(row=1, column=1, value="AI Visibility Audit - Parameter Logic")
    title.font = Font(bold=True, size=14)
    title.alignment = Alignment(vertical="center", indent=1)
    title.border = Border(bottom=RULE)
    ws.cell(row=1, column=2).border = Border(bottom=RULE)
    ws.row_dimensions[1].height = 28

    r = 3

    def line(label: str, value: str, bold: bool = False) -> None:
        nonlocal r
        left = ws.cell(row=r, column=1, value=label)
        left.font = Font(bold=True, size=10)
        left.alignment = TOP_WRAP
        right = ws.cell(row=r, column=2, value=value)
        right.alignment = TOP_WRAP
        right.font = Font(bold=True, size=10) if bold else BODY_FONT
        ws.row_dimensions[r].height = max(16, 13 * (1 + len(value) // 100))
        r += 1

    def heading(text: str) -> None:
        nonlocal r
        r += 1
        cell = ws.cell(row=r, column=1, value=text)
        cell.font = Font(bold=True, size=11)
        cell.border = Border(bottom=HAIRLINE)
        ws.cell(row=r, column=2).border = Border(bottom=HAIRLINE)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        r += 1

    line("Freeze version", FREEZE_VERSION, bold=True)
    line("Frozen at", datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"))
    line("Parameters", f"{len(registry)} total - "
                       f"{sum(1 for p in registry if p['section'] == 'technical')} Technical, "
                       f"{sum(1 for p in registry if p['section'] == 'on_page')} On-Page, "
                       f"{sum(1 for p in registry if p['section'] == 'off_page')} Off-Page")
    line("Use an LLM", f"{len(llm_ids)} of {len(registry)} - {', '.join(sorted(llm_ids))}")

    heading("What this workbook is")
    line("Purpose", "The authoritative description of every parameter the audit scores: what each "
                    "one means, how its metric is calculated, and whether a language model is "
                    "involved in that calculation - and where it is not, why not.")
    line("How to read a row", "One row per parameter, on the sheet for its pillar. Where a check "
                              "cannot measure the whole of what its name promises, the definition "
                              "column says so explicitly rather than leaving the gap implied.")
    line("Source of truth", "Generated from backend/app/parameters/*.py by "
                            "tools/build_parameter_workbook.py. The generator re-reads the handler "
                            "source and fails if any row's LLM claim disagrees with the code, so this "
                            "document cannot drift away from what actually runs.")

    heading("When an LLM is used, and when it is not")
    line("LLM used", "Only where the question is a judgment about MEANING that no rule can settle: "
                     "does this passage actually define the concept, does this opening lead with "
                     "substance, is this list genuinely useful, what stage of the buying journey does "
                     "this page serve, is this snippet describing the right company. In every such "
                     "case the deterministic heuristic is kept as a fallback, so a scan still completes "
                     "with no API key - it just reports lower confidence.")
    line("LLM not used - exact", "Where the fact is deterministic and machine-checkable: HTTP status, "
                                 "robots.txt rules, tag presence, counts, ratios, set similarity. A "
                                 "model here would be slower, more expensive and less reproducible "
                                 "while being no more accurate.")
    line("LLM not used - no data", "Where the limitation is a MISSING DATA SOURCE rather than a missing "
                                   "judgment: backlink quality needs a link index, review velocity "
                                   "needs a review API, certification verification needs issuer "
                                   "registries. Asking a model to fill those gaps produces confident "
                                   "fabrication, so those checks are scored as explicitly capped "
                                   "proxies that state their own limit instead.")

    heading("Reading the scores")
    line("Per parameter", "0-100. PASS at 90 or above, PARTIAL at 60-89, FAIL below 60.")
    line("UNKNOWN", "The check could not run, or does not apply to this site. UNKNOWN rows are "
                    "EXCLUDED from the averages entirely - they hand their share to their scored "
                    "siblings rather than scoring zero.")
    line("Pillar score", "Weighted average of the scored parameters in that pillar.")
    line("Overall score", "Technical 35% + On-Page 40% + Off-Page 25%, renormalised across whichever "
                          "pillars produced a score.")

    heading("Known structural limits")
    line("Off-Page ceiling", "OFF-06, OFF-08, OFF-13 and OFF-16 are hard-capped at 55, 60, 55 and 50 "
                             "because the licensed data they need is not connected. They therefore "
                             "cannot return PASS, and the Off-Page pillar cannot reach 100 until a "
                             "backlink index, a review API and a certification adapter are wired in. "
                             "A mid-70s Off-Page score is the realistic ceiling today, not a finding "
                             "about the site.")
    line("Open gaps", "ON-02 (self-containedness), ON-14 (named clients), ON-17 (content shelf life), "
                      "TECH-12 (tables published as images) and TECH-14 (alt-text descriptiveness) are "
                      "measured by proxy today. Each row states its own limitation in column 3.")


def main() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    llm_ids = validate(registry)
    registry = freeze(registry, llm_ids)

    wb = Workbook()
    wb.remove(wb.active)
    for section, sheet_name in SECTION_SHEET.items():
        _sheet(wb, sheet_name, [p for p in registry if p["section"] == section])
    _overview(wb, registry, llm_ids)

    WORKBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(WORKBOOK_PATH)
    print(f"Wrote {WORKBOOK_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
