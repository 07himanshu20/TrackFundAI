"""
portfolio_views.py
REST endpoints for the hierarchical portfolio dashboard.

All endpoints are org-scoped: each user sees only their organization's
portfolio data. Authentication is required via JWT (IsGPUser permission).

Endpoints:
  GET  /api/portfolio/                        -> top-level (funds + meta)
  GET  /api/portfolio/node/<node_id>/         -> single node + immediate children
  GET  /api/portfolio/ancestors/<node_id>/    -> breadcrumb trail
  GET  /api/portfolio/compare/                -> ?ids=a,b,c&mode=X&metric=Y
  POST /api/portfolio/chat/                   -> hierarchy-scoped Gemini chat
  POST /api/portfolio/reload/                 -> force-reload portfolio data
"""

import json
import logging

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework import status

from accounts.permissions import IsGPUser
from api.portfolio import service as portfolio_service
from api.portfolio import compare as compare_module

logger = logging.getLogger(__name__)


def _get_org_id(request):
    """Extract org id from the authenticated request."""
    org = request.organization
    if not org:
        return None
    return org.id


# ---------------------------------------------------------------------------
# 1. Portfolio root — list of funds + portfolio meta
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsGPUser])
def portfolio_root(request):
    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    try:
        doc = portfolio_service.get_document(org_id)
    except FileNotFoundError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    funds = portfolio_service.list_funds(org_id, user=request.user)

    if not funds:
        return Response(
            {"error": "No portfolio data found for this organization. "
             "Upload fund Excel files via Data Upload to populate the portfolio dashboard."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({
        "schema_version": doc.get("schema_version"),
        "base_currency": doc.get("base_currency"),
        "fx_as_of": doc.get("fx_as_of"),
        "fx_rates": doc.get("fx_rates"),
        "generated_at": doc.get("generated_at"),
        "period_range": doc.get("period_range"),
        "funds": funds,
    })


# ---------------------------------------------------------------------------
# 2. Single node lookup
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsGPUser])
def portfolio_node(request, node_id: str):
    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    node = portfolio_service.get_node(org_id, node_id, user=request.user)
    if not node:
        return Response({"error": f"node not found: {node_id}"},
                        status=status.HTTP_404_NOT_FOUND)

    children = portfolio_service.get_children(org_id, node_id)
    ancestors = portfolio_service.get_ancestors(org_id, node_id)

    return Response({
        "id": node.get("id"),
        "name": node.get("name"),
        "level": node.get("level"),
        "parent_id": node.get("parent_id"),
        "currency": node.get("currency"),
        "is_real": node.get("is_real", False),
        "description": node.get("description"),
        "financials": node.get("financials", {}),
        "children": children,
        "ancestors": ancestors,
    })


# ---------------------------------------------------------------------------
# 3. Breadcrumb ancestors
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsGPUser])
def portfolio_ancestors(request, node_id: str):
    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    trail = portfolio_service.get_ancestors(org_id, node_id)
    if not trail:
        return Response({"error": "node not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"ancestors": trail})


# ---------------------------------------------------------------------------
# 4. Comparison endpoint
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsGPUser])
def portfolio_compare(request):
    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    ids_param = request.query_params.get("ids", "").strip()
    mode = request.query_params.get("mode", "actual").strip()
    metric = request.query_params.get("metric", "revenue").strip()
    as_of = (request.query_params.get("as_of") or "").strip() or None
    range_from = (request.query_params.get("range_from") or "").strip() or None
    range_to = (request.query_params.get("range_to") or "").strip() or None

    if not ids_param:
        return Response({"error": "query param `ids` is required (comma-separated)"},
                        status=status.HTTP_400_BAD_REQUEST)

    ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    nodes = portfolio_service.find_nodes(org_id, ids, user=request.user)

    if len(nodes) < 1:
        return Response({"error": "no nodes matched the provided ids", "ids": ids},
                        status=status.HTTP_404_NOT_FOUND)

    payload = compare_module.build_comparison(
        nodes,
        mode=mode,
        metric=metric,
        as_of=as_of,
        range_from=range_from,
        range_to=range_to,
    )
    payload["requested_ids"] = ids
    payload["missing_ids"] = [i for i in ids if not portfolio_service.get_node(org_id, i)]
    return Response(payload)


# ---------------------------------------------------------------------------
# 5. Hierarchy-scoped chatbot
# ---------------------------------------------------------------------------

