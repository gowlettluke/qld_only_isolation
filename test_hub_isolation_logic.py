import tempfile
import unittest
from pathlib import Path

import networkx as nx

from qld_isolation_proper_v6 import (
    Place,
    classify_places,
    hub_component_access,
    write_outputs,
)


class HubIsolationLogicTests(unittest.TestCase):
    def setUp(self):
        # A -- T -- B.  Blocking T--B strands hub A and town T together,
        # while hub B is also left unable to reach another hub.
        self.G = nx.Graph()
        self.G.add_edge("A", "T")
        self.G.add_edge("T", "B")
        self.hubs = ["A", "B"]
        self.places = [
            Place("hub_a", "Hub A", -20.0, 145.0, is_hub=True),
            Place("town_t", "Town T", -20.1, 145.1, is_hub=False),
            Place("hub_b", "Hub B", -20.2, 145.2, is_hub=True),
        ]
        self.place_nodes = {"hub_a": "A", "town_t": "T", "hub_b": "B"}
        self.place_dist = {p.place_id: 0.0 for p in self.places}
        self.snap_strategy = {p.place_id: "nearest" for p in self.places}
        self.snap_note = {p.place_id: "" for p in self.places}

    def _classify(self, blocked_imp=None, blocked_all=None):
        blocked_imp = blocked_imp or set()
        blocked_all = blocked_all if blocked_all is not None else blocked_imp
        before_counts, _ = hub_component_access(self.G, self.hubs, set())
        imp_counts, _ = hub_component_access(self.G, self.hubs, blocked_imp)
        all_counts, _ = hub_component_access(self.G, self.hubs, blocked_all)
        rows = classify_places(
            self.places,
            self.place_nodes,
            self.place_dist,
            self.snap_strategy,
            self.snap_note,
            set(before_counts),
            set(imp_counts),
            set(all_counts),
            before_counts,
            imp_counts,
            all_counts,
            {},
            {},
            [],
            [],
        )
        return {row["place_id"]: row for row in rows}

    def test_hub_must_reach_another_hub(self):
        rows = self._classify(blocked_imp={("T", "B", 0)})
        hub_a = rows["hub_a"]
        self.assertEqual(hub_a["reachable_hubs_impassable_only"], 1)
        self.assertFalse(hub_a["hub_access_impassable_only"])
        self.assertEqual(hub_a["isolation_category"], "isolated_full_closures")
        self.assertIn("cannot reach any other modelled hub", hub_a["isolation_reason"])

    def test_town_only_reaching_stranded_hub_is_caveated_isolation(self):
        rows = self._classify(blocked_imp={("T", "B", 0)})
        town = rows["town_t"]
        self.assertEqual(town["reachable_hubs_impassable_only"], 1)
        self.assertFalse(town["hub_access_impassable_only"])
        self.assertEqual(
            town["isolation_category"],
            "isolated_via_stranded_hub_full_closures",
        )
        self.assertIn("local access to the stranded hub remains", town["isolation_reason"])

    def test_restriction_only_stranding_preserves_scenario(self):
        rows = self._classify(
            blocked_imp=set(),
            blocked_all={("T", "B", 0)},
        )
        self.assertEqual(rows["hub_a"]["isolation_category"], "isolated_with_restrictions")
        self.assertEqual(
            rows["town_t"]["isolation_category"],
            "isolated_via_stranded_hub_with_restrictions",
        )

    def test_two_reachable_hubs_is_not_isolated(self):
        rows = self._classify()
        for row in rows.values():
            self.assertTrue(row["hub_access_impassable_only"])
            self.assertEqual(row["reachable_hubs_impassable_only"], 2)
            self.assertEqual(row["isolation_category"], "not_isolated")

    def test_preexisting_single_hub_component_is_qa_not_current_closure_isolation(self):
        G = nx.Graph()
        G.add_edge("A", "T")
        before_counts, _ = hub_component_access(G, ["A"], set())
        places = [
            Place("hub_a", "Hub A", -20.0, 145.0, is_hub=True),
            Place("town_t", "Town T", -20.1, 145.1, is_hub=False),
        ]
        rows = classify_places(
            places,
            {"hub_a": "A", "town_t": "T"},
            {"hub_a": 0.0, "town_t": 0.0},
            {"hub_a": "nearest", "town_t": "nearest"},
            {"hub_a": "", "town_t": ""},
            set(before_counts),
            set(before_counts),
            set(before_counts),
            before_counts,
            before_counts,
            before_counts,
            {},
            {},
            [],
            [],
        )
        self.assertTrue(all(r["isolation_category"] == "unknown_preexisting_single_hub_component" for r in rows))

    def test_caveated_categories_are_written_to_isolated_outputs(self):
        rows = self._classify(blocked_imp={("T", "B", 0)})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = {"created_at": "test", "counts": {}}
            write_outputs(out, [], {}, list(rows.values()), [], [], summary)
            csv_text = (out / "isolated_places_qld.csv").read_text(encoding="utf-8")
            self.assertIn("isolated_via_stranded_hub_full_closures", csv_text)
            self.assertNotIn("not_isolated", csv_text)


if __name__ == "__main__":
    unittest.main()
