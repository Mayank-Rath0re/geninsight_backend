# services/analyst_pdf.py
"""
Server-side PDF rendering for an AI Analyst report.

The client sends back the already-computed analysis (narrative + step result
rows); this module only lays it out. No AI call and no database access happen
here, so exporting is fast and cannot fail because of an LLM outage.
"""

import logging
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.lineplots import ScatterPlot
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.platypus import (
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
MAX_CHART_POINTS = 10

INK = colors.HexColor("#1F1D1A")
MUTED = colors.HexColor("#6B655D")
ACCENT = colors.HexColor("#6E4FC0")
RULE = colors.HexColor("#DCD6CC")
SOFT = colors.HexColor("#F5F2EC")

CHART_WIDTH = 168 * mm
CHART_HEIGHT = 64 * mm
CHART_FILLS = [
    colors.HexColor("#6E4FC0"),
    colors.HexColor("#9C86D6"),
    colors.HexColor("#C4B6E8"),
    colors.HexColor("#8A7FB5"),
    colors.HexColor("#B0A6C9"),
    colors.HexColor("#5A4396"),
    colors.HexColor("#D9D2EA"),
    colors.HexColor("#7B6FA8"),
]


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


def _gtext(value) -> str:
    raw = "" if value is None else str(value)
    return raw.encode("latin-1", "replace").decode("latin-1")


def _axis_label(value) -> str:
    return _gtext(_fmt_number(value))[:12]


def _to_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _chart_series(step: dict, result: dict):
    """Pull (labels, values, raw_x) out of a step's result rows."""
    columns = [str(c) for c in (result.get("columns") or [])]
    rows = result.get("rows") or []
    if not columns or not rows:
        return [], [], []

    lower = {c.lower(): i for i, c in enumerate(columns)}
    x_idx = lower.get(str(step.get("x_column") or "").lower(), 0)
    y_idx = lower.get(str(step.get("y_column") or "").lower(), len(columns) - 1)
    if x_idx == y_idx and len(columns) > 1:
        y_idx = 0 if x_idx != 0 else 1

    labels, values, raw_x = [], [], []
    for row in rows[:MAX_CHART_POINTS]:
        if not isinstance(row, (list, tuple)):
            continue
        cell_x = row[x_idx] if x_idx < len(row) else ""
        number = _to_float(row[y_idx] if y_idx < len(row) else None)
        if number is None:
            continue
        labels.append(_gtext(cell_x)[:18])
        values.append(number)
        raw_x.append(_to_float(cell_x))
    return labels, values, raw_x


def _style_axis(axis, size=6.5) -> None:
    axis.labels.fontName = "Helvetica"
    axis.labels.fontSize = size
    axis.strokeColor = RULE
    axis.gridStrokeColor = RULE
    axis.gridStrokeWidth = 0.3
    axis.visibleGrid = 1
    axis.visibleTicks = 0


def _bar_chart(drawing, labels, values, horizontal: bool) -> None:
    chart = HorizontalBarChart() if horizontal else VerticalBarChart()
    if horizontal:
        chart.x, chart.y, chart.width, chart.height = 96, 12, 356, 148
    else:
        chart.x, chart.y, chart.width, chart.height = 34, 20, 424, 136
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    _style_axis(chart.categoryAxis)
    _style_axis(chart.valueAxis)
    chart.valueAxis.labelTextFormat = _axis_label
    chart.valueAxis.visibleGrid = 1
    chart.categoryAxis.strokeColor = RULE
    chart.bars[0].fillColor = ACCENT
    chart.bars[0].strokeColor = None
    drawing.add(chart)


def _line_chart(drawing, labels, values) -> None:
    chart = HorizontalLineChart()
    chart.x, chart.y, chart.width, chart.height = 34, 24, 424, 128
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    _style_axis(chart.categoryAxis)
    _style_axis(chart.valueAxis)
    chart.valueAxis.labelTextFormat = _axis_label
    chart.lines[0].strokeColor = ACCENT
    chart.lines[0].strokeWidth = 1.6
    chart.lines[0].fillColor = None
    drawing.add(chart)


def _scatter_chart(drawing, raw_x, values) -> None:
    points = []
    for index, value in enumerate(values):
        x_value = raw_x[index] if index < len(raw_x) else None
        points.append((float(index + 1) if x_value is None else x_value, value))
    chart = ScatterPlot()
    chart.x, chart.y, chart.width, chart.height = 40, 24, 410, 128
    chart.data = [points]
    _style_axis(chart.xValueAxis)
    _style_axis(chart.yValueAxis)
    chart.yValueAxis.labelTextFormat = _axis_label
    chart.xValueAxis.labelTextFormat = _axis_label
    chart.lines[0].strokeWidth = 0
    chart.lines[0].symbol = makeMarker("FilledCircle")
    chart.lines[0].symbol.fillColor = ACCENT
    chart.lines[0].symbol.strokeColor = ACCENT
    drawing.add(chart)


def _pie_chart(drawing, labels, values) -> bool:
    pairs = [(label, value) for label, value in zip(labels, values) if value > 0]
    if len(pairs) < 2:
        return False
    pairs = pairs[:8]
    chart = Pie()
    chart.x, chart.y, chart.width, chart.height = 142, 6, 192, 156
    chart.data = [value for _, value in pairs]
    chart.labels = [label for label, _ in pairs]
    chart.slices.fontName = "Helvetica"
    chart.slices.fontSize = 6.5
    chart.slices.strokeColor = colors.white
    chart.slices.strokeWidth = 0.6
    for i in range(len(pairs)):
        chart.slices[i].fillColor = CHART_FILLS[i % len(CHART_FILLS)]
        chart.slices[i].labelRadius = 1.04
    chart.sideLabels = 1
    drawing.add(chart)
    return True


def _chart_flowable(step: dict, result: dict):
    """Best-effort chart for a step. A rendering failure never blocks the PDF."""
    try:
        chart_type = str(step.get("chart_type") or "bar").lower()
        labels, values, raw_x = _chart_series(step, result)
        if len(values) < 2:
            return None
        drawing = Drawing(CHART_WIDTH, CHART_HEIGHT)
        if chart_type == "pie":
            if not _pie_chart(drawing, labels, values):
                return None
        elif chart_type == "line":
            _line_chart(drawing, labels, values)
        elif chart_type == "scatter":
            _scatter_chart(drawing, raw_x, values)
        elif chart_type == "column":
            _bar_chart(drawing, labels, values, horizontal=False)
        else:
            _bar_chart(drawing, labels, values, horizontal=True)
        return drawing
    except Exception:
        logger.warning("Skipping chart in PDF for step %s", step.get("id"), exc_info=True)
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
                chart = _chart_flowable(step, result)
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
