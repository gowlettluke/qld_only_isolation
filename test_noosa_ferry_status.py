import csv
import unittest
from pathlib import Path

import networkx as nx

import prepare_noosa_ferry_status as mod


class NoosaStatusParsingTests(unittest.TestCase):
    def page(self, latest: str, older: str = "") -> str:
        older_html = f"<div>Posted:</div><h2>Older</h2><p>{older}</p>" if older else ""
        return f"<html><body><h1>Ferry Status</h1><div>Posted:</div><h2>Latest</h2><p>{latest}</p>{older_html}</body></html>"

    def test_latest_open_ignores_older_closed(self):
        html = self.page(
            "Our ferry services are currently operating as normal between Tewantin and Noosa Northshore.",
            "Ferry services are closed until further notice.",
        )
        status, evidence = mod.parse_noosa_status_html(html)
        self.assertEqual(status, "OPEN")
        self.assertIn("currently operating as normal", evidence)
        self.assertNotIn("closed until further notice", evidence.lower())

    def test_latest_closed_ignores_older_open(self):
        html = self.page(
            "Ferry services are closed until further notice due to unsafe river conditions.",
            "Ferry services are currently operating as normal.",
        )
        status, evidence = mod.parse_noosa_status_html(html)
        self.assertEqual(status, "CLOSED")
        self.assertIn("closed until further notice", evidence.lower())

    def test_latest_ambiguous_is_unknown(self):
        status, _ = mod.parse_noosa_status_html(self.page("Please check back for further service updates."))
        self.assertEqual(status, "UNKNOWN")

    def test_latest_conflict_is_unknown(self):
        status, _ = mod.parse_noosa_status_html(
            self.page("Ferry services are currently operating as normal. Ferry services are closed until further notice.")
        )
        self.assertEqual(status, "UNKNOWN")

    def test_no_posted_block_is_unknown(self):
        status, _ = mod.parse_noosa_status_html("<html><body><h1>Ferry Status</h1><p>No update.</p></body></html>")
        self.assertEqual(status, "UNKNOWN")

    def test_future_planned_closure_does_not_get_treated_as_current_closed(self):
        status, _ = mod.parse_noosa_status_html(
            self.page("Ferry services will be closed from 11pm tomorrow for scheduled maintenance.")
        )
        self.assertEqual(status, "UNKNOWN")


class NoosaTopologyTests(unittest.TestCase):
    def graph(self, connected: bool) -> nx.Graph:
        G = nx.Graph()
        G.add_node("third", x=mod.THIRD_CUTTING_LON, y=mod.THIRD_CUTTING_LAT)
        G.add_node("mid", x=153.075, y=-26.10)
        G.add_node("rainbow", x=mod.RAINBOW_4WD_LON, y=mod.RAINBOW_4WD_LAT)
        # Ferry-area node is deliberately irrelevant/excluded.
        G.add_node("ferry", x=mod.NOOSA_MAP_LON, y=mod.NOOSA_MAP_LAT)
        if connected:
            G.add_edge("third", "mid")
            G.add_edge("mid", "rainbow")
        else:
            G.add_edge("third", "ferry")
            G.add_edge("ferry", "rainbow")
        return G

    def test_native_4wd_path_detected_without_ferry(self):
        result = mod.inspect_4wd_fallback_graph(self.graph(True))
        self.assertTrue(result["available"])
        self.assertEqual(result["mode"], "native_graph_4wd_route")

    def test_path_via_ferry_area_does_not_count_as_4wd_fallback(self):
        result = mod.inspect_4wd_fallback_graph(self.graph(False))
        self.assertFalse(result["available"])
        self.assertEqual(result["mode"], "conditional_proxy_if_ferry_closed")


