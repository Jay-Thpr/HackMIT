import copy
import unittest

from advanced.evidence import consumer_lag, correctness, replication_lag_bytes


def snapshot():
    return {"shards": [{"shard_id": i, "complete": True,
                        "ordering_violations": [], "missing_outbox": [], "orphan_payments": [],
                        "duplicate_effects": [], "tenants": [{"tenant_id": f"tenant-{i}", "accepted": 10,
                                                              "paid": 10, "fulfilled": 10,
                                                              "outbox_pending": 0, "outstanding": 0,
                                                              "mismatched_effects": 0,
                                                              "inconsistent_fulfillment": 0}]}
                       for i in range(3)]}


class EvidenceTest(unittest.TestCase):
    def test_replication_position_difference(self):
        self.assertEqual(replication_lag_bytes("1/00000010", "0/FFFFFFF0"), 32)
        self.assertEqual(replication_lag_bytes("1/A", "1/A"), 0)
        self.assertIsNone(replication_lag_bytes("1/A", "1/B"))
        self.assertIsNone(replication_lag_bytes(None, "1/B"))
        self.assertIsNone(replication_lag_bytes("invalid", "1/B"))

    def test_kafka_committed_offset_semantics(self):
        self.assertEqual(consumer_lag(20, 15), 5)
        self.assertEqual(consumer_lag(20, 20), 0)
        self.assertEqual(consumer_lag(0, None, beginning_offset=0), 0)
        self.assertIsNone(consumer_lag(20, None, beginning_offset=0))
        self.assertIsNone(consumer_lag(20, None, beginning_offset=20))
        for end, committed in [(0, None), (20, -1), (10, 20), (True, 0), (10, False)]:
            self.assertIsNone(consumer_lag(end, committed))

    def test_complete_quiescent_business_accounting(self):
        self.assertEqual(correctness(snapshot(), ["tenant-0", "tenant-1", "tenant-2"])["status"], "passed")

    def test_missing_shard_cannot_look_healthy(self):
        data = snapshot()
        data["shards"].pop()
        self.assertEqual(correctness(data, ["tenant-0"])["status"], "unknown")
        data = snapshot()
        data["shards"][1]["complete"] = False
        self.assertEqual(correctness(data, ["tenant-0"])["status"], "unknown")

    def test_failed_or_missing_checks_are_not_zero(self):
        for check in ("ordering_violations", "missing_outbox", "orphan_payments", "duplicate_effects"):
            with self.subTest(check=check):
                data = snapshot()
                del data["shards"][0][check]
                self.assertEqual(correctness(data, ["tenant-0"])["status"], "unknown")
                data["shards"][0][check] = [{"violations": 1}]
                self.assertEqual(correctness(data, ["tenant-0"])["status"], "failed")

    def test_unfinished_work_is_not_recovery(self):
        data = snapshot()
        row = data["shards"][0]["tenants"][0]
        row.update(paid=8, fulfilled=8, outstanding=2)
        self.assertEqual(correctness(data, ["tenant-0"])["status"], "failed")

    def test_unexercised_tenant_is_unknown(self):
        data = snapshot()
        row = data["shards"][0]["tenants"][0]
        row.update(accepted=0, paid=0, fulfilled=0)
        self.assertEqual(correctness(data, ["tenant-0"])["status"], "unknown")
        self.assertEqual(correctness(snapshot(), ["missing"])["status"], "unknown")

    def test_inconsistent_shard_identity_is_unknown(self):
        data = snapshot()
        data["shards"][1] = copy.deepcopy(data["shards"][0])
        self.assertEqual(correctness(data, ["tenant-0"])["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