@api_view(["POST"])
@parser_classes([JSONParser])
@permission_classes([IsGPUser])
def portfolio_chat(request):
    """
    POST body:
      {
        "message": str,
        "history": [{role, content}, ...],
        "scope_id": str        # optional — node id to scope the chat to
      }
    """
    from api import gemini_service

    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    message = (request.data.get("message") or "").strip()
    history = request.data.get("history") or []
    scope_id = request.data.get("scope_id") or None

    if not message:
        return Response({"error": "message is required"},
                        status=status.HTTP_400_BAD_REQUEST)

    # Build scoped context
    try:
        doc = portfolio_service.get_document(org_id)
    except FileNotFoundError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if scope_id:
        node = portfolio_service.get_node(org_id, scope_id, user=request.user)
        if not node:
            return Response({"error": f"scope_id not found: {scope_id}"},
                            status=status.HTTP_404_NOT_FOUND)
        context = {
            "scope": {
                "id": node.get("id"),
                "name": node.get("name"),
                "level": node.get("level"),
                "currency": node.get("currency"),
                "is_real": node.get("is_real", False),
                "ancestors": portfolio_service.get_ancestors(org_id, scope_id)[:-1],
            },
            "node": node,
            "base_currency": doc.get("base_currency"),
            "fx_as_of": doc.get("fx_as_of"),
        }
    else:
        context = {
            "scope": {"level": "portfolio", "name": "Full portfolio"},
            "base_currency": doc.get("base_currency"),
            "fx_as_of": doc.get("fx_as_of"),
            "fx_rates": doc.get("fx_rates"),
            "period_range": doc.get("period_range"),
            "funds_overview": [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "is_real": f.get("is_real", False),
                    "summary": (f.get("financials", {}) or {}).get("summary", {}),
                    "sector_count": f.get("child_count", 0),
                }
                for f in portfolio_service.list_funds(org_id, user=request.user)
            ],
        }

    try:
        result = _scoped_gemini_chat(message, history, context, gemini_service)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception:
        logger.exception("Portfolio chat error")
        return Response({"error": "AI service unavailable"},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(result)


def _scoped_gemini_chat(message: str, history: list, context: dict, gemini_service) -> dict:
    """
    Wrap gemini_service with a hierarchy-aware system prompt that describes
    the currently selected scope + the available data.
    """
    import google.generativeai as genai

    # Ensure configured (reuses the same flag inside gemini_service)
    gemini_service._ensure_configured()

    scope = context.get("scope", {})
    level = scope.get("level", "portfolio")
    name = scope.get("name", "Portfolio")

    system_instruction = f"""
You are a CFO-grade financial analyst AI for a multi-fund VC portfolio dashboard.
The user is currently viewing the **{level.upper()}** level: **{name}**.

All monetary figures in the data below are in USD (converted via FX rates as of
{context.get('fx_as_of')}), unless a nested company's native currency is noted.

Your job is to help the user understand this scope's financials: budget vs actual,
margin analysis, variance drivers, trends, and comparisons across children
(if any). When the user asks about something outside the current scope, tell them
which part of the hierarchy to drill into instead of fabricating data.

CURRENT SCOPE CONTEXT (JSON):
{json.dumps(context, indent=2, default=str)[:60000]}

═══════════════════════════════════════════════════════════════════
INLINE CHART DIRECTIVES
═══════════════════════════════════════════════════════════════════
When a chart would materially clarify the answer, emit a fenced block:

```chart
{{"type":"bar|line|doughnut|pie","title":"...","labels":[...],
  "datasets":[{{"label":"...","data":[...]}}],"yFormat":"USD|percent|days|number"}}
```

Rules:
- Raw numbers only (no commas, no currency prefix).
- Max 2 chart blocks per answer; ≤15 labels per chart.
- Always write the text explanation BEFORE the chart block.

Guidelines:
- Be concise and analytical. Lead with numbers.
- Format currency as USD X,XXX,XXX or USD X.XM.
- Use bold for key figures and bullet lists for comparisons.
- If the user asks for something outside this scope (e.g. a sibling fund), tell
  them clearly and suggest navigating to that entity instead.
"""

    model_name = "gemini-2.5-flash"
    try:
        from django.conf import settings
        model_name = getattr(settings, "GEMINI_MODEL", model_name)
    except Exception:
        pass

    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system_instruction,
    )

    gemini_history = []
    for turn in history:
        role = turn.get("role", "user")
        text = turn.get("content", "")
        gemini_history.append({"role": role, "parts": [text]})

    chat_session = model.start_chat(history=gemini_history)
    response = chat_session.send_message(message)
    reply_text = response.text

    return {
        "reply": reply_text,
        "scope": scope,
    }


# ---------------------------------------------------------------------------
# 6. Force reload
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsGPUser])
def portfolio_reload(request):
    """Force-reload portfolio data for the current user's organization."""
    org_id = _get_org_id(request)
    if not org_id:
        return Response({"error": "No organization."}, status=status.HTTP_403_FORBIDDEN)

    try:
        portfolio_service.reload(org_id)
    except FileNotFoundError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    doc = portfolio_service.get_document(org_id)
    return Response({
        "ok": True,
        "fund_count": len(doc.get("funds", [])),
        "generated_at": doc.get("generated_at"),
    })
