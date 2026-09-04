#!/usr/bin/env python3
"""Prepare the QLD Traffic feed for *current* isolation analysis.

This script sits between the ferry preparation steps and the isolation analyser.

It fixes two hazards in the upstream QLD Traffic GeoJSON:

1. Published does not mean active *now*.
   QLD Traffic can publish future special events/closures well before their
   duration.start and can leave records published after duration.end. Those
   records are excluded from the current analysis outside their time window.

2. area_alert polygons are alert extents, not "every road inside is closed".
   For area_alert=true features, only explicit LineString/MultiLineString
   geometry is retained as potentially blocking road geometry. Polygon/point
   components are retained as a separate normalised informational feature with
   passability_norm=unknown so the dashboard can show them without the analyser
   blocking every graph edge inside the alert area.

If a timestamp is missing or cannot be parsed, the event is retained rather
than silently discarded. This is intentionally fail-safe for genuine current
closures.

No external packages are required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BRISBANE_TZ = dt.timezone(dt.timedelta(hours=10))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {"1", "true", "t", "yes", "y"}


def parse_time(value: Any) -> Optional[dt.datetime]:
    """Parse a QLD Traffic ISO timestamp into an aware datetime.

    Naive timestamps are interpreted as Queensland local time (+10:00).
    """
    text = clean(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        # Date-only values are uncommon but valid enough for envelope filtering.
        try:
            parsed = dt.datetime.combine(dt.date.fromisoformat(text), dt.time.min)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRISBANE_TZ)
    return parsed


def event_id(props: Dict[str, Any]) -> str:
    return clean(
        props.get("source_event_id")
        or props.get("id")
        or props.get("event_id")
        or props.get("eventId")
        or props.get("guid")
        or props.get("reference")
        or props.get("url")
        or props.get("title")
        or props.get("headline")
    )


def event_times(props: Dict[str, Any]) -> Tuple[str, str, bool]:
    duration = props.get("duration") if isinstance(props.get("duration"), dict) else {}

    start = clean(
        duration.get("start")
        or props.get("start_time")
        or props.get("startTime")
        or props.get("fromDate")
        or props.get("from")
    )
    end = clean(
        duration.get("end")
        or props.get("end_time")
        or props.get("endTime")
        or props.get("toDate")
        or props.get("to")
    )
    has_recurrence = bool(duration.get("active_days") or duration.get("recurrences"))
    return start, end, has_recurrence


def temporal_state(props: Dict[str, Any], now: dt.datetime) -> Tuple[str, str, str, bool]:
    """Return state, start_text, end_text, has_recurrence.

    state is one of current/future/expired/invalid_time.
    """
    start_text, end_text, has_recurrence = event_times(props)
    start = parse_time(start_text)
    end = parse_time(end_text)

    # If a supplied timestamp is malformed, retain the event. False negatives
    # are more dangerous than a single conservative false positive here.
    if (start_text and start is None) or (end_text and end is None):
        return "invalid_time", start_text, end_text, has_recurrence

    if start is not None and end is not None and end < start:
        return "invalid_time", start_text, end_text, has_recurrence

    if start is not None and now < start:
        return "future", start_text, end_text, has_recurrence
    if end is not None and now > end:
        return "expired", start_text, end_text, has_recurrence
    return "current", start_text, end_text, has_recurrence


def iter_leaf_geometries(geometry: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    if not isinstance(geometry, dict):
        return
    gtype = clean(geometry.get("type"))
    if gtype == "GeometryCollection":
        for child in geometry.get("geometries") or []:
            yield from iter_leaf_geometries(child)
        return

    # Keep Multi* objects intact. The isolation analyser already understands
    # them and this avoids changing the supplied line topology.
    if gtype:
        yield geometry


def split_area_alert_geometry(
    geometry: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split an area alert into blocking-capable lines and context geometry."""
    road_lines: List[Dict[str, Any]] = []
    context: List[Dict[str, Any]] = []
    for part in iter_leaf_geometries(geometry):
        gtype = clean(part.get("type"))
        if gtype in {"LineString", "MultiLineString"}:
            road_lines.append(part)
        else:
            # Point/MultiPoint/Polygon/MultiPolygon and unexpected types are
            # contextual only for area_alert records.
            context.append(part)
    return road_lines, context


