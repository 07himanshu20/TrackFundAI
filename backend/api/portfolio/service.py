"""
service.py
In-memory portfolio document loader + lookup helpers for the REST layer.

Phase 2 migration: Reads from PostgreSQL (portfolio.PortfolioNode) first.
Falls back to portfolio.json only if no DB snapshot exists.

The in-memory cache is identical to the old JSON-based one. The public API
is unchanged — callers see the exact same dict shapes:
  - get_document()           -> whole PortfolioDocument
  - get_node(node_id)        -> single node (any level) or None
  - get_children(node_id)    -> list of direct children (empty list if leaf)
  - get_ancestors(node_id)   -> breadcrumb trail from root fund down to node
  - list_funds()             -> list of fund summary dicts
  - find_nodes(ids)          -> ordered list of nodes matching ids (missing ones skipped)

The node_id format is hierarchical: `fund_x::sector_y::segment_z::company_w`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_document: Optional[dict] = None
_index: dict[str, dict] = {}
_parent_of: dict[str, Optional[str]] = {}

PORTFOLIO_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "portfolio.json"
)


def _index_node(node: dict, parent_id: Optional[str]):
    nid = node.get("id")
    if not nid:
        return
    _index[nid] = node
    _parent_of[nid] = parent_id
    for child in node.get("children", []) or []:
        _index_node(child, nid)


def _load_from_db() -> Optional[dict]:
    """Try to load the portfolio tree from the database.
    Returns a PortfolioDocument dict or None if no snapshot exists.
    """
    try:
        from portfolio.models import PortfolioSnapshot, PortfolioNode
    except Exception:
        return None

    snapshot = PortfolioSnapshot.objects.filter(is_active=True).first()
    if not snapshot:
        return None

    all_nodes = list(
        PortfolioNode.objects.filter(snapshot=snapshot)
        .order_by('sort_order', 'name')
        .values(
            'node_id', 'name', 'level', 'parent_node_id',
            'currency', 'native_currency', 'is_real', 'description',
            'financials',
        )
    )

    if not all_nodes:
        return None

    # Build a lookup: node_id -> node dict (with empty children list)
    node_map: dict[str, dict] = {}
    for row in all_nodes:
        node = {
            "id": row["node_id"],
            "name": row["name"],
            "level": row["level"],
            "parent_id": row["parent_node_id"],
            "currency": row["currency"],
            "is_real": row["is_real"],
            "description": row["description"],
            "financials": row["financials"] or {},
            "children": [],
        }
        if row["native_currency"]:
            node["native_currency"] = row["native_currency"]
        node_map[row["node_id"]] = node

    # Wire up parent-child relationships
    funds = []
    for nid, node in node_map.items():
        parent_nid = node["parent_id"]
        if parent_nid and parent_nid in node_map:
            node_map[parent_nid]["children"].append(node)
        elif not parent_nid:
            funds.append(node)

    doc = {
        "schema_version": snapshot.schema_version,
        "base_currency": snapshot.base_currency,
        "fx_as_of": snapshot.fx_as_of,
        "fx_rates": snapshot.fx_rates or {},
        "generated_at": snapshot.generated_at.isoformat() if snapshot.generated_at else "",
        "period_range": snapshot.period_range or {},
        "funds": funds,
    }

    logger.info(
        "Portfolio loaded from DB snapshot %s (%d nodes, %d funds)",
        snapshot.id, len(all_nodes), len(funds),
    )
    return doc


def _load_from_json() -> Optional[dict]:
    """Fallback: load from portfolio.json file."""
    if not os.path.exists(PORTFOLIO_JSON_PATH):
        return None
    with open(PORTFOLIO_JSON_PATH, "r") as f:
        return json.load(f)


def _load():
    global _document
    with _lock:
        if _document is not None:
            return

        # Try DB first, then JSON fallback
        doc = _load_from_db()
        source = "database"

        if doc is None:
            doc = _load_from_json()
            source = "JSON file"

        if doc is None:
            raise FileNotFoundError(
                f"No portfolio data found in database or at {PORTFOLIO_JSON_PATH}. "
                "Run `python manage.py import_portfolio_json` or "
                "`python -m api.portfolio.builder` to generate it."
            )

        _index.clear()
        _parent_of.clear()
        for fund in doc.get("funds", []):
            _index_node(fund, None)
        _document = doc
        logger.info("Portfolio loaded from %s (%d nodes indexed)", source, len(_index))


def reload():
    """Force a reload of portfolio data (from DB or disk)."""
    global _document
    with _lock:
        _document = None
        _index.clear()
        _parent_of.clear()
    _load()


def get_document() -> dict:
    _load()
    return _document  # type: ignore[return-value]


def list_funds() -> list[dict]:
    """Shallow list of funds (no deep children)."""
    _load()
    return [_shallow(f) for f in _document["funds"]]  # type: ignore[index]


def get_node(node_id: str) -> Optional[dict]:
    _load()
    return _index.get(node_id)


def get_children(node_id: str) -> list[dict]:
    node = get_node(node_id)
    if not node:
        return []
    return [_shallow(c) for c in node.get("children", []) or []]


def get_ancestors(node_id: str) -> list[dict]:
    """Breadcrumb trail — oldest ancestor first, target node last."""
    _load()
    trail: list[dict] = []
    cur: Optional[str] = node_id
    while cur:
        node = _index.get(cur)
        if not node:
            break
        trail.append(_breadcrumb(node))
        cur = _parent_of.get(cur)
    trail.reverse()
    return trail


def find_nodes(ids: list[str]) -> list[dict]:
    _load()
    out = []
    for nid in ids:
        n = _index.get(nid)
        if n:
            out.append(n)
    return out


def _shallow(node: dict) -> dict:
    """Return a node copy with children flattened to id+name+level+is_real summary."""
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "level": node.get("level"),
        "parent_id": node.get("parent_id"),
        "currency": node.get("currency"),
        "is_real": node.get("is_real", False),
        "description": node.get("description"),
        "financials": node.get("financials", {}),
        "child_count": len(node.get("children", []) or []),
        "children_preview": [
            {"id": c.get("id"), "name": c.get("name"), "level": c.get("level"),
             "is_real": c.get("is_real", False)}
            for c in (node.get("children", []) or [])
        ],
    }


def _breadcrumb(node: dict) -> dict:
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "level": node.get("level"),
    }
