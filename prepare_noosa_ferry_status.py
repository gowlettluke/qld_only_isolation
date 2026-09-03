#!/usr/bin/env python3
"""Add Noosa North Shore Ferry live status and Cooloola 4WD conditional access.

This runs after prepare_daintree_ferry_status.py. It reads that prepared QLD
Traffic GeoJSON, checks the dedicated Noosa North Shore Ferries status page,
inspects the released road graph for a usable Noosa North Shore -> Rainbow
Beach fallback route, and writes a final closure feed for the isolation analyser.

Model policy
------------
* Noosa ferry OPEN: the ferry connector remains available.
* Noosa ferry CLOSED + graph contains the 4WD fallback: inject an exact
  impassable closure over the ferry crossing. The graph can still route via the
  published Cooloola 4WD route in the impassable-only scenario. A permanent
  conditional restriction at Third Cutting blocks that 4WD route in the
  conservative all-blocking scenario.
* Noosa ferry CLOSED + graph does NOT contain the fallback: DO NOT create a
  false full isolation. Instead mark the ferry/corridor edge as conditional.
  This is an explicit network abstraction: the closed ferry itself is not being
  treated as usable; the conditional edge stands in for the published 4WD
  alternative that the graph failed to represent. It remains available in the
  impassable-only scenario and is blocked in all-blocking.
* Noosa ferry UNKNOWN: fail open for topology (do not create false isolation),
  but publish UNKNOWN to the dashboard.

Normal ferry operating hours are deliberately ignored. They are not an
isolation criterion.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import networkx as nx
import requests

NOOSA_STATUS_URL = "https://status.noosa-northshoreferries.com.au/"
NOOSA_INFO_URL = "https://noosa-northshoreferries.com.au/ferry-info/"
COOLOOLA_4WD_URL = "https://parks.qld.gov.au/parks/cooloola/visiting-safely"

NOOSA_CONNECTOR_NAME = "Noosa North Shore Ferry graph connector"

# Official operator map point for the ferry area.
NOOSA_MAP_LAT = -26.37347272926862
NOOSA_MAP_LON = 153.0387010954322

# Published/mapped 4WD access points used only to validate that the released
# road graph actually contains a north-side fallback route. We deliberately do
# NOT add a 60 km synthetic shortcut edge between these coordinates.
THIRD_CUTTING_LAT = -26.329950
THIRD_CUTTING_LON = 153.061390
RAINBOW_4WD_LAT = -25.900788
RAINBOW_4WD_LON = 153.092282

NOOSA_CLOSURE_ID = "noosa-north-shore-ferry-live-status"
COOLOOLA_RESTRICTION_ID = "cooloola-great-beach-drive-4wd-conditional-access"

# If we test Third Cutting -> Rainbow Beach while allowing nodes around the
# ferry, a native ferry edge in the source graph could create a false positive
# by routing south across the river and then back north on ordinary roads.
# Excluding this small ferry area ensures the topology check is genuinely for
# an alternative route that does not use the Noosa ferry.
FERRY_EXCLUSION_RADIUS_M = 1200.0
MAX_FALLBACK_ENDPOINT_SNAP_M = 3000.0


class _VisibleText(HTMLParser):
    BLOCKS = {
        "article", "aside", "br", "div", "footer", "h1", "h2", "h3", "h4",
        "header", "li", "main", "p", "section", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.suppressed += 1
        if not self.suppressed and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.suppressed:
            self.suppressed -= 1
        if not self.suppressed and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\xa0", " ")
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    return parser.text()


def latest_status_update(text: str) -> str:
    """Return only the newest status-page update.

    The operator page lists newest first and separates entries with the literal
    label "Posted:". Historical entries must not influence the live state.
    """
    matches = list(re.finditer(r"(?im)^\s*Posted:\s*$", text))
    if not matches:
        return ""
    start = matches[0].end()
    end = matches[1].start() if len(matches) > 1 else len(text)
    return text[start:end].strip()


OPEN_PATTERNS = [
    r"\bcurrently\s+operating\s+as\s+normal\b",
    r"\bremain\s+open\s+and\s+operating\s+as\s+normal\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is|remain|remains)\s+open\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is)\s+operating\s+as\s+normal\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is)\s+currently\s+operating\b",
]

CLOSED_PATTERNS = [
    r"\bclosed\s+until\s+further\s+notice\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is|remain|remains)\s+closed\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is)\s+temporarily\s+closed\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is|have been|has been)\s+suspended\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is)\s+not\s+operating\b",
    r"\b(?:ferry|ferries|ferry services?|services?)\s+(?:are|is)\s+unable\s+to\s+operate\b",
    r"\bservice\s+suspended\b",
]


def classify_latest_update(update: str) -> Tuple[str, str]:
    if not update:
        return "UNKNOWN", "No current status update block could be identified."
    compact = re.sub(r"\s+", " ", update).strip()
    lower = compact.lower()
    open_hits = [p for p in OPEN_PATTERNS if re.search(p, lower, flags=re.I)]
    closed_hits = [p for p in CLOSED_PATTERNS if re.search(p, lower, flags=re.I)]
    if open_hits and closed_hits:
        return "UNKNOWN", "Latest status update contains conflicting open and closed signals."
    if closed_hits:
        return "CLOSED", compact[:900]
    if open_hits:
        return "OPEN", compact[:900]
    return "UNKNOWN", compact[:900]


def parse_noosa_status_html(html: str) -> Tuple[str, str]:
    text = html_to_text(html)
    update = latest_status_update(text)
    return classify_latest_update(update)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def node_latlon(G: nx.Graph, node: Any) -> Optional[Tuple[float, float]]:
    data = G.nodes[node]
    try:
        return float(data["y"]), float(data["x"])
    except Exception:
        return None


def nearest_node_with_distance(G: nx.Graph, lat: float, lon: float) -> Tuple[Optional[Any], Optional[float]]:
    best_node: Optional[Any] = None
    best_dist = float("inf")
    for node, data in G.nodes(data=True):
        try:
            nlat = float(data["y"])
            nlon = float(data["x"])
        except Exception:
            continue
        d = haversine_m(lat, lon, nlat, nlon)
        if d < best_dist:
            best_dist = d
            best_node = node
    return best_node, (best_dist if best_node is not None else None)


def inspect_4wd_fallback_graph(G: nx.Graph) -> Dict[str, Any]:
    """Check whether the actual graph contains a ferry-independent fallback.

    We test connectivity from Third Cutting to Rainbow Beach while temporarily
    excluding the Noosa ferry area. This prevents a native/source ferry edge
    from making the topology test succeed by simply crossing back to Tewantin.
    """
    start, start_dist = nearest_node_with_distance(G, THIRD_CUTTING_LAT, THIRD_CUTTING_LON)
    end, end_dist = nearest_node_with_distance(G, RAINBOW_4WD_LAT, RAINBOW_4WD_LON)

    result: Dict[str, Any] = {
        "available": False,
        "mode": "conditional_proxy_if_ferry_closed",
        "third_cutting_nearest_node": str(start) if start is not None else "",
        "third_cutting_snap_distance_m": round(float(start_dist), 1) if start_dist is not None else None,
        "rainbow_beach_nearest_node": str(end) if end is not None else "",
        "rainbow_beach_snap_distance_m": round(float(end_dist), 1) if end_dist is not None else None,
        "ferry_exclusion_radius_m": FERRY_EXCLUSION_RADIUS_M,
        "reason": "",
    }

    if start is None or end is None:
        result["reason"] = "Could not find graph nodes near one or both published 4WD access points."
        return result
    if start_dist is None or end_dist is None or start_dist > MAX_FALLBACK_ENDPOINT_SNAP_M or end_dist > MAX_FALLBACK_ENDPOINT_SNAP_M:
        result["reason"] = "One or both 4WD access points are too far from the released graph for a reliable topology check."
        return result

    excluded: set[Any] = set()
    for node, data in G.nodes(data=True):
        try:
            nlat = float(data["y"])
            nlon = float(data["x"])
        except Exception:
            continue
        if haversine_m(NOOSA_MAP_LAT, NOOSA_MAP_LON, nlat, nlon) <= FERRY_EXCLUSION_RADIUS_M:
            excluded.add(node)

    if start in excluded or end in excluded:
        result["reason"] = "A 4WD access endpoint fell inside the ferry exclusion area; topology check is inconclusive."
        return result

    view = nx.subgraph_view(G, filter_node=lambda n: n not in excluded)
    traversal = view.to_undirected(as_view=True) if view.is_directed() else view
    try:
        available = nx.has_path(traversal, start, end)
    except (nx.NodeNotFound, nx.NetworkXError) as exc:
        result["reason"] = f"Topology check failed: {type(exc).__name__}: {exc}"
        return result

    result["available"] = bool(available)
    if available:
        result["mode"] = "native_graph_4wd_route"
        result["reason"] = "Released graph contains a ferry-independent path between Third Cutting and Rainbow Beach."
    else:
        result["reason"] = "Released graph does not contain a ferry-independent path between Third Cutting and Rainbow Beach; conditional proxy will be used if the ferry closes."
    return result


def inspect_4wd_fallback_graph_path(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "available": False,
            "mode": "conditional_proxy_if_ferry_closed",
            "reason": f"Graph file not found for topology check: {path}",
        }
    print(f"[NOOSA] checking Cooloola 4WD fallback topology in graph: {path}")
    G = nx.read_graphml(path)
    result = inspect_4wd_fallback_graph(G)
    result["graph_path"] = str(path)
    print("[NOOSA] 4WD fallback topology: " + json.dumps(result, ensure_ascii=False))
    return result


def read_connectors(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_connector(rows: Iterable[Dict[str, str]], name: str) -> Dict[str, str]:
    target = name.strip().lower()
    for row in rows:
        if str(row.get("name") or "").strip().lower() == target:
            return row
    raise ValueError(f"Required manual connector not found: {name}")


def connector_line(row: Dict[str, str]) -> Dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [
            [float(row["from_lon"]), float(row["from_lat"])],
            [float(row["to_lon"]), float(row["to_lat"])],
        ],
    }


def noosa_closed_feature(connector: Dict[str, str], fetched_at: str) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": connector_line(connector),
        "properties": {
            "event_id": NOOSA_CLOSURE_ID,
            "source_event_id": NOOSA_CLOSURE_ID,
            "source": "noosa_north_shore_ferries_status",
            "jurisdiction": "qld",
            "category_raw": "Ferry closure",
            "category_norm": "road_closed",
            "passability_norm": "impassable",
            "status_norm": "active",
            "reason_norm": "ferry_closure",
            "fetched_at": fetched_at,
            "title": "Noosa North Shore Ferry closed",
            "description": "The dedicated Noosa North Shore Ferries status page explicitly reports the ferry service closed or suspended. The released graph contains a separate ferry-independent Cooloola 4WD fallback route.",
            "road_name": NOOSA_CONNECTOR_NAME,
            "locality": "Tewantin / Noosa North Shore",
            "restrictions_text": "Ferry crossing unavailable; separate high-clearance 4WD fallback may remain available",
            "url": NOOSA_STATUS_URL,
        },
    }


def noosa_closed_conditional_proxy_feature(connector: Dict[str, str], fetched_at: str) -> Dict[str, Any]:
    """Represent the known 4WD fallback when the released graph omits it.

    The geometry is the normal ferry connector only as a *network proxy*. The
    ferry is still reported CLOSED in the status JSON/UI. Treating this edge as
    conditional ensures the model says "conditional access" rather than "fully
    isolated" solely because the source graph failed to contain the published
    Great Beach Drive alternative.
    """
    return {
        "type": "Feature",
        "geometry": connector_line(connector),
        "properties": {
            "event_id": NOOSA_CLOSURE_ID,
            "source_event_id": NOOSA_CLOSURE_ID,
            "source": "noosa_north_shore_ferries_status_plus_qld_parks_fallback",
            "jurisdiction": "qld",
            "category_raw": "Ferry closed; conditional alternate access",
            "category_norm": "restricted",
            "passability_norm": "passable_with_conditions",
            "status_norm": "active",
            "reason_norm": "ferry_closure_with_4wd_fallback",
            "fetched_at": fetched_at,
            "title": "Noosa North Shore Ferry closed — conditional 4WD fallback",
            "description": "The ferry is explicitly closed. Queensland Parks publishes a high-clearance 4WD route from Noosa North Shore to Rainbow Beach, but that route is missing from the released analysis graph. This conditional edge is a modelling proxy for the alternative route; it does not mean the ferry itself is passable.",
            "road_name": NOOSA_CONNECTOR_NAME,
            "locality": "Tewantin / Noosa North Shore / Cooloola",
            "restrictions_text": "Ferry closed; alternative access is high-clearance 4WD only and subject to vehicle permit, beach, tide and park conditions",
            "url": NOOSA_STATUS_URL,
            "fallback_url": COOLOOLA_4WD_URL,
            "model_proxy": True,
        },
    }


def cooloola_conditional_feature(fetched_at: str) -> Dict[str, Any]:
    # Point at Third Cutting: the transition from the conventional road network
    # to the published high-clearance-4WD Cooloola beach route. It makes the real
    # graph route conditional without adding a long synthetic shortcut edge.
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [THIRD_CUTTING_LON, THIRD_CUTTING_LAT]},
        "properties": {
            "event_id": COOLOOLA_RESTRICTION_ID,
            "source_event_id": COOLOOLA_RESTRICTION_ID,
            "source": "qld_parks_published_access",
            "jurisdiction": "qld",
            "category_raw": "Vehicle restriction",
            "category_norm": "restricted",
            "passability_norm": "passable_with_conditions",
            "status_norm": "active",
            "reason_norm": "vehicle_restriction",
            "fetched_at": fetched_at,
            "title": "Cooloola Great Beach Drive — high-clearance 4WD access only",
            "description": "Queensland Parks publishes Cooloola Beach Drive between Noosa North Shore and Rainbow Beach as high-clearance 4WD access requiring a vehicle access permit.",
            "road_name": "",
            "locality": "Noosa North Shore / Cooloola / Rainbow Beach",
            "restrictions_text": "High-clearance 4WD only; vehicle access permit required; beach/tide/park conditions apply",
            "url": COOLOOLA_4WD_URL,
        },
    }


def remove_previous_synthetics(features: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ids = {NOOSA_CLOSURE_ID, COOLOOLA_RESTRICTION_ID}
    out: List[Dict[str, Any]] = []
    for feature in features:
        props = feature.get("properties") or {}
        fid = str(props.get("event_id") or props.get("source_event_id") or props.get("id") or "")
        if fid in ids:
            continue
        out.append(feature)
    return out


def prepare_feed(
    payload: Dict[str, Any],
    connector_rows: List[Dict[str, str]],
    status: str,
    fetched_at: str,
    fallback_available: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if payload.get("type") != "FeatureCollection":
        raise ValueError("Input closures file is not a GeoJSON FeatureCollection")

    noosa_connector = find_connector(connector_rows, NOOSA_CONNECTOR_NAME)
    original = list(payload.get("features") or [])
    features = remove_previous_synthetics(original)

    four_wd_event_injected = False
    ferry_event_mode = "none"

    # Only inject the Third Cutting restriction when the actual graph contains
    # the 4WD route that the point is meant to control.
    if fallback_available:
        features.append(cooloola_conditional_feature(fetched_at))
        four_wd_event_injected = True

    if status == "CLOSED":
        if fallback_available:
            features.append(noosa_closed_feature(noosa_connector, fetched_at))
            ferry_event_mode = "impassable_ferry_closure_with_native_4wd_fallback"
        else:
            features.append(noosa_closed_conditional_proxy_feature(noosa_connector, fetched_at))
            ferry_event_mode = "conditional_access_proxy_for_missing_4wd_graph"

    out = dict(payload)
    out["features"] = features
    meta = {
        "input_feature_count": len(original),
        "output_feature_count": len(features),
        "noosa_ferry_event_injected": status == "CLOSED",
        "noosa_ferry_event_mode": ferry_event_mode,
        "cooloola_4wd_conditional_event_injected": four_wd_event_injected,
    }
    return out, meta


def fetch_status(timeout_s: float) -> Tuple[str, str, int, str]:
    response = requests.get(
        NOOSA_STATUS_URL,
        timeout=timeout_s,
        headers={"User-Agent": "qld-isolation-ferry-status/1.0"},
    )
    response.raise_for_status()
    status, evidence = parse_noosa_status_html(response.text)
    return status, evidence, response.status_code, response.url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Prepare Noosa Ferry live status and conditional Cooloola 4WD access for the QLD isolation model.")
    p.add_argument("--manual-connectors", required=True)
    p.add_argument("--graph", required=True, help="Released QLD GraphML used to verify the Cooloola 4WD fallback topology.")
    p.add_argument("--closures-in", required=True)
    p.add_argument("--closures-out", required=True)
    p.add_argument("--status-out", required=True)
    p.add_argument("--http-timeout-s", type=float, default=30.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    fetched_at = utc_now_iso()
    error = ""
    http_status: Optional[int] = None
    final_url = NOOSA_STATUS_URL
    try:
        status, evidence, http_status, final_url = fetch_status(args.http_timeout_s)
    except Exception as exc:
        status = "UNKNOWN"
        evidence = ""
        error = f"{type(exc).__name__}: {exc}"

    topology = inspect_4wd_fallback_graph_path(Path(args.graph))
    fallback_available = bool(topology.get("available"))

    connectors = read_connectors(Path(args.manual_connectors))
    payload = json.loads(Path(args.closures_in).read_text(encoding="utf-8"))
    prepared, meta = prepare_feed(payload, connectors, status, fetched_at, fallback_available)

    closures_out = Path(args.closures_out)
    closures_out.parent.mkdir(parents=True, exist_ok=True)
    closures_out.write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8")

    if fallback_available:
        closed_policy = (
            "An exact impassable closure is injected over the ferry crossing. "
            "The actual released graph retains the separate Cooloola 4WD route in impassable-only analysis; "
            "the Third Cutting conditional restriction blocks that fallback in all-blocking analysis."
        )
    else:
        closed_policy = (
            "The ferry is still reported CLOSED, but the released graph does not contain a usable Cooloola fallback path. "
            "To prevent false full isolation, the crossing is represented as a conditional-access proxy for the published 4WD alternative; "
            "the proxy is available in impassable-only analysis and blocked in all-blocking analysis."
        )

    status_doc = {
        "status": status,
        "check_url": NOOSA_STATUS_URL,
        "source_url": final_url,
        "info_url": NOOSA_INFO_URL,
        "fetched_at": fetched_at,
        "http_status": http_status,
        "evidence": evidence,
        "error": error,
        "map_lat": NOOSA_MAP_LAT,
        "map_lon": NOOSA_MAP_LON,
        "connector_name": NOOSA_CONNECTOR_NAME,
        "connector_policy": {
            "OPEN": "Noosa ferry crossing remains traversable.",
            "CLOSED": closed_policy,
            "UNKNOWN": "No ferry closure is injected; topology fails open to avoid false isolation, while the dashboard reports UNKNOWN.",
        },
        "operating_hours_used_in_isolation": False,
        "four_wd_fallback": {
            "status": "CONDITIONAL_ACCESS",
            "graph_route_available": fallback_available,
            "model_mode": topology.get("mode", "conditional_proxy_if_ferry_closed"),
            "model_policy": (
                "Use the actual graph route in impassable-only analysis and block it at Third Cutting in all-blocking analysis."
                if fallback_available
                else "If the ferry closes, use a conditional-access proxy rather than falsely declaring full isolation because the graph omitted the published 4WD alternative."
            ),
            "access": "High-clearance 4WD only; vehicle access permit and beach/tide/park conditions apply.",
            "source_url": COOLOOLA_4WD_URL,
            "third_cutting_lat": THIRD_CUTTING_LAT,
            "third_cutting_lon": THIRD_CUTTING_LON,
            "rainbow_beach_access_lat": RAINBOW_4WD_LAT,
            "rainbow_beach_access_lon": RAINBOW_4WD_LON,
            "topology_check": topology,
        },
        **meta,
    }
    status_out = Path(args.status_out)
    status_out.parent.mkdir(parents=True, exist_ok=True)
    status_out.write_text(json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(status_doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
