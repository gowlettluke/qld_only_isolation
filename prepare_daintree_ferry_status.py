#!/usr/bin/env python3
"""Prepare the QLD Traffic feed with live Daintree River Ferry status.

The road graph contains an audited manual connector across the Daintree River so
baseline connectivity reflects a normally operating ferry. This helper checks
Birdon's official Daintree Ferry pages before each isolation run and:

* OPEN: leaves the QLD Traffic feed unchanged.
* CLOSED: injects an impassable synthetic event exactly along the Daintree
  manual connector. The existing closure matcher therefore blocks the crossing
  in the current closure scenarios while preserving the pre-closure baseline.
* UNKNOWN: leaves the connector traversable and writes an explicit UNKNOWN
  status for the dashboard. This avoids false isolation if the website is down,
  changes format, gives an unrecognised state, or its status surfaces disagree.

The helper also writes daintree_ferry_status.json for diagnostics and the map UI.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

QLD_TRAFFIC_URL = "https://data.qldtraffic.qld.gov.au/events_v2.geojson"
DAINTREE_STATUS_URL = "https://daintreeferry.com.au/status-updates/"
DAINTREE_HOME_URL = "https://daintreeferry.com.au/"
USER_AGENT = "qld-isolation-monitor/1.0 (+https://github.com/gowlettluke/qld_only_isolation)"


class HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._heading_tag: Optional[str] = None
        self._parts: List[str] = []
        self.headings: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_tag = tag
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._heading_tag and tag.lower() == self._heading_tag:
            text = " ".join(" ".join(self._parts).split())
            if text:
                self.headings.append(text)
            self._heading_tag = None
            self._parts = []


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_ferry_status(html: str) -> Tuple[str, str]:
    """Return (OPEN|CLOSED|UNKNOWN, evidence) from one official page.

    The preferred marker is the exact current-status heading, e.g. FERRY OPEN.
    This deliberately avoids historical update headings such as
    "Ferry closed for emergency repairs".

    If the heading template changes, a unique global "Status: OPEN/CLOSED"
    indicator is accepted as a conservative fallback. Conflicting fallback
    markers produce UNKNOWN rather than guessing.
    """
    parser = HeadingParser()
    try:
        parser.feed(html)
    except Exception:
        parser.headings = []

    exact_matches: List[Tuple[str, str]] = []
    for heading in parser.headings:
        match = re.fullmatch(r"FERRY\s+(OPEN|CLOSED)", heading.strip(), flags=re.IGNORECASE)
        if match:
            exact_matches.append((match.group(1).upper(), heading))

    exact_states = {status for status, _ in exact_matches}
    if len(exact_states) == 1 and exact_matches:
        return exact_matches[0]
    if len(exact_states) > 1:
        return "UNKNOWN", "Conflicting exact FERRY OPEN/CLOSED headings found"

    fallback = re.findall(r"\bStatus\s*:\s*(OPEN|CLOSED)\b", html, flags=re.IGNORECASE)
    fallback_states = {value.upper() for value in fallback}
    if len(fallback_states) == 1 and fallback:
        state = fallback[0].upper()
        return state, f"Status: {state}"
    if len(fallback_states) > 1:
        return "UNKNOWN", "Conflicting Status: OPEN/CLOSED markers found"

    return "UNKNOWN", "No recognised current ferry status marker was found"


def _check_status_url(url: str, timeout_s: float) -> Dict[str, Any]:
    checked: Dict[str, Any] = {
        "url": url,
        "http_status": None,
        "status": "UNKNOWN",
        "evidence": "",
        "error": "",
    }
    try:
        response = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        checked["http_status"] = response.status_code
        response.raise_for_status()
        status, evidence = parse_ferry_status(response.text)
        checked["status"] = status
        checked["evidence"] = evidence
        if status == "UNKNOWN":
            checked["error"] = "Page loaded but no unambiguous OPEN/CLOSED marker was found"
    except Exception as exc:
        checked["error"] = f"{type(exc).__name__}: {exc}"
    return checked


def reconcile_ferry_checks(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Tuple[str, str]:
    """Reconcile official status surfaces without guessing.

    The dedicated Status Updates page is authoritative. If that page cannot be
    parsed as OPEN/CLOSED, the result is UNKNOWN even if the homepage still has
    a familiar status marker. This deliberately catches new states such as
    DELAYED/SUSPENDED and template changes instead of silently treating them as
    OPEN. The homepage is used as an independent consistency check when it also
    reports a recognised state.
    """
    primary_status = str(primary.get("status") or "UNKNOWN").upper()
    secondary_status = str(secondary.get("status") or "UNKNOWN").upper()

    if primary_status not in {"OPEN", "CLOSED"}:
        return "UNKNOWN", (
            primary.get("error")
            or "The official Status Updates page did not return a recognised OPEN/CLOSED state"
        )

    if secondary_status in {"OPEN", "CLOSED"} and secondary_status != primary_status:
        return "UNKNOWN", "Official Daintree Ferry pages disagree on current operating status"

    return primary_status, ""


def fetch_ferry_status(timeout_s: float = 20.0) -> Dict[str, Any]:
    """Check the official status page and cross-check it against the homepage."""
    fetched_at = utc_now_iso()
    primary = _check_status_url(DAINTREE_STATUS_URL, timeout_s)
    secondary = _check_status_url(DAINTREE_HOME_URL, timeout_s)
    checks = [primary, secondary]
    status, error = reconcile_ferry_checks(primary, secondary)

    evidence = "; ".join(
        f"{c['url']} -> {c['status']} ({c.get('evidence') or c.get('error') or 'no evidence'})"
        for c in checks
    )

    return {
        "status": status,
        "check_url": DAINTREE_STATUS_URL,
        "source_url": DAINTREE_STATUS_URL,
        "fetched_at": fetched_at,
        "evidence": evidence,
        "error": error,
        "source_checks": checks,
    }


def load_daintree_connector(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Manual connector file not found: {path}")

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = str(row.get("name") or "")
            if "daintree river ferry" not in name.lower():
                continue
            try:
                from_lat = float(row["from_lat"])
                from_lon = float(row["from_lon"])
                to_lat = float(row["to_lat"])
                to_lon = float(row["to_lon"])
            except Exception as exc:
                raise ValueError("Daintree connector is missing valid endpoint coordinates") from exc
            return {
                "name": name or "Daintree River Ferry graph connector",
                "from_lat": from_lat,
                "from_lon": from_lon,
                "to_lat": to_lat,
                "to_lon": to_lon,
                "map_lat": (from_lat + to_lat) / 2.0,
                "map_lon": (from_lon + to_lon) / 2.0,
            }

    raise ValueError("No Daintree River Ferry connector found in manual connector CSV")


def closed_ferry_feature(connector: Dict[str, Any]) -> Dict[str, Any]:
    """Create an exact blocking event on the audited Daintree connector."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [connector["from_lon"], connector["from_lat"]],
                [connector["to_lon"], connector["to_lat"]],
            ],
        },
        "properties": {
            "id": "daintree-ferry-live-status",
            "event_id": "daintree-ferry-live-status",
            "headline": "Daintree River Ferry closed",
            "title": "Daintree River Ferry closed",
            "description": "Official Daintree Ferry service status reports the ferry is closed.",
            "advice": "Road closed",
            "event_type": "Road Closure",
            "event_subtype": "Ferry closure",
            "road": connector["name"],
            "road_name": connector["name"],
            "locality": "Daintree",
            "status": "Current",
            "url": DAINTREE_STATUS_URL,
            "source_url": DAINTREE_STATUS_URL,
            "ferry_status": "CLOSED",
            "impact": {
                "impact_type": "Closures",
                "impact_subtype": "Road Closed",
            },
        },
    }


