#!/usr/bin/env python3
import datetime as dt
import unittest

import prepare_current_qldtraffic_events as mod


NOW = dt.datetime(2026, 9, 4, 11, 20, tzinfo=mod.BRISBANE_TZ)


def feature(
    event_id,
    *,
    start=None,
    end=None,
    area_alert=False,
    geometry=None,
    event_type="Hazard",
):
    duration = {}
    if start is not None:
        duration["start"] = start
    if end is not None:
        duration["end"] = end
    return {
        "type": "Feature",
        "geometry": geometry or {"type": "LineString", "coordinates": [[153.0, -27.0], [153.01, -27.01]]},
        "properties": {
            "id": event_id,
            "status": "Published",
            "event_type": event_type,
            "area_alert": area_alert,
            "duration": duration,
            "impact": {
                "impact_type": "Closures",
                "impact_subtype": "Road closed to all traffic",
            },
            "road_summary": {
                "road_name": "Example Road",
                "locality": "Example",
            },
            "description": "Road closed to all traffic",
            "url": f"https://api.qldtraffic.qld.gov.au/v2/events/{event_id}",
        },
    }


class TemporalTests(unittest.TestCase):
    def test_nested_duration_start_is_used(self):
        f = feature(
            825889,
            start="2026-09-13T04:30:00+10:00",
            end="2026-09-13T15:45:00+10:00",
            event_type="Special event",
        )
        state, start, end, recurrence = mod.temporal_state(f["properties"], NOW)
        self.assertEqual(state, "future")
        self.assertEqual(start, "2026-09-13T04:30:00+10:00")
        self.assertEqual(end, "2026-09-13T15:45:00+10:00")
        self.assertFalse(recurrence)

    def test_future_event_is_excluded(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                feature(
                    825889,
                    start="2026-09-13T04:30:00+10:00",
                    end="2026-09-13T15:45:00+10:00",
                    event_type="Special event",
                )
            ],
        }
        out, report = mod.process_feature_collection(payload, NOW)
        self.assertEqual(out["features"], [])
        self.assertEqual(report["future_excluded"], 1)

    def test_toowoomba_future_event_is_excluded(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                feature(
                    824297,
                    start="2026-09-19T05:00:00+10:00",
                    end="2026-09-19T14:00:00+10:00",
                    event_type="Special event",
                )
            ],
        }
        out, report = mod.process_feature_collection(payload, NOW)
        self.assertEqual(len(out["features"]), 0)
        self.assertIn("824297", report["future_event_ids"])

    def test_expired_event_is_excluded_even_if_published(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                feature(
                    772413,
                    start="2026-05-07T10:00:00+10:00",
                    end="2026-05-12T13:30:00+10:00",
                )
            ],
        }
        out, report = mod.process_feature_collection(payload, NOW)
        self.assertEqual(out["features"], [])
        self.assertEqual(report["expired_excluded"], 1)

    def test_current_event_is_kept(self):
        f = feature(
            123,
            start="2026-09-04T09:00:00+10:00",
            end="2026-09-04T17:00:00+10:00",
        )
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(len(out["features"]), 1)
        self.assertEqual(out["features"][0], f)
        self.assertEqual(report["ordinary_features_kept"], 1)

    def test_missing_times_are_kept(self):
        f = feature(124)
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(out["features"], [f])

    def test_malformed_time_fails_safe_and_keeps_event(self):
        f = feature(125, start="definitely-not-a-time")
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(out["features"], [f])
        self.assertEqual(report["invalid_time_kept"], 1)

    def test_offset_aware_comparison(self):
        f = feature(
            126,
            start="2026-09-04T00:30:00Z",  # 10:30 Brisbane
            end="2026-09-04T02:00:00Z",    # 12:00 Brisbane
        )
        state, *_ = mod.temporal_state(f["properties"], NOW)
        self.assertEqual(state, "current")


class AreaAlertTests(unittest.TestCase):
    def test_polygon_and_point_are_nonblocking_context_only(self):
        f = feature(
            825889,
            start="2026-09-04T09:00:00+10:00",
            end="2026-09-04T17:00:00+10:00",
            area_alert=True,
            event_type="Special event",
            geometry={
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Point", "coordinates": [153.0, -26.7]},
                    {
                        "type": "Polygon",
                        "coordinates": [[
                            [153.0, -26.8],
                            [153.2, -26.8],
                            [153.2, -26.6],
                            [153.0, -26.6],
                            [153.0, -26.8],
                        ]],
                    },
                ],
            },
        )
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(len(out["features"]), 1)
        p = out["features"][0]["properties"]
        self.assertEqual(p["category_norm"], "area_alert")
        self.assertEqual(p["passability_norm"], "unknown")
        self.assertTrue(p["area_alert_informational"])
        self.assertEqual(report["area_alert_no_line_events"], 1)

    def test_explicit_line_is_separated_and_preserved_for_analysis(self):
        f = feature(
            200,
            start="2026-09-04T09:00:00+10:00",
            end="2026-09-04T17:00:00+10:00",
            area_alert=True,
            geometry={
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Point", "coordinates": [153.0, -27.0]},
                    {"type": "LineString", "coordinates": [[153.0, -27.0], [153.1, -27.1]]},
                    {
                        "type": "Polygon",
                        "coordinates": [[
                            [153.0, -27.2],
                            [153.2, -27.2],
                            [153.2, -27.0],
                            [153.0, -27.0],
                            [153.0, -27.2],
                        ]],
                    },
                ],
            },
        )
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(len(out["features"]), 2)
        line = out["features"][0]
        context = out["features"][1]
        self.assertEqual(line["geometry"]["type"], "LineString")
        self.assertEqual(line["properties"]["_analysis_geometry_role"], "explicit_road_line")
        self.assertNotIn("passability_norm", line["properties"])
        self.assertEqual(context["properties"]["passability_norm"], "unknown")
        self.assertEqual(report["area_alert_line_features"], 1)
        self.assertEqual(report["area_alert_context_features"], 1)

    def test_area_alert_info_processing_is_idempotent(self):
        f = feature(
            201,
            start="2026-09-04T09:00:00+10:00",
            end="2026-09-04T17:00:00+10:00",
            area_alert=True,
            geometry={"type": "Polygon", "coordinates": [[[153,-27],[153.1,-27],[153.1,-27.1],[153,-27]]]},
        )
        first, _ = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        second, _ = mod.process_feature_collection(first, NOW)
        self.assertEqual(second, first)

    def test_non_area_alert_geometry_is_not_changed(self):
        f = feature(
            202,
            start="2026-09-04T09:00:00+10:00",
            end="2026-09-04T17:00:00+10:00",
            area_alert=False,
            geometry={"type": "Polygon", "coordinates": [[[153,-27],[153.1,-27],[153.1,-27.1],[153,-27]]]},
        )
        out, _ = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(out["features"][0], f)

    def test_synthetic_ferry_feature_without_duration_is_untouched(self):
        f = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[153.0, -26.3], [153.01, -26.31]]},
            "properties": {
                "id": "noosa-north-shore-ferry-live-status",
                "advice": "Road closed",
                "impact": {"impact_type": "Closures", "impact_subtype": "Road closed to all traffic"},
            },
        }
        out, report = mod.process_feature_collection({"type": "FeatureCollection", "features": [f]}, NOW)
        self.assertEqual(out["features"], [f])
        self.assertEqual(report["ordinary_features_kept"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
