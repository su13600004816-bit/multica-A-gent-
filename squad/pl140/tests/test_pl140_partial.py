#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PL-140 X PARTIAL 16 项复核补齐 · 离线单测。

运行:
    cd /home/fleet/line-config && python3 -m unittest tests.test_pl140_partial

覆盖 line_partial.py 的每个补齐函数,以及 X32 旧 PASS 新鲜度失效在 line_done_gate.py
CLI 上的端到端拦截(子进程 + fixture)。test 方法名标注对应 X 编号。
"""
import os
import sys
import json
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import line_partial as P   # noqa: E402

NOW = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)


class X14Coverage(unittest.TestCase):
    def test_x14_not_truncated(self):
        r = P.coverage_estimate(30, 50)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["est_missed"], 0)

    def test_x14_truncated_lower_bound(self):
        r = P.coverage_estimate(50, 50)  # 抓满一页,无总数探测
        self.assertTrue(r["truncated"])
        self.assertEqual(r["est_missed"], 1)
        self.assertFalse(r["exact"])

    def test_x14_truncated_exact_with_total(self):
        r = P.coverage_estimate(50, 50, total_hint=137)
        self.assertTrue(r["truncated"] and r["exact"])
        self.assertEqual(r["est_missed"], 87)


class X11SchemaDrift(unittest.TestCase):
    def test_x11_evidence_record(self):
        ev = P.schema_drift_evidence(["deferred", "reopened"], ["todo", "done"], now=NOW)
        self.assertEqual(ev["drift_count"], 2)
        self.assertIn("deferred", ev["unknown_states"])

    def test_x11_writes_independent_file(self):
        d = tempfile.mkdtemp()
        ev = P.schema_drift_evidence(["deferred"], ["todo"], now=NOW)
        path = P.write_schema_drift_evidence(ev, d, now=NOW)
        self.assertTrue(path and os.path.exists(path))
        self.assertEqual(json.load(open(path))["unknown_states"], ["deferred"])

    def test_x11_no_drift_no_file(self):
        d = tempfile.mkdtemp()
        ev = P.schema_drift_evidence([], ["todo"], now=NOW)
        self.assertIsNone(P.write_schema_drift_evidence(ev, d, now=NOW))


class X38StageTransition(unittest.TestCase):
    def test_x38_diff_emits_transitions(self):
        prev = {"i1": {"stage": "development", "ident": "PL-1"}}
        new = {"i1": {"stage": "audit", "ident": "PL-1", "event": "dev_done", "source": "watchdog"},
               "i2": {"stage": "development", "ident": "PL-2"}}  # i2 首次出现,from=None→development
        tr = P.diff_stage_state(prev, new, now=NOW)
        moves = {(t["ident"], t["from"], t["to"]) for t in tr}
        self.assertIn(("PL-1", "development", "audit"), moves)
        self.assertIn(("PL-2", None, "development"), moves)
        # 所有记录都带 from/to/event/source(X38 要求)
        for t in tr:
            for k in ("from", "to", "event", "source", "ts"):
                self.assertIn(k, t)

    def test_x38_no_change_no_transition(self):
        same = {"i1": {"stage": "audit", "ident": "PL-1"}}
        self.assertEqual(P.diff_stage_state(same, same, now=NOW), [])

    def test_x38_append_jsonl_persists_and_caps(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "stage_transitions.jsonl")
        for i in range(5):
            P.append_jsonl([{"n": i}], path, keep=3)
        lines = open(path).read().splitlines()
        self.assertEqual(len(lines), 3)                 # 滚动保留最近 3
        self.assertEqual(json.loads(lines[-1])["n"], 4)


class X54RateLimit(unittest.TestCase):
    def test_x54_detect_signals(self):
        self.assertTrue(P.detect_rate_limit("Error: HTTP 429"))
        self.assertTrue(P.detect_rate_limit("", "rate limit exceeded"))
        self.assertTrue(P.detect_rate_limit("请求过于频繁,请稍后"))
        self.assertFalse(P.detect_rate_limit("ok", "done"))

    def test_x54_signal_combines_env_and_autodetect(self):
        self.assertTrue(P.rate_limited_signal({"WATCHDOG_RATE_LIMITED": "1"}, "all good"))
        self.assertTrue(P.rate_limited_signal({}, "got 429 back"))
        self.assertFalse(P.rate_limited_signal({}, "fine"))


class X67CrossValidate(unittest.TestCase):
    def test_x67_consistent_ok(self):
        st = {"checksum": "x"}  # 无 _O 时 verify_state 返回 False,这里跳过 checksum 分支
        status = {"ts": "2026-06-10T00:00:00Z", "scan_state": "full"}
        hb = {"ts": "2026-06-10T00:00:30Z", "ok": True}
        # 用一致 ts、full+ok=True:不应报"时间差""自相矛盾"
        issues = P.cross_validate(None, status, hb)
        self.assertNotIn("status 标 full 扫描但 heartbeat.ok=False,自相矛盾", issues)
        self.assertFalse(any("相差" in s for s in issues))

    def test_x67_time_skew_flagged(self):
        status = {"ts": "2026-06-10T00:00:00Z"}
        hb = {"ts": "2026-06-10T01:00:00Z", "ok": True}
        issues = P.cross_validate({"a": 1}, status, hb)  # state 无 checksum→不报损坏
        self.assertTrue(any("相差" in s for s in issues))

    def test_x67_full_but_hb_fail_contradiction(self):
        status = {"ts": "2026-06-10T00:00:00Z", "scan_state": "full"}
        hb = {"ts": "2026-06-10T00:00:10Z", "ok": False}
        issues = P.cross_validate({"a": 1}, status, hb)
        self.assertTrue(any("自相矛盾" in s for s in issues))

    def test_x67_missing_files(self):
        self.assertIn("status.json 缺失", P.cross_validate({"a": 1}, None, {"ts": "2026-06-10T00:00:00Z"}))


class X71TamperProof(unittest.TestCase):
    def test_x71_sign_and_verify(self):
        payload = {"overall": "red", "red_lights": [{"ident": "PL-1"}]}
        signed = P.sign_status(payload, "k")
        self.assertIn("sig", signed)
        self.assertTrue(P.verify_payload_signature(signed, "k"))

    def test_x71_tamper_detected(self):
        signed = P.sign_status({"overall": "red"}, "k")
        tampered = dict(signed); tampered["overall"] = "green"   # 篡改红→绿
        self.assertFalse(P.verify_payload_signature(tampered, "k"))

    def test_x71_wrong_key_rejected(self):
        signed = P.sign_status({"overall": "red"}, "k")
        self.assertFalse(P.verify_payload_signature(signed, "other"))

    def test_x71_no_key_disabled(self):
        out = P.sign_status({"overall": "red"}, "")
        self.assertNotIn("sig", out)
        self.assertFalse(P.verify_payload_signature({"overall": "red"}, ""))


class X53CoordLock(unittest.TestCase):
    def test_x53_mutual_exclusion(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "coord.lock")
        fd1 = P.acquire_coord_lock(path)
        self.assertIsNotNone(fd1)
        # 同一锁文件、独立子进程再 acquire 应拿不到(被持有)
        code = ("import sys; sys.path.insert(0,%r); import line_partial as P;"
                "fd=P.acquire_coord_lock(%r); print('GOT' if fd else 'BLOCKED')" % (ROOT, path))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn("BLOCKED", r.stdout)
        P.release_coord_lock(fd1)
        # 释放后子进程可再拿到
        r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn("GOT", r2.stdout)


class X08AssigneeToctou(unittest.TestCase):
    def test_x08_unchanged(self):
        self.assertTrue(P.assignee_unchanged("a", "a"))
        self.assertFalse(P.assignee_unchanged("a", "b"))

    def test_x08_precheck_blocks_changed(self):
        ok, why = P.route_precheck("squad-A", lambda i: {"assignee_id": "personal-X"}, "i1")
        self.assertFalse(ok)
        self.assertIn("跳过本轮 route", why)

    def test_x08_precheck_allows_same(self):
        ok, _ = P.route_precheck("squad-A", lambda i: {"assignee_id": "squad-A"}, "i1")
        self.assertTrue(ok)

    def test_x08_precheck_fail_open_on_fetch_error(self):
        def boom(i): raise RuntimeError("net down")
        ok, why = P.route_precheck("squad-A", boom, "i1")
        self.assertTrue(ok)            # 拉取失败保守放行,不漏掉真异常续派
        self.assertIn("保守放行", why)


class X2427MemberHeadcount(unittest.TestCase):
    def test_x2427_counts(self):
        members = {"线小队-T01": [
            {"id": "m1", "name": "leader", "role": "leader"},
            {"id": "m2", "name": "dev", "role": "member"},
            {"id": "m3", "name": "idle", "role": "member"},
        ]}
        items = [{"assignee_id": "m1", "status": "in_progress"},
                 {"assignee_id": "m2", "status": "in_review"},
                 {"assignee_id": "m3", "status": "done"}]   # m3 名下无活跃 → 闲置
        alerts = [{"squad": "m2", "kind": "failed"}]        # m2 卡死
        hc = P.member_headcount(members, items, alerts)["线小队-T01"]
        self.assertEqual(hc["headcount"], 3)
        self.assertEqual(hc["working"], 2)                  # m1+m2
        self.assertEqual(hc["idle"], 1)                     # m3
        self.assertEqual(hc["stuck"], 1)                    # m2
        self.assertEqual(hc["by_role"], {"leader": 1, "member": 2})


class X63HeartbeatAlert(unittest.TestCase):
    def test_x63_stale_alerts(self):
        hb = {"last_successful_scan": "2026-06-09T23:00:00Z"}   # 60 分钟前
        al = P.heartbeat_alert(hb, NOW, threshold_min=10)
        self.assertIsNotNone(al)
        self.assertEqual(al["severity"], "critical")
        self.assertEqual(al["kind"], "watchdog_silent")

    def test_x63_fresh_no_alert(self):
        hb = {"last_successful_scan": "2026-06-09T23:58:00Z"}   # 2 分钟前
        self.assertIsNone(P.heartbeat_alert(hb, NOW, threshold_min=10))

    def test_x63_no_heartbeat_alerts(self):
        self.assertIsNotNone(P.heartbeat_alert(None, NOW, threshold_min=10))


class X72P0Ack(unittest.TestCase):
    def test_x72_pending_filters_critical_unacked(self):
        alerts = [{"ident": "PL-1", "kind": "failed"},        # critical
                  {"ident": "PL-2", "kind": "todo_no_claim"}]  # low
        pend = P.pending_p0_acks(alerts)
        self.assertEqual([a["ident"] for a in pend], ["PL-1"])

    def test_x72_acked_excluded(self):
        alerts = [{"ident": "PL-1", "kind": "failed"}]
        self.assertEqual(P.pending_p0_acks(alerts, {"PL-1|failed"}), [])

    def test_x72_ack_entry_shape(self):
        e = P.p0_ack_entry({"ident": "PL-1", "kind": "failed", "why": "x"}, now=NOW)
        self.assertFalse(e["acked"])
        self.assertEqual(e["severity"], "critical")


class X32DoneGateFreshness(unittest.TestCase):
    """X32:gate FAIL / 旧 PASS 之后又出现 failed run → done_gate 必须 BLOCK(旧 PASS 失效)。
    端到端走 line_done_gate.py CLI(fixture 注入 issue/comments/runs)。"""
    def _write(self, d, name, obj):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def _run(self, issue, comments, runs):
        d = tempfile.mkdtemp()
        env = dict(os.environ)
        env["DONE_GATE_ISSUE_FIXTURE"] = self._write(d, "issue.json", issue)
        env["DONE_GATE_COMMENTS_FIXTURE"] = self._write(d, "comments.json", comments)
        env["DONE_GATE_RUNS_FIXTURE"] = self._write(d, "runs.json", runs)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "line_done_gate.py"),
                            "PL-X32", "--json"], cwd=ROOT, capture_output=True, text=True, env=env)
        return r.returncode, (r.stdout + r.stderr)

    def _full_pass_fixtures(self):
        # 一个本应 ALLOW 的非页面任务:有交付 + 审计 PASS + 真门禁 PASS,无用户反例。
        issue = {"identifier": "PL-X32", "title": "线小队-T01 纯逻辑任务", "status": "in_review",
                 "description": "代码补齐", "metadata": {}}
        comments = [
            {"author_type": "agent", "created_at": "2026-06-10T00:00:00Z",
             "content": "开发交付:已 push,typecheck 通过。"},
            {"author_type": "agent", "created_at": "2026-06-10T00:05:00Z",
             "content": "专属审计:复核齐全。VERDICT: PASS"},
            {"author_type": "agent", "created_at": "2026-06-10T00:06:00Z",
             "content": "【门禁 Qwen】结论。VERDICT: PASS"},
        ]
        return issue, comments

    def test_x32_fresh_pass_allows(self):
        issue, comments = self._full_pass_fixtures()
        # 证据 PASS(00:06)之后只有更早/无更新关键 run → 不失效
        runs = [{"id": "r1", "status": "completed", "created_at": "2026-06-10T00:04:00Z"}]
        code, out = self._run(issue, comments, runs)
        self.assertEqual(code, 0, out)               # ALLOW
        self.assertIn('"evidence_fresh"', out)

    def test_x32_newer_failed_run_blocks(self):
        issue, comments = self._full_pass_fixtures()
        # PASS(00:06)之后又出现 failed run(00:10)→ 旧 PASS 失效 → BLOCK
        runs = [{"id": "r2", "status": "failed", "created_at": "2026-06-10T00:10:00Z"}]
        code, out = self._run(issue, comments, runs)
        self.assertEqual(code, 2, out)               # BLOCK
        self.assertIn("evidence_fresh", out)
        self.assertIn("失效", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
