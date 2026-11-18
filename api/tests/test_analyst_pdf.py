"""Tests for the client-side chart PNGs embedded into the AI Analyst PDF."""

import base64

from services import analyst_pdf

# 1x1 transparent PNG.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _payload(chart_images=None) -> dict:
    return {
        "analysis_title": "Chart embed",
        "analysis_goal": "Embed captured charts",
        "table_name": "orders",
        "narrative_summary": "Narrative.",
        "kpis": [{"id": "k1", "title": "Revenue", "metric_column": "rev"}],
        "steps": [
            {
                "id": "s1",
                "title": "Revenue by month",
                "question": "How did revenue move?",
                "chart_type": "line",
                "x_column": "month",
                "y_column": "rev",
                "status": "ok",
                "insight": "Revenue grew.",
            }
        ],
        "data": {
            "k1": {"columns": ["rev"], "rows": [[1234.5]]},
            "s1": {"columns": ["month", "rev"], "rows": [["Jan", 10], ["Feb", 12]]},
        },
        "chart_images": chart_images or {},
    }


def test_chart_png_is_embedded_in_pdf():
    plain = analyst_pdf.render_analysis_pdf(_payload())
    with_chart = analyst_pdf.render_analysis_pdf(
        _payload({"s1": "data:image/png;base64," + PNG_B64})
    )
    assert plain.startswith(b"%PDF")
    assert len(with_chart) > len(plain)


def test_malformed_or_oversized_chart_data_is_ignored():
    unsafe = base64.b64encode(b"not a png").decode()
    pdf = analyst_pdf.render_analysis_pdf(
        _payload({"s1": f"data:image/png;base64,{unsafe}"})
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) < len(
        analyst_pdf.render_analysis_pdf(
            _payload({"s1": "data:image/png;base64," + PNG_B64})
        )
    )
