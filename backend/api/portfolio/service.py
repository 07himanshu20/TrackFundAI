"""
service.py
Per-organization portfolio document loader + lookup helpers for the REST layer.

Each organization has its own portfolio snapshot in PostgreSQL. The in-memory
cache is keyed by org_id so different users see only their org's data.

Public API (all functions take org_id as first argument):
  - get_document(org_id)           -> whole PortfolioDocument
  - get_node(org_id, node_id)      -> single node (any level) or None
  - get_children(org_id, node_id)  -> list of direct children (empty list if leaf)
  - get_ancestors(org_id, node_id) -> breadcrumb trail from root fund down to node
  - list_funds(org_id)             -> list of fund summary dicts
  - find_nodes(org_id, ids)        -> ordered list of nodes matching ids

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

# Per-org cache: org_id -> {"document": dict, "index": {node_id: node}, "parent_of": {node_id: parent_id}}
_org_cache: dict[str, dict] = {}

PORTFOLIO_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "portfolio.json"
)


def _index_node(node: dict, parent_id: Optional[str], index: dict, parent_of: dict):
    nid = node.get("id")
    if not nid:
        return
    index[nid] = node
    parent_of[nid] = parent_id
    for child in node.get("children", []) or []:
        _index_node(child, nid, index, parent_of)


def _load_from_db(org_id) -> Optional[dict]:
    """Load the portfolio tree for a specific organization from the database."""
    try:
        from portfolio.models import PortfolioSnapshot, PortfolioNode
    except Exception:
        return None

    # Find the active snapshot for this org
    snapshot = PortfolioSnapshot.objects.filter(
        organization_id=org_id, is_active=True,
    ).first()

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
        "Portfolio loaded from DB snapshot %s for org %s (%d nodes, %d funds)",
        snapshot.id, org_id, len(all_nodes), len(funds),
    )
    return doc


def _load_from_json() -> Optional[dict]:
    """Fallback: load from portfolio.json file."""
    if not os.path.exists(PORTFOLIO_JSON_PATH):
        return None
    with open(PORTFOLIO_JSON_PATH, "r") as f:
        return json.load(f)


def _get_org_key(org_id) -> str:
    """Normalize org_id to a string key for the cache."""
    return str(org_id) if org_id else "__no_org__"


def _load(org_id):
    key = _get_org_key(org_id)
    with _lock:
        if key in _org_cache:
            return

        # Load from DB for this org
        doc = _load_from_db(org_id)
        source = "database"

        if doc is None:
            raise FileNotFoundError(
                f"No portfolio data found for this organization. "
                "Upload fund Excel files via Data Upload to populate the portfolio dashboard."
            )

        index: dict[str, dict] = {}
        parent_of: dict[str, Optional[str]] = {}
        for fund in doc.get("funds", []):
            _index_node(fund, None, index, parent_of)

        _org_cache[key] = {
            "document": doc,
            "index": index,
            "parent_of": parent_of,
        }
        logger.info("Portfolio loaded from %s for org %s (%d nodes indexed)", source, key, len(index))


def reload(org_id=None):
    """Force a reload of portfolio data for a specific org (or all orgs)."""
    with _lock:
        if org_id is not None:
            key = _get_org_key(org_id)
            _org_cache.pop(key, None)
        else:
            _org_cache.clear()
    if org_id is not None:
        _load(org_id)


def get_document(org_id) -> dict:
    _load(org_id)
    return _org_cache[_get_org_key(org_id)]["document"]


def _get_accessible_fund_names(user) -> Optional[set[str]]:
    """Return set of fund names the user has access to, or None if no filtering needed."""
    if user is None:
        return None
    try:
        from accounts.fund_access_helpers import get_accessible_fund_ids
        from funds.models import Fund
    except ImportError:
        return None

    if user.role in ('platform_admin', 'gp_admin'):
        return None  # Admins see everything

    fund_ids = get_accessible_fund_ids(user)
    return set(Fund.objects.filter(id__in=fund_ids).values_list('name', flat=True))


def list_funds(org_id, user=None) -> list[dict]:
    """Shallow list of funds (no deep children), filtered by user's FundAccess."""
    _load(org_id)
    doc = _org_cache[_get_org_key(org_id)]["document"]
    funds = doc["funds"]

    accessible = _get_accessible_fund_names(user)
    if accessible is not None:
        funds = [f for f in funds if f.get("name") in accessible]

    return [_shallow(f) for f in funds]


def _get_fund_node_id(node_id: str) -> Optional[str]:
    """Extract the fund-level node_id prefix from any node_id.
    e.g. 'fund_x::sector_y::segment_z' -> 'fund_x'
    """
    if not node_id:
        return None
    return node_id.split("::")[0] if "::" in node_id else node_id


def _user_can_access_node(org_id, node_id: str, user) -> bool:
    """Check if the user has FundAccess to the fund that contains this node."""
    if user is None:
        return True
    accessible = _get_accessible_fund_names(user)
    if accessible is None:
        return True  # No filtering (admin)

    fund_node_id = _get_fund_node_id(node_id)
    if not fund_node_id:
        return False

    _load(org_id)
    fund_node = _org_cache[_get_org_key(org_id)]["index"].get(fund_node_id)
    if not fund_node:
        return False
    return fund_node.get("name") in accessible


def get_node(org_id, node_id: str, user=None) -> Optional[dict]:
    _load(org_id)
    if user and not _user_can_access_node(org_id, node_id, user):
        return None
    return _org_cache[_get_org_key(org_id)]["index"].get(node_id)


def get_children(org_id, node_id: str) -> list[dict]:
    node = get_node(org_id, node_id)
    if not node:
        return []
    return [_shallow(c) for c in node.get("children", []) or []]


def get_ancestors(org_id, node_id: str) -> list[dict]:
    """Breadcrumb trail — oldest ancestor first, target node last."""
    _load(org_id)
    cache = _org_cache[_get_org_key(org_id)]
    index = cache["index"]
    parent_of = cache["parent_of"]

    trail: list[dict] = []
    cur: Optional[str] = node_id
    while cur:
        node = index.get(cur)
        if not node:
            break
        trail.append(_breadcrumb(node))
        cur = parent_of.get(cur)
    trail.reverse()
    return trail


def find_nodes(org_id, ids: list[str], user=None) -> list[dict]:
    _load(org_id)
    index = _org_cache[_get_org_key(org_id)]["index"]
    out = []
    for nid in ids:
        n = index.get(nid)
        if n and (user is None or _user_can_access_node(org_id, nid, user)):
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