def combine_geometry(parts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"type": "GeometryCollection", "geometries": parts}


def _impact(props: Dict[str, Any]) -> Dict[str, Any]:
    return props.get("impact") if isinstance(props.get("impact"), dict) else {}


def _road_summary(props: Dict[str, Any]) -> Dict[str, Any]:
    return props.get("road_summary") if isinstance(props.get("road_summary"), dict) else {}


def make_area_alert_context_feature(
    original: Dict[str, Any],
    context_geometry: Dict[str, Any],
    start_time: str,
    end_time: str,
    now: dt.datetime,
) -> Dict[str, Any]:
    props = dict(original.get("properties") or {})
    impact = _impact(props)
    road = _road_summary(props)
    source_id = event_id(props)

    title = clean(props.get("headline") or props.get("title"))
    description = clean(props.get("description") or props.get("information") or props.get("alert_message"))
    advice = clean(props.get("advice"))
    url = clean(props.get("url") or props.get("web_link"))

    # Supplying category_norm/passability_norm causes the existing analyser to
    # treat this as an already-normalised, explicitly non-blocking event.
    info_props: Dict[str, Any] = {
        "event_id": f"qld-area-alert-context-{source_id or 'unknown'}",
        "source_event_id": source_id,
        "source": "qldtraffic",
        "jurisdiction": "qld",
        "category_raw": clean(props.get("event_type") or "Area alert"),
        "category_norm": "area_alert",
        "passability_norm": "unknown",
        "status_norm": "active",
        "reason_norm": "area_alert_context",
        "start_time": start_time,
        "end_time": end_time,
        "last_updated": clean(props.get("last_updated") or props.get("lastUpdated")),
        "fetched_at": now.astimezone(dt.timezone.utc).isoformat(),
        "title": title or "QLD Traffic area alert",
        "description": description,
        "road_name": clean(road.get("road_name") or props.get("road_name") or props.get("road")),
        "locality": clean(road.get("locality") or props.get("locality")),
        "direction": clean(impact.get("direction") or props.get("direction")),
        "lanes_affected": clean(props.get("lanes") or props.get("lanes_affected")),
        "restrictions_text": advice,
        "url": url,
        "raw_title": title,
        "raw_description": clean(props.get("description")),
        "raw_advice": advice,
        "raw_event_type": clean(props.get("event_type")),
        "raw_event_subtype": clean(props.get("event_subtype")),
        "raw_impact_type": clean(impact.get("impact_type")),
        "raw_impact_subtype": clean(impact.get("impact_subtype")),
        "raw_road": clean(road.get("road_name") or props.get("road_name") or props.get("road")),
        "raw_locality": clean(road.get("locality") or props.get("locality")),
        "raw_status": clean(props.get("status")),
        "area_alert": True,
        "area_alert_informational": True,
        "area_alert_note": (
            "QLD Traffic alert extent only. Polygon/point geometry is shown for "
            "context and is not used to block road-network edges."
        ),
    }

    # Preserve a few useful upstream display fields without copying the entire
    # nested payload back into every output row.
    for key in ("alert_message", "information", "published", "event_priority"):
        if key in props:
            info_props[key] = props[key]

    return {
        "type": "Feature",
        "geometry": context_geometry,
        "properties": info_props,
    }