class NoosaFeedTests(unittest.TestCase):
    def connectors(self):
        return [
            {
                "from_lat": "-26.373699", "from_lon": "153.038208",
                "to_lat": "-26.371931", "to_lon": "153.039689",
                "name": mod.NOOSA_CONNECTOR_NAME,
            },
        ]

    def base_payload(self):
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [145, -20]}, "properties": {"id": "existing"}}]}

    def ids(self, payload):
        return {
            str((f.get("properties") or {}).get("event_id") or (f.get("properties") or {}).get("id") or "")
            for f in payload["features"]
        }

    def test_open_with_native_fallback_adds_only_4wd_conditional_event(self):
        out, meta = mod.prepare_feed(self.base_payload(), self.connectors(), "OPEN", "2026-09-03T00:00:00+00:00", True)
        ids = self.ids(out)
        self.assertIn(mod.COOLOOLA_RESTRICTION_ID, ids)
        self.assertNotIn(mod.NOOSA_CLOSURE_ID, ids)
        self.assertFalse(meta["noosa_ferry_event_injected"])

    def test_closed_with_native_fallback_adds_impassable_ferry_and_4wd_condition(self):
        out, meta = mod.prepare_feed(self.base_payload(), self.connectors(), "CLOSED", "2026-09-03T00:00:00+00:00", True)
        ids = self.ids(out)
        self.assertIn(mod.COOLOOLA_RESTRICTION_ID, ids)
        self.assertIn(mod.NOOSA_CLOSURE_ID, ids)
        ferry = next(f for f in out["features"] if (f.get("properties") or {}).get("event_id") == mod.NOOSA_CLOSURE_ID)
        self.assertEqual(ferry["properties"]["category_norm"], "road_closed")
        self.assertEqual(ferry["properties"]["passability_norm"], "impassable")
        self.assertEqual(meta["noosa_ferry_event_mode"], "impassable_ferry_closure_with_native_4wd_fallback")

    def test_closed_without_native_fallback_uses_conditional_proxy_not_full_block(self):
        out, meta = mod.prepare_feed(self.base_payload(), self.connectors(), "CLOSED", "2026-09-03T00:00:00+00:00", False)
        ids = self.ids(out)
        self.assertIn(mod.NOOSA_CLOSURE_ID, ids)
        self.assertNotIn(mod.COOLOOLA_RESTRICTION_ID, ids)
        ferry = next(f for f in out["features"] if (f.get("properties") or {}).get("event_id") == mod.NOOSA_CLOSURE_ID)
        self.assertEqual(ferry["properties"]["category_norm"], "restricted")
        self.assertEqual(ferry["properties"]["passability_norm"], "passable_with_conditions")
        self.assertTrue(ferry["properties"]["model_proxy"])
        self.assertEqual(meta["noosa_ferry_event_mode"], "conditional_access_proxy_for_missing_4wd_graph")

    def test_unknown_fails_open(self):
        out, meta = mod.prepare_feed(self.base_payload(), self.connectors(), "UNKNOWN", "2026-09-03T00:00:00+00:00", True)
        ids = self.ids(out)
        self.assertIn(mod.COOLOOLA_RESTRICTION_ID, ids)
        self.assertNotIn(mod.NOOSA_CLOSURE_ID, ids)
        self.assertFalse(meta["noosa_ferry_event_injected"])

    def test_4wd_event_is_conditional_not_impassable(self):
        feature = mod.cooloola_conditional_feature("2026-09-03T00:00:00+00:00")
        props = feature["properties"]
        self.assertEqual(props["category_norm"], "restricted")
        self.assertEqual(props["passability_norm"], "passable_with_conditions")
        self.assertEqual(feature["geometry"]["type"], "Point")

    def test_repo_manual_connector_file_contains_only_noosa_ferry_not_long_4wd_shortcut(self):
        path = Path(__file__).with_name("manual_graph_connectors.csv")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        names = {row["name"] for row in rows}
        self.assertIn(mod.NOOSA_CONNECTOR_NAME, names)
        self.assertFalse(any("Cooloola Great Beach Drive 4WD graph connector" == name for name in names))
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
