from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NAVY = colors.HexColor("#14367A")
GREEN = colors.HexColor("#1B8A5A")
AMBER = colors.HexColor("#D97706")
RED = colors.HexColor("#C2410C")
GRAY = colors.HexColor("#64748B")


def _status_color(status: str):
    return {"PASS": GREEN, "PARTIAL": AMBER, "FAIL": RED}.get(status, GRAY)


def build_pdf(report: dict) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.55 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], textColor=NAVY, fontSize=18, spaceAfter=6)
    h = ParagraphStyle("H", parent=styles["Heading2"], textColor=NAVY, fontSize=12, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("S", parent=styles["BodyText"], fontSize=8, leading=11, textColor=GRAY)

    cats = report.get("category_scores") or {}
    counts = report.get("status_counts") or {}
    story = [
        Paragraph("AI Visibility Audit", title),
        Paragraph(f"Report for <b>{report.get('domain')}</b>", body),
        Paragraph(f"Generated: {report.get('generated_at') or '—'} · Engine {report.get('crawler_version') or '1.0.0'}", small),
        Spacer(1, 8),
        Paragraph("Scores", h),
    ]
    score_data = [[
        f"Overall\n{report.get('overall_score') if report.get('overall_score') is not None else '—'} / 100",
        f"Technical\n{cats.get('technical') if cats.get('technical') is not None else '—'} / 100",
        f"Content\n{cats.get('on_page') if cats.get('on_page') is not None else '—'} / 100",
        f"Reputation\n{cats.get('off_page') if cats.get('off_page') is not None else '—'} / 100",
    ]]
    t = Table(score_data, colWidths=[1.8 * inch] * 4)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#E8F7EF")),
        ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#FEF3C7")),
        ("BACKGROUND", (3, 0), (3, 0), colors.HexColor("#FEE2E2")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
    ]))
    story += [t, Spacer(1, 8)]
    story.append(Paragraph(
        f"Passed {counts.get('pass', 0)} · Partial {counts.get('partial', 0)} · Failed {counts.get('fail', 0)} · Unknown {counts.get('unknown', 0)}. "
        f"UNKNOWN parameters are excluded from category denominators.",
        body,
    ))
    story.append(Paragraph("Top issues", h))
    issue_rows = [["Impact", "Issue", "Category", "Score impact"]]
    for iss in (report.get("top_issues") or [])[:8]:
        issue_rows.append([
            iss.get("severity", ""),
            Paragraph(iss.get("title", ""), small),
            iss.get("category", ""),
            f"{iss.get('score_impact', 0)} pts",
        ])
    it = Table(issue_rows, colWidths=[1.2 * inch, 3.4 * inch, 1.2 * inch, 1.0 * inch])
    it.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [it, Paragraph("All 62 parameters", h)]

    rows = [["ID", "Parameter", "Status", "Score"]]
    for p in report.get("parameters") or []:
        score = "—" if p.get("score") is None else f"{p.get('score')}"
        rows.append([p.get("parameter_id"), Paragraph(p.get("name", ""), small), p.get("status"), score])
    pt = Table(rows, colWidths=[0.9 * inch, 4.4 * inch, 1.0 * inch, 0.7 * inch])
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, p in enumerate(report.get("parameters") or [], start=1):
        style_cmds.append(("TEXTCOLOR", (2, i), (2, i), _status_color(p.get("status"))))
    pt.setStyle(TableStyle(style_cmds))
    story.append(pt)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Scores are a weighted blend of Technical (35%), Content &amp; Answers (40%) and Reputation &amp; Authority (25%). "
        "Each parameter is Pass / Partial / Fail / Unknown. This PDF uses the same persisted scan results as the dashboard.",
        small,
    ))
    doc.build(story)
    return buf.getvalue()
