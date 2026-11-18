# services/analyst_pdf.py
"""
Server-side PDF rendering for an AI Analyst report.

The client sends back the already-computed analysis (narrative + step result
rows); this module only lays it out. No AI call and no database access happen
here, so exporting is fast and cannot fail because of an LLM outage.
"""

import base64
import binascii
import logging
import struct
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from services.analyst import _fmt_number

logger = logging.getLogger("my_global_app_logger")

MAX_PDF_ROWS = 12
MAX_PDF_COLUMNS = 8
MAX_CHART_BYTES = 2 * 1024 * 1024

INK = colors.HexColor("#1F1D1A")
MUTED = colors.HexColor("#6B655D")
ACCENT = colors.HexColor("#6E4FC0")
RULE = colors.HexColor("#DCD6CC")
SOFT = colors.HexColor("#F5F2EC")

CHART_WIDTH = 168 * mm
CHART_MAX_HEIGHT = 95 * mm


def _text(value) -> str:
    raw = "" if value is None else str(value)
    return escape(raw.encode("latin-1", "replace").decode("latin-1"))


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "AnalystTitle", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=21, leading=25, textColor=INK, alignment=0, spaceAfter=4,
        ),
        "goal": ParagraphStyle(
            "AnalystGoal", parent=base["Normal"], fontName="Helvetica",
            fontSize=10.5, leading=14, textColor=ACCENT, spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "AnalystMeta", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=11, textColor=MUTED,
        ),
        "h2": ParagraphStyle(
            "AnalystH2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=13, leading=16, textColor=INK, spaceBefore=14, spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "AnalystH3", parent=base["Heading3"], fontName="Helvetica-Bold",
            fontSize=11, leading=14, textColor=INK, spaceBefore=4, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "AnalystBody", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13.5, textColor=INK,
        ),
        "question": ParagraphStyle(
            "AnalystQuestion", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=9, leading=12, textColor=MUTED, spaceAfter=4,
        ),
        "takeaway": ParagraphStyle(
            "AnalystTakeaway", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13, textColor=INK, spaceBefore=4,
        ),
        "cell": ParagraphStyle(
            "AnalystCell", parent=base["Normal"], fontName="Helvetica",
            fontSize=7.8, leading=10, textColor=INK,
        ),
        "head": ParagraphStyle(
            "AnalystHead", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=7.8, leading=10, textColor=colors.white,
        ),
        "note": ParagraphStyle(
            "AnalystNote", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=8.5, leading=11, textColor=MUTED,
        ),
    }


def _kpi_value(kpi: dict, data: dict):
    payload = data.get(kpi.get("id")) or {}
    rows = payload.get("rows") or []
    if not rows or not rows[0]:
        return "n/a"
    raw = rows[0][0]
    if isinstance(raw, (int, float)):
        value = _fmt_number(raw)
    else:
        try:
            value = _fmt_number(float(raw))
        except (TypeError, ValueError):
            value = str(raw)
    return f"{kpi.get('prefix') or ''}{value}{kpi.get('suffix') or ''}"


