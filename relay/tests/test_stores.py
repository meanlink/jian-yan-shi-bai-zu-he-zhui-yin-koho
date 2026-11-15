"""relay 子系统单元测试（标准库 unittest，无网络依赖）。

运行：python -m unittest relay.tests.test_stores -v
"""
import os
import tempfile
import threading
import unittest

from relay.fingerprint import fingerprint
from relay.receiver_store import ReceiverStore
from relay.sender_store import (
    CONFLICT,
    IN_FLIGHT,
    PENDING,
    RECEIVED,
    RollbackBlocked,
    SenderStore,
)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sender = SenderStore(os.path.join(self.tmp.name, "sender.db"))
        self.receiver = ReceiverStore(os.path.join(self.tmp.name, "receiver.db"))
        self.sender.add_destination("QA", "质量枢纽", "http://x/api/receive")
        self.arc = self.sender.add_archive("简报", {"k": "v"})
        self.plan = self.sender.create_plan(self.arc["id"], "QA")

    def tearDown(self):
        self.sender.close()
        self.receiver.close()
        self.tmp.cleanup()

    def enqueue(self):
        d = self.sender.enqueue_for_plan(self.plan["id"])
        return d["id"]


class TestFingerprint(unittest.TestCase):
    def test_键顺序无关(self):
        self.assertEqual(
            fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1})
        )

    def test_内容变化指纹变化(self):
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))


class TestSenderStateMachine(StoreTestBase):
    def test_状态单调_回退被拒绝(self):
        did = self.enqueue()
        self.sender.claim_for_dispatch()  # PENDING -> IN_FLIGHT
        updated = self.sender.mark_received(did, self.arc["fingerprint"], 1.0)
        self.assertEqual(updated["status"], RECEIVED)
        # 已接收不允许回待确认
        with self.assertRaises(RollbackBlocked):
            self.sender.transition(did, PENDING)
        # 尝试记录里留下回退拦截证据
        attempts = self.sender.attempts(did)
        self.assertTrue(any(a["outcome"] == "rollback_blocked" for a in attempts))

    def test_并发认领只有一个赢家(self):
        did = self.enqueue()
        winners = []
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            row = self.sender.claim_for_dispatch()
            if row is not None:
                winners.append(row["id"])

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(winners, [did])
        self.assertEqual(self.sender.get_delivery(did)["status"], IN_FLIGHT)
        self.assertEqual(self.sender.get_delivery(did)["attempt_count"], 1)

    def test_指纹不符升级冲突且为终态(self):
        did = self.enqueue()
        self.sender.claim_for_dispatch()
        self.sender.mark_conflict(did, "f" * 64, "测试冲突")
        self.assertEqual(self.sender.get_delivery(did)["status"], CONFLICT)
        with self.assertRaises(RollbackBlocked):
            self.sender.mark_received(did, "f" * 64, 1.0)
        self.assertEqual(self.sender.get_delivery(did)["status"], CONFLICT)

    def test_晚到正确回执不回退已接收(self):
        did = self.enqueue()
        self.sender.claim_for_dispatch()
        self.sender.mark_received(did, self.arc["fingerprint"], 1.0)
        # 再来一次相同指纹回执：幂等保持 RECEIVED
        again = self.sender.mark_received(did, self.arc["fingerprint"], 2.0)
        self.assertEqual(again["status"], RECEIVED)

    def test_暂停不生成新投递_在途记录不受影响(self):
        did = self.enqueue()
        self.sender.claim_for_dispatch()  # 在途
        self.sender.set_plan_paused(self.plan["id"], True)
        self.assertIsNone(self.sender.enqueue_for_plan(self.plan["id"]))
        # 在途记录仍在，目的地快照不变
        self.assertEqual(self.sender.get_delivery(did)["status"], IN_FLIGHT)

    def test_改目的地不挪动在途记录(self):
        did = self.enqueue()
        self.sender.add_destination("DR", "灾备", "http://y/api/receive")
        self.sender.change_plan_destination(self.plan["id"], "DR")
        self.assertEqual(self.sender.get_delivery(did)["dest_code"], "QA")
        new_id = self.sender.enqueue_for_plan(self.plan["id"])
        self.assertEqual(self.sender.get_delivery(new_id["id"])["dest_code"], "DR")

    def test_旧归档原件保留_新版本另存(self):
        self.sender.add_archive("简报", {"k": "v2"})
        arcs = [a for a in self.sender.list_archives() if a["title"] == "简报"]
        self.assertEqual(len(arcs), 2)
        v1 = next(a for a in arcs if a["version"] == 1)
        v2 = next(a for a in arcs if a["version"] == 2)
        self.assertNotEqual(v1["fingerprint"], v2["fingerprint"])
        self.assertEqual(v1["superseded_by"], v2["id"])


class TestReceiverIdempotency(StoreTestBase):
    def _receive(self, payload=None, delivery_id="dlv-x"):
        payload = payload or {"k": "v"}
        return self.receiver.receive(
            delivery_id, self.arc["id"], "QA",
            fingerprint(payload), '{"k":"v"}',
        )

    def test_重复投递不重复接收(self):
        status, fact = self._receive()
        self.assertEqual(status, "accepted")
        status2, fact2 = self._receive()
        self.assertEqual(status2, "duplicate")
        self.assertEqual(fact2["delivery_id"], fact["delivery_id"])  # 同一条事实
        self.assertEqual(fact2["duplicate_count"], 1)
        self.assertEqual(len(self.receiver.list_facts()), 1)

    def test_同ID内容不符冲突且不覆盖原件(self):
        status, fact = self._receive({"k": "v"})
        self.assertEqual(status, "accepted")
        original_fp = fact["fingerprint"]
        status2, fact2 = self.receiver.receive(
            "dlv-x", self.arc["id"], "QA", fingerprint({"k": "tampered"}),
            '{"k":"tampered"}',
        )
        self.assertEqual(status2, "conflict")
        # 首次接收原件未被覆盖
        self.assertEqual(self.receiver.fact("dlv-x")["fingerprint"], original_fp)
        self.assertEqual(len(self.receiver.list_facts()), 1)

    def test_回执每次重复都签发(self):
        self._receive()
        self._receive()
        receipts = self.receiver.list_receipts("dlv-x")
        self.assertEqual(len(receipts), 2)


if __name__ == "__main__":
    unittest.main()
