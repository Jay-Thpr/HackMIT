"""Unit tests for the clone lease lifecycle in services.lab.app.

Every Docker / HTTP seam (lab.run, lab.http, Clone.start/verify_ready/reset/teardown/
apply/undo) is stubbed, so no container or network is touched. lab.time.monotonic is
replaced with a manual clock so lease expiry is deterministic.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.lab.app as lab
from fastapi import HTTPException
from faultline_contracts.clone import CloneSpec, CloneStatus, LabActionHandle, LabActionRequest, WorkloadSpec
from faultline_contracts.common import utcnow
from faultline_contracts.levers import ActionStatus


def _spec(name="c"):
    return CloneSpec(name=name)


def _body(resp) -> dict:
    return json.loads(resp.body)


def _due_handle(clone_id: str, action_id: str = "a1") -> LabActionHandle:
    h = LabActionHandle(action_id=action_id, clone_id=clone_id, action="db_latency",
                        params={"extra_ms": 1}, ttl_s=1)
    return h.model_copy(update={"applied_at": utcnow() - timedelta(seconds=10)})


class LabLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._real_run = lab.run
        self.now = [10_000.0]
        fatal_http = SimpleNamespace(
            get=AsyncMock(side_effect=AssertionError("http get invoked")),
            post=AsyncMock(side_effect=AssertionError("http post invoked")),
            delete=AsyncMock(side_effect=AssertionError("http delete invoked")),
        )
        patchers = [
            patch.dict(lab.clones),
            patch.object(lab, "_seq", 0),
            patch.object(lab, "_create_lock", asyncio.Lock()),
            patch.object(lab, "MAX_CLONES_CFG", 2),
            patch.object(lab, "MAX_LIFETIME_S", 3600.0),
            patch.object(lab, "time", SimpleNamespace(monotonic=lambda: self.now[0])),
            patch.object(lab, "run", AsyncMock(side_effect=AssertionError("docker invoked"))),
            patch.object(lab, "http", fatal_http),
            patch.object(lab.Clone, "start", AsyncMock()),
            patch.object(lab.Clone, "verify_ready", AsyncMock()),
            patch.object(lab.Clone, "reset", AsyncMock()),
            patch.object(lab.Clone, "teardown", AsyncMock()),
            patch.object(lab.Clone, "apply", AsyncMock()),
            patch.object(lab.Clone, "undo", AsyncMock()),
            patch.object(lab.Clone, "apply_workload", AsyncMock()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        lab.clones.clear()

    def _only_clone(self) -> lab.Clone:
        return next(iter(lab.clones.values()))

    def _age(self, seconds: float) -> None:
        self.now[0] += seconds

    async def test_create_admits_up_to_cap_then_409_and_frees_slot_after_destroy(self):
        a = _body(await lab.create_clone(_spec("a")))
        b = _body(await lab.create_clone(_spec("b")))
        self.assertEqual(a["status"], "ready")
        self.assertEqual(b["status"], "ready")
        slots = {c.slot for c in lab.clones.values()}
        self.assertEqual(slots, {1, 2})
        with self.assertRaises(HTTPException) as ctx:
            await lab.create_clone(_spec("x"))
        self.assertEqual(ctx.exception.status_code, 409)

        await lab.destroy_clone(a["clone_id"])
        self.assertEqual(len(lab._alive()), 1)
        c = _body(await lab.create_clone(_spec("x")))
        self.assertEqual(c["status"], "ready")
        self.assertEqual(lab.clones[c["clone_id"]].slot, 1)

    async def test_failed_create_quarantines_slot_until_cleanup_succeeds(self):
        lab.Clone.start.side_effect = lab.LabFailure("build failed")
        lab.Clone.teardown.side_effect = lab.LabFailure("compose down failed")
        with self.assertRaises(lab.LabFailure):
            await lab.create_clone(_spec("bad"))
        lab.Clone.start.side_effect = None

        c = self._only_clone()
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertIn("cleanup pending", c.detail)
        self.assertEqual(len(lab._alive()), 1)

        ok = _body(await lab.create_clone(_spec("ok")))
        self.assertEqual(ok["status"], "ready")
        with self.assertRaises(HTTPException) as ctx:
            await lab.create_clone(_spec("nope"))
        self.assertEqual(ctx.exception.status_code, 409)

        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertEqual(lab.Clone.teardown.await_count, 1)

        self._age(31)
        lab.Clone.teardown.side_effect = None
        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.destroyed)
        self.assertEqual(len(lab._alive()), 1)
        third = _body(await lab.create_clone(_spec("third")))
        self.assertEqual(third["status"], "ready")

    async def test_manual_destroy_failure_quarantines_and_retries_after_30s(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]
        lab.Clone.teardown.side_effect = lab.LabFailure("compose down failed")

        with self.assertRaises(lab.LabFailure):
            await lab.destroy_clone(c.clone_id)
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertGreater(c.cleanup_retry_at, self.now[0])
        self.assertIn(c, lab._alive())

        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertEqual(lab.Clone.teardown.await_count, 1)

        self._age(30)
        lab.Clone.teardown.side_effect = None
        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.destroyed)

    async def test_canceled_destroy_quarantines_clone_for_lease_retry(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hanging_teardown():
            entered.set()
            await release.wait()

        c.teardown = hanging_teardown
        task = asyncio.create_task(lab.destroy_clone(c.clone_id))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(c.status, CloneStatus.failed)
        self.assertIn(c, lab._alive())
        for call in (
            lambda: lab.apply_action(c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=1)),
            lambda: lab.reset_clone(c.clone_id),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await call()
            self.assertEqual(ctx.exception.status_code, 409)

        release.set()
        c.teardown = AsyncMock()
        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.destroyed)

    async def test_expiry_reaps_clone_and_marks_handles_undone_only_after_teardown(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]
        handle = _body(await lab.apply_action(
            c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 100}, ttl_s=30)))
        self.assertEqual(handle["status"], "active")

        self._age(3600)
        self.assertEqual(c.remaining_s, 0.0)
        with self.assertRaises(HTTPException) as ctx:
            await lab.apply_action(c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=1))
        self.assertEqual(ctx.exception.status_code, 409)

        lab.Clone.teardown.side_effect = lab.LabFailure("down failed")
        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertEqual(lab.clones[c.clone_id].actions[handle["action_id"]].status, ActionStatus.active)

        self._age(30)
        lab.Clone.teardown.side_effect = None
        await lab._reap_clone(c)
        self.assertEqual(c.status, CloneStatus.destroyed)
        self.assertEqual(c.detail, "clone lifetime expired")
        self.assertEqual(c.actions[handle["action_id"]].status, ActionStatus.undone)

    async def test_reset_and_workload_do_not_renew_lease_and_failed_reset_quarantines(self):
        a = _body(await lab.create_clone(_spec("a")))
        b = _body(await lab.create_clone(_spec("b")))
        ca, cb = lab.clones[a["clone_id"]], lab.clones[b["clone_id"]]
        deadline, expires = cb.deadline, cb.expires_at

        await lab.reset_clone(cb.clone_id)
        self.assertEqual(cb.status, CloneStatus.ready)
        self.assertEqual(cb.deadline, deadline)
        self.assertEqual(cb.expires_at, expires)
        await lab.set_workload(cb.clone_id, WorkloadSpec(rps=60))
        self.assertEqual(cb.deadline, deadline)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_reset():
            entered.set()
            await release.wait()
            raise lab.LabFailure("db never drained")

        lab.Clone.reset.side_effect = blocked_reset
        reset_task = asyncio.create_task(lab.reset_clone(cb.clone_id))
        await entered.wait()
        with self.assertRaises(HTTPException) as ctx:
            await lab.create_clone(_spec("x"))
        self.assertEqual(ctx.exception.status_code, 409)

        release.set()
        with self.assertRaises(lab.LabFailure):
            await reset_task
        self.assertEqual(cb.status, CloneStatus.failed)

        with self.assertRaises(HTTPException) as ctx:
            await lab.apply_action(cb.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=1))
        self.assertEqual(ctx.exception.status_code, 409)

        await lab._reap_clone(cb)
        self.assertEqual(cb.status, CloneStatus.destroyed)
        self.assertEqual(len(lab._alive()), 1)

    async def test_create_and_reset_are_bounded_by_remaining_lease(self):
        hang = asyncio.Event()

        async def hang_start():
            await hang.wait()

        lab.Clone.start.side_effect = hang_start
        with patch.object(lab, "MAX_LIFETIME_S", 0.05):
            with self.assertRaises(lab.LabFailure) as ctx:
                await lab.create_clone(_spec("slow"))
        self.assertIn("lifetime expired during provisioning", str(ctx.exception))
        self.assertEqual(self._only_clone().status, CloneStatus.destroyed)

        task = asyncio.create_task(lab.create_clone(_spec("canceled")))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        c = list(lab.clones.values())[-1]
        self.assertEqual(c.status, CloneStatus.failed)
        self.assertEqual(c.detail, "clone provisioning cancelled")

        lab.Clone.start.side_effect = None
        resp = _body(await lab.create_clone(_spec("ok")))
        c = lab.clones[resp["clone_id"]]
        c.deadline = self.now[0] + 0.05
        lab.Clone.reset.side_effect = hang_start
        with self.assertRaises(lab.LabFailure) as ctx:
            await lab.reset_clone(c.clone_id)
        self.assertIn("lifetime expired during reset", str(ctx.exception))
        self.assertEqual(c.status, CloneStatus.failed)

    async def test_run_kills_and_reaps_process_group_on_timeout_and_cancellation(self):
        real_run = self._real_run
        gate = asyncio.Event()

        def fake_proc():
            calls = {"n": 0}

            async def communicate():
                calls["n"] += 1
                if calls["n"] == 1:
                    await gate.wait()
                return b"", b""

            return SimpleNamespace(communicate=communicate, kill=Mock(), returncode=0, calls=calls, pid=12345)

        proc = fake_proc()
        spawn = AsyncMock(return_value=proc)
        with patch("asyncio.create_subprocess_exec", spawn), \
             patch.object(lab.os, "killpg", Mock()) as killpg:
            with self.assertRaises(lab.LabFailure) as ctx:
                await real_run("docker", "compose", "ps", timeout_s=0.05)
        self.assertIn("timed out", str(ctx.exception))
        killpg.assert_called_once_with(12345, signal.SIGKILL)
        proc.kill.assert_not_called()
        self.assertEqual(proc.calls["n"], 2)
        self.assertIs(spawn.await_args.kwargs["start_new_session"], os.name == "posix")

        proc2 = fake_proc()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc2)), \
             patch.object(lab.os, "killpg", Mock()) as killpg2:
            task = asyncio.create_task(real_run("docker", "compose", "ps", timeout_s=60))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        killpg2.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(proc2.calls["n"], 2)

        proc3 = SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), kill=Mock(), pid=12345)
        with patch.object(lab, "os", SimpleNamespace(name="nt")):
            await lab._terminate_process(proc3)
        proc3.kill.assert_called_once()
        proc3.communicate.assert_awaited_once()

    async def test_action_ttl_over_remaining_and_expired_clone_rejections(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]
        c.deadline = self.now[0] + 30
        with self.assertRaises(HTTPException) as ctx:
            await lab.apply_action(c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=31))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("remaining clone lifetime", ctx.exception.detail)
        self.assertEqual(c.actions, {})
        lab.Clone.apply.assert_not_awaited()

        c.deadline = self.now[0]
        for call in (
            lambda: lab.apply_action(c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=1)),
            lambda: lab.set_workload(c.clone_id, WorkloadSpec(rps=50)),
            lambda: lab.reset_clone(c.clone_id),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await call()
            self.assertEqual(ctx.exception.status_code, 409)

    async def test_waiting_request_rechecks_readiness_after_lock(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]

        async with c.lock:
            waiter = asyncio.create_task(lab.apply_action(
                c.clone_id, LabActionRequest(action="db_latency", params={"extra_ms": 1}, ttl_s=5)))
            await asyncio.sleep(0)
            c.status = CloneStatus.destroyed
        with self.assertRaises(HTTPException) as ctx:
            await waiter
        self.assertEqual(ctx.exception.status_code, 409)
        lab.Clone.apply.assert_not_awaited()

        w = asyncio.create_task(lab.set_workload(c.clone_id, WorkloadSpec(rps=50)))
        with self.assertRaises(HTTPException):
            await w
        lab.Clone.apply_workload.assert_not_awaited()

    async def test_expire_actions_skips_busy_clone_and_retries_next_tick(self):
        a = _body(await lab.create_clone(_spec("a")))
        b = _body(await lab.create_clone(_spec("b")))
        ca, cb = lab.clones[a["clone_id"]], lab.clones[b["clone_id"]]
        ca.actions["a1"] = _due_handle(ca.clone_id)
        cb.actions["a1"] = _due_handle(cb.clone_id)

        async with ca.lock:
            await lab._expire_actions(ca)
            lab.Clone.undo.assert_not_awaited()
            await lab._expire_actions(cb)
            lab.Clone.undo.assert_awaited_once()
            self.assertIs(lab.Clone.undo.await_args[0][0], cb.actions["a1"])
            self.assertEqual(ca.actions["a1"].status, ActionStatus.active)

        await lab._expire_actions(ca)
        self.assertEqual(lab.Clone.undo.await_count, 2)
        self.assertIs(lab.Clone.undo.await_args[0][0], ca.actions["a1"])

    async def test_expiry_rechecks_state_and_retired_clone_destroy_is_idempotent(self):
        resp = _body(await lab.create_clone(_spec("a")))
        c = lab.clones[resp["clone_id"]]
        c.actions["a1"] = _due_handle(c.clone_id)
        await lab._expire_actions(c)
        lab.Clone.undo.assert_awaited_once()
        self.assertEqual(lab.Clone.undo.await_args[0][1], ActionStatus.expired)

        await lab.destroy_clone(c.clone_id)
        lab.Clone.undo.reset_mock()
        c.actions["a2"] = _due_handle(c.clone_id, "a2")
        await lab._expire_actions(c)
        lab.Clone.undo.assert_not_awaited()
        self.assertEqual(c.actions["a2"].status, ActionStatus.active)

        self.assertEqual(lab.Clone.teardown.await_count, 1)
        await lab.destroy_clone(c.clone_id)
        self.assertEqual(lab.Clone.teardown.await_count, 1)

        new = _body(await lab.create_clone(_spec("b")))
        cb = lab.clones[new["clone_id"]]
        done = LabActionHandle(action_id="a1", clone_id=cb.clone_id, action="db_latency",
                               params={"extra_ms": 1}, ttl_s=5)
        done.status = ActionStatus.undone
        cb.actions["a1"] = done
        out = _body(await lab.undo_action(cb.clone_id, "a1"))
        self.assertEqual(out["status"], "undone")
        lab.Clone.undo.assert_not_awaited()

        live = LabActionHandle(action_id="a3", clone_id=cb.clone_id, action="db_latency",
                               params={"extra_ms": 1}, ttl_s=5)
        cb.actions["a3"] = live
        cb.status = CloneStatus.failed
        with self.assertRaises(HTTPException) as ctx:
            await lab.undo_action(cb.clone_id, "a3")
        self.assertEqual(ctx.exception.status_code, 409)
        lab.Clone.undo.assert_not_awaited()

    async def test_healthz_reports_lease_and_cleanup_state(self):
        ok = _body(await lab.create_clone(_spec("ok")))
        bad = _body(await lab.create_clone(_spec("bad")))
        cb = lab.clones[bad["clone_id"]]
        cb.status = CloneStatus.failed
        cb.detail = "cleanup pending: down failed"

        hz = await lab.healthz()
        self.assertTrue(hz["ok"])
        self.assertEqual(hz["max_clones"], 2)
        self.assertEqual(hz["clone_max_lifetime_s"], 3600.0)
        self.assertEqual(hz["clones_alive"], 2)
        by_id = {c["clone_id"]: c for c in hz["clones"]}
        self.assertEqual(set(by_id), {ok["clone_id"], cb.clone_id})
        self.assertFalse(by_id[ok["clone_id"]]["cleanup_pending"])
        self.assertTrue(by_id[cb.clone_id]["cleanup_pending"])
        self.assertGreater(by_id[cb.clone_id]["remaining_s"], 0)
        self.assertIn("expires_at", by_id[cb.clone_id])

    async def test_lifespan_blocked_cleanup_never_starves_expiry_or_other_reaps(self):
        with patch.object(lab, "_sweep_orphans", AsyncMock()) as sweep, \
             patch.object(lab, "MAX_CLONES_CFG", 3):
            async with lab.lifespan(lab.app):
                sweep.assert_awaited_once()
                a = _body(await lab.create_clone(_spec("a")))
                b = _body(await lab.create_clone(_spec("b")))
                c = _body(await lab.create_clone(_spec("c")))
                ca, cb, cc = (lab.clones[x["clone_id"]] for x in (a, b, c))

                hang = asyncio.Event()
                entered = asyncio.Event()

                async def hang_teardown():
                    entered.set()
                    await hang.wait()

                ca.teardown = hang_teardown
                ca.deadline = 0.0
                cb.deadline = 0.0
                reap_ca = asyncio.create_task(lab._reap_clone(ca))
                await entered.wait()
                self.assertTrue(ca.lock.locked())
                self.assertEqual(ca.status, CloneStatus.failed)

                lab.Clone.undo.reset_mock()
                ca.actions["a1"] = _due_handle(ca.clone_id)
                cc_handle = _due_handle(cc.clone_id)
                cc.actions["a1"] = cc_handle

                async def settled() -> bool:
                    return (
                        cb.status == CloneStatus.destroyed
                        and lab.Clone.undo.await_count == 1
                        and lab.Clone.undo.await_args[0][0] is cc_handle
                    )

                for _ in range(60):
                    if await settled():
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(await settled())
                self.assertEqual(ca.status, CloneStatus.failed)
                self.assertEqual(ca.actions["a1"].status, ActionStatus.active)
                hang.set()
                await reap_ca


class LabConfigTest(unittest.TestCase):
    def test_invalid_capacity_and_lifetime_values_fail_at_import(self):
        sandbox = Path(__file__).resolve().parents[1]
        base_env = {**os.environ, "LAB_MAX_CLONES": "2", "LAB_CLONE_MAX_LIFETIME_S": "3600"}
        cases = [
            {"LAB_MAX_CLONES": "0"}, {"LAB_MAX_CLONES": "4"},
            {"LAB_CLONE_MAX_LIFETIME_S": "0"}, {"LAB_CLONE_MAX_LIFETIME_S": "-5"},
            {"LAB_CLONE_MAX_LIFETIME_S": "nan"}, {"LAB_CLONE_MAX_LIFETIME_S": "inf"},
        ]
        for override in cases:
            env = {**base_env, **override}
            result = subprocess.run(
                [sys.executable, "-c", "import services.lab.app"],
                cwd=sandbox, env=env, capture_output=True, text=True, timeout=10,
            )
            with self.subTest(**override):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("LAB_", result.stderr)


if __name__ == "__main__":
    unittest.main()