def _result_table(columns: list, rows: list, style) -> Table:
    columns = list(columns)[:MAX_PDF_COLUMNS]
    rows = list(rows)[:MAX_PDF_ROWS]
    header = [Paragraph(_text(c), style["head"]) for c in columns]
    body = []
    for row in rows:
        body.append([
            Paragraph(_text(row[i] if i < len(row) else ""), style["cell"])
            for i in range(len(columns))
        ])
    table = Table([header] + body, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _decode_png(data_url) -> tuple[bytes, int, int] | None:
    """Validate a chart data URL and pull its PNG bytes plus pixel size."""
    if not isinstance(data_url, str) or not data_url.strip():
        return None
    payload = data_url.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    # PNG signature + IHDR chunk (width/height at bytes 16..24).
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 33:
        return None
    if len(raw) > MAX_CHART_BYTES:
        return None
    width, height = struct.unpack(">II", raw[16:24])
    if width <= 0 or height <= 0:
        return None
    return raw, width, height


def _chart_flowable(step: dict, chart_images: dict):
    """Embed the PNG the client captured for this step, if it sent one.

    A missing or malformed image never blocks the PDF; the step still renders
    its result table.
    """
    decoded = _decode_png(chart_images.get(step.get("id")))
    if decoded is None:
        return None
    raw, width, height = decoded

    draw_width = CHART_WIDTH
    draw_height = draw_width * height / width
    if draw_height > CHART_MAX_HEIGHT:
        draw_height = CHART_MAX_HEIGHT
        draw_width = draw_height * width / height
    try:
        return Image(BytesIO(raw), width=draw_width, height=draw_height)
    except Exception:
        logger.warning(
            "Skipping chart image in PDF for step %s", step.get("id"), exc_info=True
        )
        return None


def render_analysis_pdf(payload: dict) -> bytes:
    style = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=_text(payload.get("analysis_title") or "AI Analyst report"),
        author="Amoeba AI Analyst",
    )

    story = []
    story.append(Paragraph(_text(payload.get("analysis_title") or "AI Analyst report"), style["title"]))
    if payload.get("analysis_goal"):
        story.append(Paragraph(_text(payload["analysis_goal"]), style["goal"]))
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    story.append(Paragraph(
        f"Generated by Amoeba AI Analyst · {generated} · {_text(payload.get('table_name') or '')}",
        style["meta"],
    ))

    narrative = str(payload.get("narrative_summary") or "").strip()
    story.append(Paragraph("Executive summary", style["h2"]))
    if narrative:
        for paragraph in [p.strip() for p in narrative.split("\n") if p.strip()]:
            story.append(Paragraph(_text(paragraph), style["body"]))
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("No narrative summary was produced for this analysis.", style["note"]))

    kpis = payload.get("kpis") or []
    data = payload.get("data") or {}
    if kpis:
        story.append(Paragraph("Key metrics", style["h2"]))
        rows = [[
            Paragraph("Metric", style["head"]),
            Paragraph("Value", style["head"]),
        ]]
        for kpi in kpis:
            rows.append([
                Paragraph(_text(kpi.get("title") or kpi.get("metric_column") or "Metric"), style["cell"]),
                Paragraph(_text(_kpi_value(kpi, data)), style["cell"]),
            ])
        table = Table(rows, colWidths=[110 * mm, 64 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)

    steps = payload.get("steps") or []
    if steps:
        story.append(Paragraph("Analysis steps", style["h2"]))
        for index, step in enumerate(steps, 1):
            head = [Paragraph(f"Step {index} · {_text(step.get('title') or '')}", style["h3"])]
            if step.get("question"):
                head.append(Paragraph(_text(step["question"]), style["question"]))

            if step.get("status") == "ok":
                if step.get("insight"):
                    head.append(Paragraph(
                        f"<b>Takeaway.</b> {_text(step['insight'])}", style["takeaway"]
                    ))
                story.append(KeepTogether(head))

                result = data.get(step.get("id")) or {}
                chart = _chart_flowable(step, payload.get("chart_images") or {})
                if chart is not None:
                    story.append(Spacer(1, 2))
                    story.append(chart)

                columns = result.get("columns") or []
                rows = result.get("rows") or []
                if columns and rows:
                    story.append(Spacer(1, 3))
                    story.append(_result_table(columns, rows, style))
            else:
                head.append(Paragraph(
                    "This step couldn't be computed from the available data.",
                    style["note"],
                ))
                story.append(KeepTogether(head))
            story.append(Spacer(1, 8))

    failed = [s for s in steps if s.get("status") != "ok"]
    if failed:
        story.append(Paragraph("Data notes", style["h2"]))
        story.append(Paragraph(
            _text(
                f"{len(failed)} of {len(steps)} analysis steps couldn't be calculated. "
                "The summary above only covers the steps that returned data."
            ),
            style["note"],
        ))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, "Amoeba · AI Analyst")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc_.page}")
        canvas.setStrokeColor(RULE)
        canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