def process_feature(
    feature: Dict[str, Any],
    now: dt.datetime,
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    source_id = event_id(props)

    state, start_time, end_time, has_recurrence = temporal_state(props, now)
    if has_recurrence:
        report["recurrence_envelope_events"] += 1

    if state == "future":
        report["future_excluded"] += 1
        if len(report["future_event_ids"]) < 100:
            report["future_event_ids"].append(source_id)
        return []
    if state == "expired":
        report["expired_excluded"] += 1
        if len(report["expired_event_ids"]) < 100:
            report["expired_event_ids"].append(source_id)
        return []
    if state == "invalid_time":
        report["invalid_time_kept"] += 1
        if len(report["invalid_time_event_ids"]) < 100:
            report["invalid_time_event_ids"].append(source_id)

    if not truthy(props.get("area_alert")):
        report["ordinary_features_kept"] += 1
        return [feature]

    # Idempotence: a context feature produced by an earlier invocation should
    # simply pass through.
    if truthy(props.get("area_alert_informational")) or (
        clean(props.get("category_norm")) == "area_alert"
        and clean(props.get("passability_norm")) == "unknown"
    ):
        report["area_alert_context_features"] += 1
        return [feature]

    report["area_alert_events"] += 1
    road_lines, context = split_area_alert_geometry(feature.get("geometry"))
    out: List[Dict[str, Any]] = []

    if road_lines:
        line_feature = {
            "type": "Feature",
            "geometry": combine_geometry(road_lines),
            "properties": dict(props),
        }
        line_feature["properties"]["_analysis_geometry_role"] = "explicit_road_line"
        out.append(line_feature)
        report["area_alert_line_features"] += 1
    else:
        report["area_alert_no_line_events"] += 1
        if len(report["area_alert_no_line_event_ids"]) < 100:
            report["area_alert_no_line_event_ids"].append(source_id)

    if context:
        context_geometry = combine_geometry(context)
        assert context_geometry is not None
        out.append(
            make_area_alert_context_feature(
                feature,
                context_geometry,
                start_time,
                end_time,
                now,
            )
        )
        report["area_alert_context_features"] += 1

    # If the upstream feature was malformed and contained neither line nor
    # contextual leaf geometry, retain it as-is rather than silently delete it.
    if not out:
        report["malformed_area_alert_kept"] += 1
        return [feature]

    return out


def process_feature_collection(
    payload: Dict[str, Any],
    now: dt.datetime,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    features = payload.get("features") if isinstance(payload.get("features"), list) else []
    report: Dict[str, Any] = {
        "checked_at": now.astimezone(dt.timezone.utc).isoformat(),
        "policy": {
            "temporal_filter": (
                "Exclude features before duration.start and after duration.end. "
                "Missing/unparseable timestamps are retained."
            ),
            "area_alert": (
                "For area_alert=true, only explicit line geometry can block the "
                "road graph. Polygon/point geometry is retained as non-blocking context."
            ),
            "recurrences": (
                "When recurrence metadata is present, duration start/end are used "
                "as the outer activity envelope; recurrence-specific subwindows are "
                "not interpreted by this preprocessor."
            ),
        },
        "input_features": len(features),
        "output_features": 0,
        "ordinary_features_kept": 0,
        "future_excluded": 0,
        "expired_excluded": 0,
        "invalid_time_kept": 0,
        "recurrence_envelope_events": 0,
        "area_alert_events": 0,
        "area_alert_line_features": 0,
        "area_alert_context_features": 0,
        "area_alert_no_line_events": 0,
        "malformed_area_alert_kept": 0,
        "future_event_ids": [],
        "expired_event_ids": [],
        "invalid_time_event_ids": [],
        "area_alert_no_line_event_ids": [],
    }

    output_features: List[Dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        output_features.extend(process_feature(feature, now, report))

    report["output_features"] = len(output_features)

    output = dict(payload)
    output["type"] = "FeatureCollection"
    output["features"] = output_features
    return output, report


def parse_now(value: Optional[str]) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError(f"Could not parse --now timestamp: {value}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--closures-in", required=True, type=Path)
    parser.add_argument("--closures-out", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path)
    parser.add_argument(
        "--now",
        default="",
        help="Optional ISO timestamp for deterministic testing; defaults to current time.",
    )
    args = parser.parse_args()

    now = parse_now(args.now or None)
    payload = json.loads(args.closures_in.read_text(encoding="utf-8"))
    output, report = process_feature_collection(payload, now)

    args.closures_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.closures_out.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "[QLDTRAFFIC FILTER] "
        f"input={report['input_features']} output={report['output_features']} "
        f"future_excluded={report['future_excluded']} "
        f"expired_excluded={report['expired_excluded']} "
        f"area_alerts={report['area_alert_events']} "
        f"area_alert_no_lines={report['area_alert_no_line_events']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