def prepare_feed(
    connectors_path: Path,
    closures_out: Path,
    status_out: Path,
    timeout_s: float,
) -> Dict[str, Any]:
    connector = load_daintree_connector(connectors_path)
    status_meta = fetch_ferry_status(timeout_s=timeout_s)

    qld_response = requests.get(
        QLD_TRAFFIC_URL,
        timeout=max(timeout_s, 30.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json,application/json"},
    )
    qld_response.raise_for_status()
    payload = qld_response.json()
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError("QLD Traffic response was not a GeoJSON FeatureCollection")

    count_before = len(payload["features"])
    injected = None
    if status_meta["status"] == "CLOSED":
        injected = closed_ferry_feature(connector)
        payload["features"].append(injected)

    status_meta.update(
        {
            "connector_name": connector["name"],
            "map_lat": connector["map_lat"],
            "map_lon": connector["map_lon"],
            "connector_policy": (
                "blocked in current closure scenarios"
                if status_meta["status"] == "CLOSED"
                else "left traversable"
                if status_meta["status"] == "OPEN"
                else "left traversable; status uncertainty must be shown to the user"
            ),
            "synthetic_event_injected": injected is not None,
            "synthetic_event_blocks_isolation": status_meta["status"] == "CLOSED",
            "qld_traffic_url": QLD_TRAFFIC_URL,
            "qld_traffic_http_status": qld_response.status_code,
            "qld_traffic_feature_count_before_ferry": count_before,
            "qld_traffic_feature_count_after_ferry": len(payload["features"]),
        }
    )

    closures_out.parent.mkdir(parents=True, exist_ok=True)
    status_out.parent.mkdir(parents=True, exist_ok=True)
    closures_out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    status_out.write_text(json.dumps(status_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[DAINTREE] "
        f"status={status_meta['status']} "
        f"injected={status_meta['synthetic_event_injected']} "
        f"blocking={status_meta['synthetic_event_blocks_isolation']}"
    )
    print(f"[DAINTREE] evidence={status_meta['evidence']}")
    if status_meta.get("error"):
        print(f"[DAINTREE] warning={status_meta['error']}")
    return status_meta


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare QLD Traffic feed with Daintree Ferry live status"
    )
    parser.add_argument("--manual-connectors", default="manual_graph_connectors.csv")
    parser.add_argument("--closures-out", required=True)
    parser.add_argument("--status-out", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    prepare_feed(
        Path(args.manual_connectors),
        Path(args.closures_out),
        Path(args.status_out),
        args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
