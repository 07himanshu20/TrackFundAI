"""
views.py
All REST API endpoints for the Analisa Resources MBR dashboard.
"""
import os
import logging

from django.conf import settings
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from api import data_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _require_data():
    data = data_store.get_data()
    if data is None:
        return None, Response(
            {"error": "No MIS file loaded. Please upload an Excel file."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return data, None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([AllowAny])
def summary(request):
    """GET /api/summary/ — top-level KPI snapshot."""
    data, err = _require_data()
    if err:
        return err

    s = data.get("summary", {})
    cf = data.get("cash_flow", [])
    monthly = data.get("monthly_pl", [])
    meta = data_store.get_meta()

    # Latest closing cash from cash flow
    latest_cash = None
    if cf:
        last_cf = cf[-1]
        latest_cash = last_cf.get("closing_cash")

    # YTD Revenue: compare same months in 2025 vs 2024
    months_2025 = {m["month_num"] for m in monthly if m["year"] == 2025}
    ytd_2025 = sum(m["revenue"] for m in monthly if m["year"] == 2025)
    ytd_2024 = sum(m["revenue"] for m in monthly if m["year"] == 2024 and m["month_num"] in months_2025)
    yoy_growth = round((ytd_2025 - ytd_2024) / ytd_2024 * 100, 2) if ytd_2024 else None

    return Response({
        "company": data.get("company"),
        "currency": data.get("currency"),
        "report_month": data.get("report_month"),
        "loaded_at": meta.get("loaded_at"),
        "summary_pl": s,
        "latest_closing_cash": latest_cash,
        "ytd_revenue_2025": round(ytd_2025, 2),
        "ytd_revenue_2024": round(ytd_2024, 2),
        "yoy_revenue_growth_pct": yoy_growth,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def monthly_pl(request):
    """GET /api/monthly-pl/?year=2024,2025 — monthly P&L trend."""
    data, err = _require_data()
    if err:
        return err

    years_param = request.query_params.get("year", "")
    if years_param:
        try:
            filter_years = [int(y.strip()) for y in years_param.split(",")]
        except ValueError:
            filter_years = []
    else:
        filter_years = []

    monthly = data.get("monthly_pl", [])
    if filter_years:
        monthly = [m for m in monthly if m["year"] in filter_years]

    return Response({"monthly_pl": monthly})


@api_view(["GET"])
@permission_classes([AllowAny])
def cash_flow(request):
    """GET /api/cash-flow/ — monthly cash flow statement."""
    data, err = _require_data()
    if err:
        return err
    return Response({"cash_flow": data.get("cash_flow", [])})


@api_view(["GET"])
@permission_classes([AllowAny])
def working_capital(request):
    """GET /api/working-capital/ — DSO/DIO/DPO/NWC metrics."""
    data, err = _require_data()
    if err:
        return err
    return Response({"working_capital": data.get("working_capital", {})})


@api_view(["GET"])
@permission_classes([AllowAny])
def sales_segments(request):
    """GET /api/sales-segments/ — revenue by business segment."""
    data, err = _require_data()
    if err:
        return err
    return Response({"sales_segments": data.get("sales_segments", {})})


@api_view(["GET"])
@permission_classes([AllowAny])
def full_data(request):
    """GET /api/full-data/ — complete parsed dataset (for AI context)."""
    data, err = _require_data()
    if err:
        return err
    return Response(data)


@api_view(["POST"])
@parser_classes([JSONParser])
@permission_classes([AllowAny])
def chat(request):
    """
    POST /api/chat/
    Body: { "message": str, "history": [...] }
    Calls Gemini with full portfolio context. Returns AI reply + highlights.
    """
    from api import gemini_service

    message = request.data.get("message", "").strip()
    history = request.data.get("history", [])

    if not message:
        return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

    data = data_store.get_data()
    portfolio_context = data if data else {"note": "No MIS data loaded yet."}

    try:
        result = gemini_service.chat(message, history, portfolio_context)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:
        logger.exception("Chat endpoint error")
        return Response({"error": "AI service unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(result)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([AllowAny])
def upload_mis(request):
    """
    POST /api/upload-mis/
    Accepts multipart Excel file upload, parses it, refreshes the in-memory store.
    """
    if "file" not in request.FILES:
        return Response({"error": "No file provided. Use field name 'file'."}, status=status.HTTP_400_BAD_REQUEST)

    uploaded_file = request.FILES["file"]
    filename = uploaded_file.name

    # Accept only .xlsx / .xls
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        return Response({"error": "Only .xlsx and .xls files are accepted."}, status=status.HTTP_400_BAD_REQUEST)

    # Save to media directory
    media_root = getattr(settings, "MEDIA_ROOT", "/tmp")
    os.makedirs(media_root, exist_ok=True)
    save_path = os.path.join(media_root, filename)

    with open(save_path, "wb") as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)

    # Parse and cache
    try:
        parsed_data = data_store.load_file(save_path)
    except Exception as e:
        logger.exception("Upload parse error")
        return Response({"error": f"Failed to parse file: {e}"}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    parse_report = parsed_data.get("parse_report", {})
    return Response({
        "message": "File uploaded and parsed successfully.",
        "company": parsed_data.get("company"),
        "report_month": parsed_data.get("report_month"),
        "monthly_periods": len(parsed_data.get("monthly_pl", [])),
        "cash_flow_periods": len(parsed_data.get("cash_flow", [])),
        "parse_report": parse_report,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def status_check(request):
    """GET /api/status/ — health check + data load status."""
    meta = data_store.get_meta()
    data = data_store.get_data()
    return Response({
        "status": "ok",
        "data_loaded": meta["has_data"],
        "loaded_at": meta["loaded_at"],
        "filepath": os.path.basename(meta["filepath"]) if meta["filepath"] else None,
        "report_month": data.get("report_month") if data else None,
    })
