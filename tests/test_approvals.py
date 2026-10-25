from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from approvals import (  # noqa: E402
    FRIEND,
    GROUP_ADD,
    GROUP_INVITE,
    ApprovalRecord,
    ApprovalStore,
    ApprovalWorker,
    onebot_action,
)


def record(kind: str, action: str, group_id: int | None = None) -> ApprovalRecord:
    return ApprovalRecord(
        id=1,
        kind=kind,
        flag="flag",
        user_id=7,
        group_id=group_id,
        group_name="",
        requester_name="",
        comment="",
        status="executing",
        action=action,
        error="",
        created_at=0,
        updated_at=0,
        handled_at=0,
    )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmp.name) / "approvals.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_add_is_idempotent_per_flag(self) -> None:
        first, created = self.store.add(
            kind=FRIEND, flag="f1", user_id=1, comment="你好"
        )
        second, created_again = self.store.add(
            kind=FRIEND, flag="f1", user_id=1, comment="重复"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.comment, "你好")
        self.assertEqual(self.store.active_count(), 1)

    def test_queue_claim_finish(self) -> None:
        stored, _ = self.store.add(kind=FRIEND, flag="f1", user_id=1)
        queued = self.store.queue(stored.id, "approve")
        self.assertEqual((queued.status, queued.action), ("queued", "approve"))
        # Same decision is idempotent; a different one cannot change a queued row.
        self.assertEqual(self.store.queue(stored.id, "approve").status, "queued")
        with self.assertRaises(ValueError):
            self.store.queue(stored.id, "reject")

        claimed = self.store.claim()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].status, "executing")
        self.store.finish(stored.id, "approved")
        final = self.store.get(stored.id)
        assert final is not None
        self.assertEqual(final.status, "approved")
        with self.assertRaises(ValueError):
            self.store.queue(stored.id, "approve")

    def test_failed_request_can_be_retried(self) -> None:
        stored, _ = self.store.add(
            kind=GROUP_ADD, flag="g1", user_id=2, group_id=42
        )
        self.store.queue(stored.id, "reject")
        self.store.claim()
        self.store.finish(stored.id, "failed", error="napcat down")
        retried = self.store.queue(stored.id, "approve")
        self.assertEqual(
            (retried.status, retried.action, retried.error),
            ("queued", "approve", ""),
        )

    def test_reset_executing_requeues_interrupted_work(self) -> None:
        stored, _ = self.store.add(kind=FRIEND, flag="f2", user_id=3)
        self.store.queue(stored.id, "approve")
        self.store.claim()
        self.assertEqual(self.store.reset_executing(), 1)
        again = self.store.get(stored.id)
        assert again is not None
        self.assertEqual(again.status, "queued")


class ActionTests(unittest.TestCase):
    def test_friend_action(self) -> None:
        action, params = onebot_action(record(FRIEND, "approve"))
        self.assertEqual(action, "set_friend_add_request")
        self.assertEqual(params, {"flag": "flag", "approve": True})

    def test_group_invite_reject_action(self) -> None:
        action, params = onebot_action(record(GROUP_INVITE, "reject", group_id=42))
        self.assertEqual(action, "set_group_add_request")
        self.assertEqual(params["sub_type"], "invite")
        self.assertFalse(params["approve"])
        self.assertIn("reason", params)

    def test_group_add_approve_action(self) -> None:
        action, params = onebot_action(record(GROUP_ADD, "approve", group_id=42))
        self.assertEqual(action, "set_group_add_request")
        self.assertEqual(params, {"flag": "flag", "sub_type": "add", "approve": True})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmp.name) / "approvals.db")
        self.calls: list[tuple[str, dict]] = []

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    async def call(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        return {}

    async def test_executes_queued_friend_request(self) -> None:
        stored, _ = self.store.add(kind=FRIEND, flag="f1", user_id=1)
        self.store.queue(stored.id, "approve")
        worker = ApprovalWorker(self.store, self.call, check_seconds=1)
        self.assertEqual(await worker.tick(), 1)
        self.assertEqual(self.calls[0][0], "set_friend_add_request")
        final = self.store.get(stored.id)
        assert final is not None
        self.assertEqual(final.status, "approved")

    async def test_failure_is_recorded(self) -> None:
        stored, _ = self.store.add(kind=FRIEND, flag="f1", user_id=1)
        self.store.queue(stored.id, "reject")

        async def boom(_action: str, _params: dict) -> None:
            raise RuntimeError("napcat down")

        worker = ApprovalWorker(self.store, boom, check_seconds=1)
        self.assertEqual(await worker.tick(), 1)
        final = self.store.get(stored.id)
        assert final is not None
        self.assertEqual(final.status, "failed")
        self.assertIn("napcat down", final.error)

    async def test_waits_until_connected(self) -> None:
        stored, _ = self.store.add(kind=FRIEND, flag="f1", user_id=1)
        self.store.queue(stored.id, "approve")
        worker = ApprovalWorker(
            self.store, self.call, check_seconds=1, ready=lambda: False
        )
        self.assertEqual(await worker.tick(), 0)
        self.assertEqual(self.calls, [])
        current = self.store.get(stored.id)
        assert current is not None
        self.assertEqual(current.status, "queued")


if __name__ == "__main__":
    unittest.main()
