"""Freeze the parameter logic and render the parameter workbook.

Run from the repository root:  python tools/build_parameter_workbook.py

Three things happen, in this order, and the first failure stops the rest:

1. VALIDATE. The prose spec in spec_technical/spec_onpage/spec_offpage is checked against
   the actual handler source: every one of the 62 parameters must be described exactly
   once, and each entry's `llm` flag must agree with whether that handler really calls
   judge(). This is what keeps the workbook honest - the document cannot claim a parameter
   uses an LLM once the code stops doing so, or vice versa, without this failing.

2. FREEZE. The validated spec is written to backend/app/parameters/frozen_spec.json, and
   registry.json's `ai_required` / `automation_level` fields are rewritten to match the
   code. Those two registry fields had drifted badly: 16 parameters claimed "Yes" while
   calling no model at all.

3. RENDER. docs/Parameter-Logic-Frozen.xlsx is written with the three requested columns -
   Parameter, how the metric is calculated, and whether an LLM is used and why.
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

from spec_offpage import OFFPAGE  # noqa: E402
from spec_onpage import ONPAGE  # noqa: E402
from spec_technical import TECHNICAL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "backend" / "app" / "parameters"
REGISTRY_PATH = PARAMS / "registry.json"
FROZEN_PATH = PARAMS / "frozen_spec.json"
FORMULAS_PATH = PARAMS / "formulas.json"
WORKBOOK_PATH = ROOT / "docs" / "Parameter-Logic-Frozen.xlsx"

FREEZE_VERSION = "1.0.0"
SPEC = {**TECHNICAL, **ONPAGE, **OFFPAGE}
SECTION_SHEET = {"technical": "Technical", "on_page": "On-Page", "off_page": "Off-Page"}


# --- 1. validate -----------------------------------------------------------------------

def handlers_calling_the_model() -> set[str]:
    """Parameter IDs whose handler actually contains a judge() call, read from source."""
    using = set()
    for module in ("technical", "onpage", "offpage"):
        source = (PARAMS / f"{module}.py").read_text(encoding="utf-8")
        for match in re.finditer(r"\nasync def (\w+)\(spec, ctx\):(.*?)(?=\nasync def |\nHANDLERS)", source, re.S):
            if "judge(" in match.group(2):
                using.add(match.group(1).upper().replace("_", "-"))
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

    if problems:
        print("FROZEN SPEC VALIDATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)

    print(f"Validated {len(ids)} parameters against handler source. "
          f"{len(actual)} use an LLM: {', '.join(sorted(actual))}")
    return actual


# --- 2. freeze -------------------------------------------------------------------------

def freeze(registry: list[dict], llm_ids: set[str]) -> list[dict]:
    frozen = {
        "freeze_version": FREEZE_VERSION,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Frozen parameter logic. Each entry describes what backend/app/parameters/*.py "
            "actually does. tools/build_parameter_workbook.py re-validates every llm flag "
            "against the handler source, so this file cannot silently drift from the code."
        ),
        "parameters": {
            p["parameter_id"]: {
                "name": p["name"],
                "section": p["section"],
                "weight": p["weight"],
                "pass_threshold": p["pass_threshold"],
                "partial_threshold": p["partial_threshold"],
                "uses_llm": SPEC[p["parameter_id"]]["llm"],
                "metric": SPEC[p["parameter_id"]]["metric"],
                "llm_rationale": SPEC[p["parameter_id"]]["why"],
            }
            for p in registry
        },
    }
    FROZEN_PATH.write_text(json.dumps(frozen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {FROZEN_PATH.relative_to(ROOT)}")

    changed = 0
    for p in registry:
        uses = p["parameter_id"] in llm_ids
        want_ai = "Yes" if uses else "No"
        want_level = "LLM-assisted (heuristic fallback)" if uses else "Deterministic (rule-based)"
        if p.get("ai_required") != want_ai or p.get("automation_level") != want_level:
            changed += 1
        p["ai_required"] = want_ai
        p["automation_level"] = want_level
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Aligned registry.json ai_required/automation_level with the code ({changed} row(s) corrected)")

    # formulas.json feeds the "Scoring Formula" column of every per-scan metrics workbook
    # (scan_output.py). It covered only 41 of the 62 parameters, so 21 rows silently fell
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
MONO = Font(name="Consolas", size=9)
TOP_WRAP = Alignment(wrap_text=True, vertical="top", indent=1)

COLUMNS = [
    ("Parameter", 34),
    ("How we are calculating the metric", 82),
    ("LLM used? Why / why not", 74),
]


def _sheet(wb: Workbook, title: str, rows: list[dict]) -> None:
    """One pillar sheet: a header row, then one row per parameter. No banner, no fills."""
    ws = wb.create_sheet(title)
    for col, (name, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left", indent=1)
        cell.border = HEADER_BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"

    for offset, row in enumerate(rows):
        r = 2 + offset
        entry = SPEC[row["parameter_id"]]
        values = [
            f"{row['parameter_id']}\n{row['name']}",
            entry["metric"],
            ("LLM: YES\n\n" if entry["llm"] else "LLM: NO\n\n") + entry["why"],
        ]
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = TOP_WRAP
            cell.border = CELL_BORDER
            cell.font = MONO if c == 2 else BODY_FONT
        ws.cell(row=r, column=1).font = Font(bold=True, size=10)
        ws.row_dimensions[r].height = max(70, min(320, 13 * max(
            entry["metric"].count("\n") + len(entry["metric"]) // 80,
            entry["why"].count("\n") + len(entry["why"]) // 72,
        )))
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
    title = ws.cell(row=1, column=1, value="AI Visibility Audit - Parameter Logic (Frozen)")
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
    line("Purpose", "The frozen, authoritative description of how each of the 62 parameters is "
                    "calculated, and whether a language model is involved in that calculation.")
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
