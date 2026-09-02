import unittest

from prepare_daintree_ferry_status import parse_ferry_status, reconcile_ferry_checks


class DaintreeFerryStatusParserTests(unittest.TestCase):
    def test_current_open_ignores_historical_closed_update(self):
        html = """
        <h1>Ferry Status Updates</h1>
        <h3>FERRY OPEN</h3>
        <h2>Previous Updates</h2>
        <h4>Ferry closed for emergency repairs</h4>
        """
        self.assertEqual(parse_ferry_status(html)[0], "OPEN")

    def test_current_closed(self):
        html = """
        <h1>Ferry Status Updates</h1>
        <h3>FERRY CLOSED</h3>
        <h2>Previous Updates</h2>
        <h4>Ferry operating as usual</h4>
        """
        self.assertEqual(parse_ferry_status(html)[0], "CLOSED")

    def test_unique_global_status_is_accepted_as_fallback(self):
        html = '<div class="header-status">Status: OPEN</div><h1>Ferry information</h1>'
        self.assertEqual(parse_ferry_status(html)[0], "OPEN")

    def test_unrecognised_status_is_unknown(self):
        html = "<h3>FERRY DELAYED</h3>"
        self.assertEqual(parse_ferry_status(html)[0], "UNKNOWN")

    def test_conflicting_global_status_is_unknown(self):
        html = "<div>Status: OPEN</div><div>Status: CLOSED</div>"
        self.assertEqual(parse_ferry_status(html)[0], "UNKNOWN")


class DaintreeFerryStatusReconcileTests(unittest.TestCase):
    def check(self, status, error=""):
        return {"status": status, "error": error}

    def test_primary_open_secondary_open_is_open(self):
        self.assertEqual(reconcile_ferry_checks(self.check("OPEN"), self.check("OPEN"))[0], "OPEN")

    def test_primary_closed_secondary_closed_is_closed(self):
        self.assertEqual(reconcile_ferry_checks(self.check("CLOSED"), self.check("CLOSED"))[0], "CLOSED")

    def test_primary_unknown_never_falls_back_to_homepage_open(self):
        self.assertEqual(reconcile_ferry_checks(self.check("UNKNOWN", "unrecognised"), self.check("OPEN"))[0], "UNKNOWN")

    def test_disagreement_is_unknown(self):
        self.assertEqual(reconcile_ferry_checks(self.check("CLOSED"), self.check("OPEN"))[0], "UNKNOWN")

    def test_primary_known_secondary_unknown_keeps_primary(self):
        self.assertEqual(reconcile_ferry_checks(self.check("OPEN"), self.check("UNKNOWN"))[0], "OPEN")


if __name__ == "__main__":
    unittest.main()
