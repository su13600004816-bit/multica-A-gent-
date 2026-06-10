#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线机制 BOM 漏洞补齐 · 离线回归测试(纯单测,不依赖真实平台写操作)。

运行:
    python3 -m unittest tests.test_line_mechanism      # 在 /home/fleet/line-config 下
    或   python3 tests/test_line_mechanism.py

覆盖 PL-104/PL-105 BOM 对照表(每个 test 方法名标注对应 BOM):
  BOM-1 状态机          : test_bom1_*
  BOM-2 run 结果        : test_bom2_*(含 PL-89 run f70159f8 历史样本)
  BOM-3 阶段推进        : test_bom3_*(PL-89/PL-91 两类断点)
  BOM-4 页面 evidence   : test_bom4_*
  BOM-5 门禁误触发      : test_bom5_*
  BOM-6 收口/reset 阻断 : test_bom6_*(done_gate + line_reset CLI 子进程)
  BOM-7 告警去重/连续取消: test_bom7_*
  BOM-8 调度职责边界    : test_bom8_*
全部断言通过即证明:上述异常在脚本里能被识别,且证据不全时无法 done/reset。
"""
import os
import sys
import json
import unittest
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import line_states as S          # noqa: E402
import line_watchdog as W        # noqa: E402
import line_done_gate as G       # noqa: E402
import line_evidence as E        # noqa: E402  (B段:证据/门禁/路由闭环)
import line_observe as O         # noqa: E402  (C段:并发/持久化/可观测)

FX_NOW = "2026-06-09T04:28:00Z"   # 真实看门狗误报 "WATCHDOG OK" 的那一刻
PL89_ID = "8bcfaeb5-8b2a-45e1-983a-2e79d319f6b5"
PL91_ID = "2b2fc294-f96a-4e38-a8de-a33e2ce214f8"
F70 = "f70159f8-4635-492b-89cc-434d9fd1d1b1"   # PL-89 被取消、result=null 的样本 run


def _load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


class BOM1StateMachine(unittest.TestCase):
    """BOM-1:统一状态机必须集中枚举,且 cancelled/failed/missing 阻断收口。"""

    def test_bom1_all_required_states_enumerated(self):
        required = {
            "development_started", "development_completed",
            "development_cancelled", "development_failed",
            "audit_started", "audit_pass", "audit_fail", "audit_cancelled",
            "gate_started", "gate_pass", "gate_fail",
            "gate_misrouted", "gate_cancelled",
            "page_evidence_required", "page_evidence_missing",
            "page_evidence_pass", "page_evidence_fail",
            "done_real_allowed", "done_real_blocked",
        }
        self.assertTrue(required.issubset(S.ALL_STATES),
                        "缺状态: %s" % (required - S.ALL_STATES))

    def test_bom1_blocking_states_block_done(self):
        for st in ("development_cancelled", "development_failed",
                   "audit_fail", "gate_misrouted",
                   "page_evidence_missing", "done_real_blocked"):
            self.assertTrue(S.is_blocking(st), "%s 应阻断收口" % st)
        self.assertFalse(S.is_blocking("development_completed"))


class BOM2RunResult(unittest.TestCase):
    """BOM-2:cancelled+result=null 与 completed 缺证据 都必须被判异常。"""

    def test_bom2_pl89_f70159f8_is_cancelled(self):
        # PL-89 的历史断点样本:取消且 result=null
        run = {"id": F70, "status": "cancelled", "result": None}
        self.assertEqual(S.classify_run(run), "development_cancelled")

    def test_bom2_completed_without_evidence_is_invalid(self):
        self.assertTrue(S.is_invalid_completion(
            {"status": "completed", "result": ""}))
        self.assertTrue(S.is_invalid_completion(
            {"status": "completed", "result": "干完了,收工"}))   # 无 VERDICT/PR/URL/截图
        self.assertFalse(S.is_invalid_completion(
            {"status": "completed", "result": "PR #1 https://x/pull/1 VERDICT: PASS"}))

    def test_bom2_completed_with_evidence_classified_completed(self):
        run = {"status": "completed",
               "result": "VERDICT: PASS, PR #2 https://x/y/pull/2"}
        self.assertEqual(S.classify_run(run), "development_completed")

    def test_bom2_consecutive_cancelled_counts(self):
        runs = [
            {"status": "cancelled", "result": None, "created_at": "2026-06-09T04:00Z"},
            {"status": "cancelled", "result": None, "created_at": "2026-06-09T04:10Z"},
        ]
        self.assertEqual(S.consecutive_cancelled(runs), 2)


class BOM3StageAdvance(unittest.TestCase):
    """BOM-3:PL-89(in_progress)与 PL-91(in_review)两类断点必须被 watchdog 识别。"""

    def setUp(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW
        issues = _load("pl89_issues.json")
        runs = _load("pl89_runs.json")
        comments = _load("pl89_comments.json")
        self._items = issues
        # 直接注入,绕开真实 multica 调用
        W.fetch_runs = lambda iid: runs.get(iid, [])
        W.last_comment_age = lambda iid: W.age_min(comments.get(iid))
        self._alerts = W.detect(issues, zombie_min=40.0, stale_min=5.0)

    def _kinds_for(self, ident):
        return {a["kind"] for a in self._alerts if a["ident"] == ident}

    def test_bom3_pl89_cancelled_and_stale_detected(self):
        k = self._kinds_for("PL-89")
        self.assertIn("cancelled", k, "PL-89 最新 run 取消未被识别")
        self.assertIn("stage_stale", k, "PL-89 阶段卡死未被识别")

    def test_bom3_pl91_cancelled_and_stale_detected(self):
        k = self._kinds_for("PL-91")
        self.assertIn("cancelled", k, "PL-91 最新 run 取消未被识别")
        self.assertIn("stage_stale", k, "PL-91 in_review 卡死未被识别")

    def test_bom3_no_false_watchdog_ok(self):
        # 历史事故:此刻看门狗误报 "WATCHDOG OK"。现在必须有告警。
        self.assertTrue(self._alerts, "回归样本时刻不应为空告警(防 false OK)")


class BOM3StageDeadline(unittest.TestCase):
    """BOM-3(返工补点):正常阶段完成后,下一环节超 deadline 不推进也必须告警。

    覆盖审计 FAIL 阻断点的四条铁律:
      dev_done   → in_review 无审计 run
      audit_pass → 审计 PASS 后无 gate
      gate_pass  → gate PASS 后无 done/reset
      gate_fail  → gate/页面视觉 FAIL 后无返工 run
    并验证对照组(下一环节已启动)不误报。
    """

    def setUp(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW   # 04:28:00Z,deadline=5 分钟
        issues = _load("stage_progress_issues.json")
        runs = _load("stage_progress_runs.json")
        comments = _load("stage_progress_comments.json")
        self._items = issues
        W.fetch_runs = lambda iid: runs.get(iid, [])
        W.last_comment_age = lambda iid: W.age_min(comments.get(iid))
        self._alerts = W.detect(issues, zombie_min=40.0, stale_min=5.0)

    def _why_for(self, ident):
        return " ".join(a["why"] for a in self._alerts
                        if a["ident"] == ident and a["kind"] == "stage_stale")

    def test_bom3d_dev_done_without_audit(self):
        why = self._why_for("PL-91")
        self.assertTrue(why, "dev_done 后无审计 run 未告警")
        self.assertIn("审计", why)

    def test_bom3d_audit_pass_without_gate(self):
        why = self._why_for("PL-91G")
        self.assertTrue(why, "审计 PASS 后无 gate 未告警")
        self.assertIn("门禁", why)

    def test_bom3d_gate_pass_without_done(self):
        why = self._why_for("PL-89D")
        self.assertTrue(why, "门禁 PASS 后无 done/reset 未告警")
        self.assertIn("收口", why)

    def test_bom3d_gate_fail_without_rework(self):
        why = self._why_for("PL-89R")
        self.assertTrue(why, "门禁/视觉 FAIL 后无返工 run 未告警")
        self.assertIn("返工", why)

    def test_bom3d_healthy_pipeline_not_flagged(self):
        # 对照组:审计 PASS 后 gate run 已启动 → 不应有任何告警
        flagged = [a for a in self._alerts if a["ident"] == "PL-89H"]
        self.assertEqual(flagged, [], "推进正常的流水线被误报: %s" % flagged)


class BOM3StageDeadlinePure(unittest.TestCase):
    """BOM-3 deadline 纯函数:classify_event 分类 + 临界点(DeepSeek 要求各 if 单测)。"""

    def test_classify_event_maps_each_phase(self):
        self.assertEqual(S.classify_event("开发交付完成,PR #3 https://x/pull/3"), "dev_done")
        self.assertEqual(S.classify_event("专属审计 VERDICT: PASS"), "audit_pass")
        self.assertEqual(S.classify_event("专属审计结论 VERDICT: FAIL"), "audit_fail")
        self.assertEqual(S.classify_event("【门禁 Qwen】真机可见 VERDICT: PASS"), "gate_pass")
        self.assertEqual(S.classify_event("【门禁 Qwen】真机未见 VERDICT: FAIL"), "gate_fail")
        self.assertEqual(S.classify_event("占位岗不能冒充千问,VERDICT: PASS"), "gate_misrouted")
        self.assertIsNone(S.classify_event(""))

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 6, 9, 4, 28, 0, tzinfo=timezone.utc)

    def test_overdue_boundary(self):
        now = self._now()
        # 正好 5 分钟(临界点)→ 不报;5 分钟+1 秒 → 报。
        at_5min = "2026-06-09T04:23:00Z"
        at_5min1s = "2026-06-09T04:22:59Z"
        self.assertIsNone(S.stage_progress_overdue("dev_done", at_5min, False, now, 5.0))
        res = S.stage_progress_overdue("dev_done", at_5min1s, False, now, 5.0)
        self.assertIsNotNone(res)
        self.assertEqual(res["next"], "审计")

    def test_overdue_suppressed_when_next_run_exists(self):
        now = self._now()
        old = "2026-06-09T04:10:00Z"   # 18 分钟前,早超 deadline
        # 但下一环节已有新 run → 不报
        self.assertIsNone(
            S.stage_progress_overdue("audit_pass", old, True, now, 5.0))

    def test_gate_pass_done_not_flagged(self):
        now = self._now()
        old = "2026-06-09T04:10:00Z"
        # issue 已 done → gate_pass 不再追问收口
        self.assertIsNone(
            S.stage_progress_overdue("gate_pass", old, False, now, 5.0, issue_done=True))
        self.assertIsNotNone(
            S.stage_progress_overdue("gate_pass", old, False, now, 5.0, issue_done=False))


class BOM4PageEvidence(unittest.TestCase):
    """BOM-4:页面任务 build/typecheck 不够 DONE_REAL;无 URL+截图判 missing;用户反例优先 FAIL。"""

    def test_bom4_tech_only_is_missing(self):
        st = S.page_evidence_state(True, "build 通过,typecheck 0,代码核查 OK")
        self.assertEqual(st, "page_evidence_missing")

    def test_bom4_url_and_shot_is_pass(self):
        st = S.page_evidence_state(True, "https://app.x/canvas 截图见附件 canvas.png")
        self.assertEqual(st, "page_evidence_pass")

    def test_bom4_user_counterexample_is_fail(self):
        st = S.page_evidence_state(True, "https://app.x 截图.png", user_counterexample=True)
        self.assertEqual(st, "page_evidence_fail")

    def test_bom4_nonpage_task_passes(self):
        self.assertEqual(S.page_evidence_state(False, "纯后端改动"), "page_evidence_pass")


class BOM5GateMisroute(unittest.TestCase):
    """BOM-5:占位岗 '不能冒充千问/请用API' 必须判 gate_misrouted,不当完成。"""

    def test_bom5_placeholder_reply_is_misrouted(self):
        txt = "当前占位岗被直接 @ 不能做千问门禁判定,不能冒充千问,请走 line_bridge.py gate。"
        self.assertTrue(S.is_gate_misrouted(txt))
        self.assertEqual(S.gate_state(txt), "gate_misrouted")

    def test_bom5_misroute_overrides_fake_pass(self):
        # 即便占位岗嘴上写了 PASS,也必须判误触发,不能当通过
        txt = "占位岗不能执行门禁,VERDICT: PASS"
        self.assertEqual(S.gate_state(txt), "gate_misrouted")

    def test_bom5_real_gate_pass(self):
        self.assertEqual(S.gate_state("【门禁 Qwen】真机可见 VERDICT: PASS"), "gate_pass")

    def test_bom5_watchdog_flags_pl89_misroute(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW
        issues = _load("pl89_issues.json")
        runs = _load("pl89_runs.json")
        comments = _load("pl89_comments.json")
        W.fetch_runs = lambda iid: runs.get(iid, [])
        W.last_comment_age = lambda iid: W.age_min(comments.get(iid))
        alerts = W.detect(issues, 40.0, 5.0)
        kinds = {a["kind"] for a in alerts if a["ident"] == "PL-89"}
        self.assertIn("gate_misrouted", kinds)


class BOM6DoneAndReset(unittest.TestCase):
    """BOM-6:证据不全 done_gate BLOCK 且 line_reset 拒绝执行;PL-89 早收口不可再现。"""

    def _gate_env(self, comments_file):
        env = dict(os.environ)
        env["DONE_GATE_ISSUE_FIXTURE"] = os.path.join(HERE, "pl89_done_issue.json")
        env["DONE_GATE_COMMENTS_FIXTURE"] = os.path.join(HERE, comments_file)
        return env

    def test_bom6_done_real_state_blocks_on_missing(self):
        st, reasons = S.done_real_state({
            "development_completed": True, "audit_pass": True,
            "gate_pass": False, "is_page_task": True,
            "page_evidence_pass": False,
        })
        self.assertEqual(st, "done_real_blocked")
        self.assertTrue(reasons)

    def test_bom6_done_real_state_allows_when_complete(self):
        st, reasons = S.done_real_state({
            "development_completed": True, "audit_pass": True,
            "gate_pass": True, "is_page_task": True,
            "page_evidence_pass": True, "user_counterexample": False,
        })
        self.assertEqual(st, "done_real_allowed")

    def test_bom6_done_gate_cli_blocks_pl89(self):
        env = self._gate_env("pl89_done_block_comments.json")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "line_done_gate.py"), "PL-89"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, "PL-89 早收口应被门禁 BLOCK\n" + r.stdout)

    def test_bom6_done_gate_cli_allows_after_real_fix(self):
        env = self._gate_env("pl89_done_pass_comments.json")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "line_done_gate.py"), "PL-89"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "真返工+真门禁+视觉证据齐全应 ALLOW\n" + r.stdout)

    def test_bom6_done_gate_cli_allows_t02_local_qwen_api_gate(self):
        comments = [
            {"created_at": "2026-06-09T01:00:00Z", "author_type": "agent",
             "content": "开发完成: PR #1 typecheck build 0"},
            {"created_at": "2026-06-09T01:01:00Z", "author_type": "agent",
             "content": "专属审计结论\n\nVERDICT: PASS"},
            {"created_at": "2026-06-09T01:02:00Z", "author_type": "agent",
             "content": "千问本地 API 门禁已完成（POST http://127.0.0.1:18181/chat，model=`qwen-plus`）。\n\nVERDICT: PASS"},
        ]
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "t02_local_qwen_done_comments.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False)
        issue_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "t02_local_qwen_done_issue.json")
        with open(issue_path, "w", encoding="utf-8") as f:
            json.dump({"identifier": "PL-T02", "title": "后端补丁基线核查", "description": "backend patch baseline check", "metadata": {}}, f, ensure_ascii=False)
        env = dict(os.environ)
        env["DONE_GATE_ISSUE_FIXTURE"] = issue_path
        env["DONE_GATE_COMMENTS_FIXTURE"] = path
        r = subprocess.run([sys.executable, os.path.join(ROOT, "line_done_gate.py"), "PL-T02"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "T02 本地千问 API 门禁 PASS 应 ALLOW\n" + r.stdout)

    def test_bom6_line_reset_aborts_when_gate_blocks(self):
        # line_reset.py 子进程会调用 done_gate;证据不全必须拒绝 reset(rc=2),不清记忆。
        env = self._gate_env("pl89_done_block_comments.json")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "line_reset.py"),
                            "PL-89", "--no-archive"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, "证据不全时 line_reset 必须阻断\n" + r.stdout + r.stderr)


class XCrossDoneGate(unittest.TestCase):
    """X42/X69:发布门(line_done_gate)纳入 terminal-conflict 阻断与 evidence ledger 校验。"""

    def _full_pass_comments(self):
        # dev + 审计 PASS + 真门禁 PASS + 非页面任务,保证原五条门禁全过,只考察新增两条。
        return [
            {"created_at": "2026-06-09T01:00:00Z", "author_type": "agent",
             "content": "开发完成: PR #1 typecheck build 0"},
            {"created_at": "2026-06-09T01:01:00Z", "author_type": "agent",
             "content": "专属审计结论\n\nVERDICT: PASS"},
            {"created_at": "2026-06-09T01:02:00Z", "author_type": "agent",
             "content": "【门禁 Qwen】回写\n\nVERDICT: PASS"},
        ]

    def _issue(self):
        return {"id": "iid-x", "identifier": "PL-X42", "title": "后端补丁", "description": "backend", "metadata": {}}

    def test_x42_running_run_blocks_close(self):
        # 仍有 running run -> 置 done 会制造 terminal-conflict -> BLOCK。
        runs = [{"id": "run-running-1", "status": "running", "created_at": "2026-06-09T01:03:00Z"}]
        allow, _page, reasons = G.evaluate(self._issue(), self._full_pass_comments(), runs)
        self.assertFalse(allow, "running run 存在时收口必须 BLOCK")
        self.assertFalse(reasons["no_terminal_conflict"][0])

    def test_x42_no_running_run_allows(self):
        # run 早于审计/门禁 PASS,evidence_fresh 不判旧;只考察 terminal-conflict/ledger 两条新增。
        runs = [{"id": "run-done-1", "status": "completed", "created_at": "2026-06-09T00:59:00Z"}]
        allow, _page, reasons = G.evaluate(self._issue(), self._full_pass_comments(), runs)
        self.assertTrue(reasons["no_terminal_conflict"][0])
        self.assertTrue(reasons["evidence_ledger"][0])
        self.assertTrue(allow, "门禁全过且无 running run/ledger 完整应 ALLOW")

    def test_x42x69_offline_no_runs_degrades(self):
        # 离线/无 run 数据:两条新增校验退化放行,不改变原五条门禁结论。
        allow, _page, reasons = G.evaluate(self._issue(), self._full_pass_comments(), [])
        self.assertTrue(reasons["no_terminal_conflict"][0])
        self.assertTrue(reasons["evidence_ledger"][0])
        self.assertTrue(allow)

    def test_x69_ledger_built_and_fingerprinted(self):
        # 有 run 数据时,发布门生成 ledger 且每条带可重算指纹(X48)。
        runs = [{"id": "r1", "status": "completed", "created_at": "2026-06-09T01:03:00Z",
                 "result": "VERDICT: PASS https://app.x 截图.png"}]
        allow, _page, reasons = G.evaluate(self._issue(), self._full_pass_comments(), runs)
        self.assertTrue(reasons["evidence_ledger"][0], reasons["evidence_ledger"][1])
        led = E.build_ledger("iid-x", "PL-X42", runs)
        self.assertEqual(led["entry_count"], len(runs))
        self.assertTrue(all(e.get("evidence_sha256") for e in led["entries"]))


class BOM7AlertDedup(unittest.TestCase):
    """BOM-7:连续取消第二次必须能再次提醒(签名随状态变化);失败/取消/误触发分别签名。"""

    def test_bom7_leading_cancelled(self):
        runs = [{"status": "cancelled"}, {"status": "cancelled"}, {"status": "completed"}]
        self.assertEqual(W.leading_cancelled(runs), 2)

    def test_bom7_consecutive_cancel_escalates_to_crisis(self):
        # 构造同一 issue 连续两次取消 -> alert 带 streak>=2 且升级 crisis
        os.environ["WATCHDOG_NOW"] = FX_NOW
        issue = {
            "id": "ix", "identifier": "PL-TEST", "status": "in_progress",
            "assignee_id": list(W.DEFAULT_SQUADS.values())[0],
        }
        runs = [
            {"id": "r2", "status": "cancelled", "result": None,
             "created_at": "2026-06-09T04:25:00Z"},
            {"id": "r1", "status": "cancelled", "result": None,
             "created_at": "2026-06-09T04:18:00Z"},
        ]
        W.fetch_runs = lambda iid: runs
        W.last_comment_age = lambda iid: 1.0   # 评论新 -> 不触发 stage_stale,隔离取消逻辑
        alerts = W.detect([issue], 40.0, 5.0)
        canc = [a for a in alerts if a["kind"] == "cancelled"]
        self.assertTrue(canc, "连续取消未产生 cancelled 告警")
        self.assertGreaterEqual(canc[0].get("streak", 0), 2)
        self.assertEqual(canc[0].get("route_override"), "crisis",
                         "连续取消应升级为危机处理")

    def test_bom7_distinct_kinds_distinct_signatures(self):
        # 不同 kind 的签名 key 不同,互不吞没
        a1 = {"ident": "PL-1", "kind": "cancelled", "why": "x"}
        a2 = {"ident": "PL-1", "kind": "gate_misrouted", "why": "x"}
        k1 = "%s|%s" % (a1["ident"], a1["kind"])
        k2 = "%s|%s" % (a2["ident"], a2["kind"])
        self.assertNotEqual(k1, k2)


class BOM7HandoffWiring(unittest.TestCase):
    """BOM-7:告警台统一指向 PL-94,不得残留旧 PL-73/失效台号硬编码。"""

    OLD_BAD = ("199691f5", "PL-73")
    PL94 = "bc056ade-f639-41af-b5df-9c7fb6a27628"

    def test_bom7_watchdog_handoff_is_pl94(self):
        self.assertEqual(W.HANDOFF, self.PL94)

    def test_bom7_no_stale_handoff_hardcoded(self):
        import line_dispatch as D
        self.assertEqual(D.HANDOFF, self.PL94)
        for mod_file in ("line_watchdog.py", "line_dispatch.py"):
            with open(os.path.join(ROOT, mod_file), encoding="utf-8") as f:
                src = f.read()
            # 旧失效台号不应作为活跃 HANDOFF 常量出现(注释里说明可以)
            for line in src.splitlines():
                if line.strip().startswith("HANDOFF"):
                    self.assertNotIn("199691f5", line, "%s 仍硬编码旧台号" % mod_file)


class BOM8Roles(unittest.TestCase):
    """BOM-8:职责边界必须落到常量/配置,Claude 不自证视觉门禁。"""

    def test_bom8_all_roles_present(self):
        for r in ("claude_write", "codex_audit", "qwen_gate",
                  "deepseek_dig", "line_brain", "cc", "cx"):
            self.assertIn(r, S.ROLES)

    def test_bom8_qwen_gate_not_self_certified(self):
        duty = S.ROLES["qwen_gate"]["duty"]
        self.assertIn("不由 Claude 自证", duty)

    def test_bom8_describe_roles_runs(self):
        self.assertIn("职责边界", S.describe_roles())


class BSegEvidence(unittest.TestCase):
    """B段(U25-U40):证据验证/探针/信任策略/新鲜度/兜底 纯函数。"""

    # U28:截图/URL/附件真实存在性
    def test_u28_text_placeholder_screenshot_unverified(self):
        r = E.audit_completed_evidence("已完成,截图见上")  # 无 URL/无图片附件
        self.assertTrue(r, "纯文本'截图'应判无法核验")

    def test_u28_url_or_image_passes(self):
        self.assertFalse(E.audit_completed_evidence("完成 https://app.x/c 截图 c.png"))
        self.assertTrue(E.verify_artifacts("截图见附件", attachments=[
            {"content_type": "image/png", "filename": "shot.png"}])["image_in_attach"])

    # U29:GitHub PR/commit
    def test_u29_bare_pr_number_unverified(self):
        self.assertTrue(E.audit_completed_evidence("已提交 PR #5 完成"))

    def test_u29_real_pr_url_wellformed(self):
        refs = E.parse_github_refs("见 https://github.com/org/repo/pull/12")
        self.assertEqual(refs["pr_urls"][0]["number"], 12)
        self.assertTrue(E.github_ref_wellformed(refs))
        self.assertFalse(E.audit_completed_evidence("PR https://github.com/org/repo/pull/12"))

    def test_u29_verify_with_injected_gh_runner(self):
        text = "https://github.com/o/r/pull/9"
        good = E.verify_github_refs(text, gh_runner=lambda a, timeout=20: (True, "{}"), verify_network=True)
        self.assertTrue(good["ok"])
        bad = E.verify_github_refs(text, gh_runner=lambda a, timeout=20: (False, "404"), verify_network=True)
        self.assertFalse(bad["ok"])
        self.assertIn("不存在", bad["reason"])

    # U30:结构化 ledger
    def test_u30_ledger_records_source_and_verdict(self):
        runs = [{"id": "r1", "status": "completed", "created_at": "2026-06-09T04:00:00Z",
                 "result": "【门禁 Qwen】真机可见 VERDICT: PASS https://x/pull/1"}]
        led = E.build_ledger("iid", "PL-X", runs)
        e0 = led["entries"][0]
        self.assertEqual(e0["source"], "qwen_gate")
        self.assertEqual(e0["verdict"], "PASS")
        self.assertEqual(e0["trust"], "trusted")

    def test_u30_ledger_write_roundtrip(self):
        led = E.build_ledger("iid", "PL-WRITE", [])
        path = E.write_ledger(led, ledger_dir=os.path.join(os.environ.get("REG_TMP", "/tmp"), "ledger"))
        self.assertTrue(path and os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["identifier"], "PL-WRITE")

    # U31:信任策略
    def test_u31_gate_requires_trusted_source(self):
        self.assertTrue(E.passes_trust_policy("gate", E.classify_source("【门禁 Qwen】VERDICT: PASS")))
        self.assertFalse(E.passes_trust_policy("gate", E.classify_source("我自己测过了,门禁 PASS")))
        # 非约束阶段:自报也接受
        self.assertTrue(E.passes_trust_policy("development", "self_report"))

    def test_u31_self_report_with_qwen_keyword_not_trusted(self):
        # B-U31 反例:开发自报含 Qwen 字样,不得被归为可信 qwen_gate 放行门禁。
        bypass = "Claude开发自报: 我已跑 Qwen 门禁 VERDICT: PASS"
        self.assertTrue(E.is_self_report(bypass))
        self.assertEqual(E.classify_source(bypass), "self_report")
        self.assertFalse(E.passes_trust_policy("gate", E.classify_source(bypass)))
        # 真实门禁回写(非自报口吻)仍可信
        self.assertEqual(E.classify_source("【门禁 Qwen】VERDICT: PASS"), "qwen_gate")
        # agent 身份维度:可信门禁 agent 调用直接采信
        self.assertEqual(E.classify_source("VERDICT: PASS", agent="门禁 Qwen"), "qwen_gate")

    # U32 / X32:新鲜度
    def test_u32_stale_when_newer_failed_run(self):
        runs = [{"id": "new", "status": "failed", "created_at": "2026-06-09T05:00:00Z"}]
        stale, reason = E.evidence_is_stale("old", "2026-06-09T04:00:00Z", runs)
        self.assertTrue(stale)
        self.assertIn("失效", reason)

    def test_u32_fresh_when_no_newer_run(self):
        runs = [{"id": "ev", "status": "completed", "created_at": "2026-06-09T04:00:00Z"}]
        stale, _ = E.evidence_is_stale("ev", "2026-06-09T04:00:00Z", runs)
        self.assertFalse(stale)

    # 深挖·版本追溯 run_chain_deep_check(DeepSeek 根因②:旧 PASS 依赖的 run 被覆盖)
    def test_deepdig_chain_completed_run_supersedes_old_pass(self):
        # PL-94 复现:16:36 PASS 后被 2 条 completed + 1 条 running run 覆盖。原 evidence_is_stale
        # 只盯 failed/cancelled 会放行;run_chain_deep_check 据 completed 终态判 superseded(强制重检),
        # 而仍在 running 的那条只进 in_flight(进行中的重检,不算覆盖,不误杀正常流水线)。
        runs = [
            {"id": "7ca69593", "status": "running",   "created_at": "2026-06-09T17:28:00Z"},
            {"id": "5c64a47f", "status": "completed", "created_at": "2026-06-09T17:10:00Z"},
            {"id": "f19d9605", "status": "completed", "created_at": "2026-06-09T16:50:00Z"},
            {"id": "passrun",  "status": "completed", "created_at": "2026-06-09T16:36:02Z"},
        ]
        chk = E.run_chain_deep_check("passrun", "2026-06-09T16:36:02Z", runs)
        self.assertTrue(chk["superseded"])
        self.assertFalse(chk["hard_invalid"])           # 无 failed/cancelled -> 需重检而非硬失效
        self.assertEqual(len(chk["chain"]), 2)          # 仅 2 条 completed 计入覆盖链
        self.assertEqual(chk["latest_run"]["id"], "5c64a47f")   # 最新终态 run = 应据其重检
        self.assertEqual([r["id"] for r in chk["in_flight"]], ["7ca69593"])
        self.assertIn("强制重检", chk["reason"])

    def test_deepdig_chain_hard_invalid_when_failed_in_chain(self):
        runs = [
            {"id": "bad",     "status": "failed",    "created_at": "2026-06-09T17:00:00Z"},
            {"id": "passrun", "status": "completed", "created_at": "2026-06-09T16:36:02Z"},
        ]
        chk = E.run_chain_deep_check("passrun", "2026-06-09T16:36:02Z", runs)
        self.assertTrue(chk["superseded"] and chk["hard_invalid"])
        self.assertEqual(chk["worst_status"], "failed")

    def test_deepdig_chain_fresh_when_pass_is_latest(self):
        runs = [{"id": "passrun", "status": "completed", "created_at": "2026-06-09T16:36:02Z"}]
        chk = E.run_chain_deep_check("passrun", "2026-06-09T16:36:02Z", runs)
        self.assertFalse(chk["superseded"])
        self.assertEqual(chk["chain"], [])

    # PL-132:无 VERDICT 的 completed run 盖在旧 PASS 之上 -> **不再**报 stale_evidence
    # (这正是 PL-128 死循环的根因:无关 completed run 被当成覆盖旧 PASS)。
    def test_pl132_no_verdict_run_does_not_invalidate_old_pass(self):
        issue = {"id": "i-pl94", "identifier": "PL-94", "status": "in_progress"}
        runs = [
            {"id": "newrun",  "status": "completed", "created_at": "2026-06-09T17:10:00Z",
             "result": {"output": "继续推进,等待审计回写 VERDICT"}},
            {"id": "passrun", "status": "completed", "created_at": "2026-06-09T16:36:02Z",
             "result": {"output": "【门禁 Qwen】VERDICT: PASS"}, "agent_name": "门禁 Qwen"},
        ]
        alerts = W.audit_issue_evidence(issue, runs, "development", check_trust=False)
        kinds = [a.get("kind") for a in alerts]
        self.assertNotIn("stale_evidence", kinds)
        self.assertNotIn("gate_fail_rework", kinds)
        # 决策层:最新有效门禁证据仍是那条 PASS
        d = E.evidence_gate_decision(runs)
        self.assertEqual(d["status"], "pass")
        self.assertEqual(d["anchor"]["run_id"], "passrun")

    def test_deepdig_audit_clean_when_pass_is_latest(self):
        issue = {"id": "i-ok", "identifier": "PL-OK", "status": "in_progress"}
        runs = [
            {"id": "passrun", "status": "completed", "created_at": "2026-06-09T16:36:02Z",
             "result": {"output": "【门禁 Qwen】VERDICT: PASS"}, "agent_name": "门禁 Qwen"},
        ]
        alerts = W.audit_issue_evidence(issue, runs, "development", check_trust=False)
        self.assertNotIn("stale_evidence", [a.get("kind") for a in alerts])

    # U36:run_created_after(续派二次确认核心)
    def test_u36_run_created_after(self):
        runs = [{"id": "a", "created_at": "2026-06-09T04:00:00Z"},
                {"id": "b", "created_at": "2026-06-09T05:00:00Z"}]
        self.assertEqual(E.run_created_after(runs, "2026-06-09T04:30:00Z")["id"], "b")
        self.assertIsNone(E.run_created_after(runs, "2026-06-09T06:00:00Z"))

    def test_u36_watchdog_confirm_rerun(self):
        W.fetch_runs = lambda iid: [{"id": "fresh", "status": "running",
                                     "created_at": "2026-06-09T05:00:00Z"}]
        W._RUN_FETCH_WARNINGS.clear()
        ok, info = W.confirm_rerun("iid", "2026-06-09T04:00:00Z")
        self.assertTrue(ok)
        W.fetch_runs = lambda iid: [{"id": "old", "status": "completed",
                                     "created_at": "2026-06-09T03:00:00Z"}]
        ok2, _ = W.confirm_rerun("iid", "2026-06-09T04:00:00Z")
        self.assertFalse(ok2)

    # U37:PL-94 兜底
    def test_u37_append_fallback(self):
        p = os.path.join(os.environ.get("REG_TMP", "/tmp"), "fb_test.jsonl")
        if os.path.exists(p):
            os.remove(p)
        E.append_fallback("alert body", "PL-94 failed")
        out = E.append_fallback("alert body 2", "PL-94 failed again", path=p)
        self.assertTrue(out and os.path.exists(p))
        with open(p, encoding="utf-8") as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertEqual(rec["channel_failed"], "PL-94")

    # U25/U26:endpoint 探针(注入 prober,全离线)
    def test_u25_u26_probe_down_detected(self):
        alerts = E.health_probe_alerts(
            {"QWEN_API_URL": "u1", "DEEPSEEK_API_URL": "u2"},
            endpoint_prober=lambda url, timeout=6: (url != "u1", "fx"))
        kinds = [a[0] for a in alerts]
        self.assertIn("probe_down", kinds)
        self.assertTrue(any("Qwen" in a[1] for a in alerts))

    def test_u25_missing_url_is_down(self):
        ep = E.probe_endpoint("Qwen", "")
        self.assertFalse(ep["ok"])

    # U27:真机探针
    def test_u27_device_offline_detected(self):
        d = E.probe_device(runner=lambda timeout=8: (0, "empty"))
        self.assertFalse(d["ok"])
        d2 = E.probe_device(runner=lambda timeout=8: (2, "two"))
        self.assertTrue(d2["ok"])

    # X40:严重级别分级
    def test_x40_severity_levels_distinct(self):
        self.assertEqual(E.severity_of("failed"), "critical")
        self.assertEqual(E.severity_of("cancelled"), "high")
        self.assertEqual(E.severity_of("todo_no_claim"), "low")

    # 看门狗集成:U28/U29 evidence_unverified 检测
    def test_bseg_watchdog_flags_unverified_evidence(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW
        issue = {"id": "iu", "identifier": "PL-UNV", "status": "in_progress",
                 "assignee_id": list(W.DEFAULT_SQUADS.values())[0]}
        # 04:10 完成,距 FX_NOW(04:28)已 18 分钟,超出 X46 截图上传宽限(默认10分),
        # 因此应正常判 evidence_unverified(而非被上传宽限暂时抑制)。
        runs = [{"id": "ru", "status": "completed",
                 "result": "已完成,截图见上 PASS", "created_at": "2026-06-09T04:10:00Z"}]
        W.fetch_runs = lambda iid: runs
        W.last_comment_age = lambda iid: 1.0
        alerts = W.detect([issue], 40.0, 5.0)
        kinds = {a["kind"] for a in alerts if a["ident"] == "PL-UNV"}
        self.assertIn("evidence_unverified", kinds)


class CSegObservability(unittest.TestCase):
    """C段(U41-U64 + X41-X64):并发/持久化/可观测纯函数 + 看门狗集成。"""
    import tempfile as _tf
    NOW = __import__("datetime").datetime(2026, 6, 9, 4, 28,
                                          tzinfo=__import__("datetime").timezone.utc)

    # U42/X61 并发抓取:结果按 key 稳定收敛,顺序无关
    def test_u42_fetch_concurrent_keys_stable(self):
        res = O.fetch_concurrent(["a", "b", "c"], lambda k: k.upper(), workers=4)
        self.assertEqual(res, {"a": "A", "b": "B", "c": "C"})
        # 单 worker 退化串行,结果一致
        self.assertEqual(O.fetch_concurrent(["x"], lambda k: k * 2, workers=1), {"x": "xx"})

    # U43/X54 自适应并发:rate-limit 信号减半
    def test_u43_adaptive_workers_halves_on_ratelimit(self):
        self.assertEqual(O.adaptive_workers(8, True), 4)
        self.assertEqual(O.adaptive_workers(8, False), 8)
        self.assertEqual(O.adaptive_workers(2, True), 2)   # 不低于 floor

    # U65/X65/X56 退避:确定性指数退避,封顶
    def test_u65_backoff_exponential_capped(self):
        self.assertEqual(O.backoff_delay(0, base=2, cap=60), 2)
        self.assertEqual(O.backoff_delay(2, base=2, cap=60), 8)
        self.assertEqual(O.backoff_delay(10, base=2, cap=60), 60)

    # U44 单轮 deadline
    def test_u44_cycle_deadline(self):
        d = O.CycleDeadline(100.0, 30.0)
        self.assertFalse(d.expired(120.0))
        self.assertTrue(d.expired(131.0))
        self.assertEqual(O.CycleDeadline(0, 0).remaining(999), float("inf"))  # 0=不限时

    # U45/X62 cursor 续扫:分片跨轮接力,扫完归零
    def test_u45_slice_cursor_resume(self):
        ids = ["a", "b", "c", "d", "e"]
        b1, c1, w1 = O.slice_by_cursor(ids, 0, 2)
        b2, c2, w2 = O.slice_by_cursor(ids, c1, 2)
        b3, c3, w3 = O.slice_by_cursor(ids, c2, 2)
        self.assertEqual((b1, c1, w1), (["a", "b"], 2, False))
        self.assertEqual((b2, c2, w2), (["c", "d"], 4, False))
        self.assertEqual((b3, c3, w3), (["e"], 0, True))
        # limit<=0 = 全量不分片
        self.assertEqual(O.slice_by_cursor(ids, 0, 0), (ids, 0, True))

    # U52/X37 schema 版本 + 迁移(幂等、补键、不丢旧数据)
    def test_u52_migrate_state(self):
        m = O.migrate_state({"routed_run_ids": ["x"], "alert_sigs": {"k": "v"}})
        self.assertEqual(m["schema_version"], O.STATE_SCHEMA_VERSION)
        self.assertEqual(m["routed_run_ids"], ["x"])
        for k in ("scan_cursor", "metrics", "closed_loop", "route_retry"):
            self.assertIn(k, m)
        self.assertEqual(O.migrate_state(m), O.migrate_state(O.migrate_state(m)))  # 幂等

    # U54 损坏恢复:主文件损坏回退备份;checksum 校验
    def test_u54_corruption_recovery(self):
        d = self._tf.mkdtemp()
        p, bak = d + "/s.json", d + "/s.bak"
        O.save_state_resilient(p, O.migrate_state({"scan_cursor": 7}), bak)
        O.save_state_resilient(p, O.migrate_state({"scan_cursor": 9}), bak)  # bak<-7
        with open(p, "w") as _f: _f.write("{ broken")
        st, src = O.load_state_resilient(p, bak)
        self.assertEqual(src, "backup")
        self.assertEqual(st["scan_cursor"], 7)
        # checksum 篡改即判损坏
        good = O.pack_state(O.migrate_state({"scan_cursor": 1}))
        self.assertTrue(O.verify_state(good))
        good["scan_cursor"] = 999
        self.assertFalse(O.verify_state(good))

    # U62/X56 SLA/owner/severity + 重试元数据
    def test_u62_sla_owner_and_retry_meta(self):
        s = O.sla_for("failed")
        self.assertEqual(s["severity"], "critical")
        self.assertEqual(s["owner"], "危机处理Codex")
        self.assertTrue(O.sla_breached("failed", 999))
        self.assertFalse(O.sla_breached("failed", 1))
        m1 = O.route_retry_meta(None, self.NOW)
        m2 = O.route_retry_meta(m1, self.NOW)
        self.assertEqual(m1["failcount"], 1)
        self.assertEqual(m2["failcount"], 2)
        self.assertGreater(m2["delay_s"], m1["delay_s"])
        self.assertTrue(m2["next_retry_ts"])

    # U63/X64 线级 metrics
    def test_u63_line_metrics(self):
        owners = {"线A": {"sa"}, "线B": {"sb"}}
        items = [{"assignee_id": "sa", "status": "in_progress"},
                 {"assignee_id": "sa", "status": "backlog"},
                 {"assignee_id": "sb", "status": "blocked"}]
        alerts = [{"squad": "sa", "kind": "failed"}, {"squad": "sa", "kind": "cancelled"}]
        lm = O.line_metrics(items, owners, alerts)
        self.assertEqual(lm["线A"]["active"], 1)
        self.assertEqual(lm["线A"]["backlog"], 1)
        self.assertEqual(lm["线A"]["alerts"], 2)
        self.assertEqual(lm["线A"]["alerts_by_severity"].get("critical"), 1)
        self.assertEqual(lm["线B"]["blocked"], 1)

    # U50/U57/X50 状态板红灯:high/critical 告警 + 用户反例都点红
    def test_u50_build_status_red_lights(self):
        alerts = [{"ident": "PL-1", "kind": "failed", "why": "x"},
                  {"ident": "PL-2", "kind": "todo_no_claim", "why": "y"}]
        st = O.build_status(self.NOW, False, alerts, {}, 12, None)
        self.assertEqual(st["overall"], "red")          # 有 critical 告警
        reds = {r["ident"] for r in st["red_lights"]}
        self.assertIn("PL-1", reds)
        self.assertNotIn("PL-2", reds)                  # low 不点红
        # X50:用户反例覆盖 agent PASS,即便无告警也强制红
        st2 = O.build_status(self.NOW, True, [], {}, 5, None, user_counter_idents=["PL-9"])
        self.assertEqual(st2["overall"], "red")
        self.assertTrue(any(r["source"] == "user" for r in st2["red_lights"]))
        # 全绿
        st3 = O.build_status(self.NOW, True, [], {}, 5, None)
        self.assertEqual(st3["overall"], "green")

    # U56/U58/X63 心跳 + 停摆判定
    def test_u56_heartbeat(self):
        hb_ok = O.build_heartbeat(self.NOW, True, 10, 5, 0)
        self.assertEqual(hb_ok["last_successful_scan"], hb_ok["ts"])
        prev = {"last_successful_scan": "2026-06-09T03:00:00Z", "consecutive_fail": 0}
        hb_fail = O.build_heartbeat(self.NOW, False, 10, 5, 1, prev)
        self.assertEqual(hb_fail["last_successful_scan"], prev["last_successful_scan"])
        self.assertEqual(hb_fail["consecutive_fail"], 1)
        stale, age = O.heartbeat_stale(prev, self.NOW, 20)
        self.assertTrue(stale)
        self.assertGreater(age, 80)

    # U48/X58 告警分块 + route 刷屏折叠
    def test_u48_chunk_and_summarize(self):
        one = O.chunk_lines(["short"], 8000, header="H")
        self.assertEqual(len(one), 1)
        many = O.chunk_lines(["line%02d" % i for i in range(40)], 60, header="H")
        self.assertGreater(len(many), 1)
        self.assertTrue(all(len(c) <= 200 for c in many))  # 每块受控
        s = O.summarize_route_lines(["r%d" % i for i in range(10)], 3)
        self.assertEqual(len(s), 4)
        self.assertIn("折叠", s[-1])

    # U55/X43/X47 闭环链路 verdict
    def test_u55_closed_loop_entry(self):
        al = {"ident": "PL-7", "iid": "i7", "kind": "cancelled"}
        e_ok = O.closed_loop_entry(self.NOW, al, {"ok": True, "confirmed": True})
        self.assertEqual(e_ok["verdict"], "confirmed")
        e_unc = O.closed_loop_entry(self.NOW, al, {"ok": True, "confirmed": False})
        self.assertEqual(e_unc["verdict"], "unconfirmed")
        e_fail = O.closed_loop_entry(self.NOW, al, {"ok": False, "route_err": "boom"})
        self.assertEqual(e_fail["verdict"], "route_failed")

    # U49 logrotate
    def test_u49_logrotate(self):
        d = self._tf.mkdtemp()
        p = d + "/cron.log"
        with open(p, "w") as _f: _f.write("x" * 200)
        self.assertFalse(O.rotate_log(p, 1000))            # 未超限不动
        self.assertTrue(O.rotate_log(p, 100))              # 超限轮转
        self.assertTrue(os.path.exists(p + ".1"))
        self.assertEqual(os.path.getsize(p), 0)            # 主文件重建为空

    # U42 看门狗集成:fetch_runs 优先命中并发预抓取缓存,不再触发子进程查询
    def test_u42_watchdog_prefetch_cache_hit(self):
        # 其它用例可能 monkeypatch 过 W.fetch_runs,reload 取回真函数再验缓存分支
        import importlib
        importlib.reload(W)
        cached = [{"id": "r", "status": "cancelled", "result": None,
                   "created_at": "2026-06-09T04:10:00Z"}]
        try:
            W._RUNS_PREFETCH.clear()
            W._RUNS_PREFETCH["iA"] = cached
            # 命中缓存:返回缓存对象,绝不落到 subprocess(无 fixture 环境下也安全)
            self.assertIs(W.fetch_runs("iA"), cached)
        finally:
            W._RUNS_PREFETCH.clear()


class XSegScanIntegrity(unittest.TestCase):
    """X07/X10(审计返工):扫描不完整(覆盖截断 / 单轮 deadline 命中)时,
    看门狗必须只告警、阻断全部 route/assign/rerun,并把状态板标 partial-scan。"""
    import tempfile as _tf
    NOW = __import__("datetime").datetime(2026, 6, 9, 4, 28,
                                          tzinfo=__import__("datetime").timezone.utc)

    # X07/X10 纯函数:build_status 在 partial-scan 下即便无红灯也不得判 green
    def test_x07x10_build_status_partial_scan_not_green(self):
        # full + ok + 无告警 = green(回归保证既有行为不变)
        full = O.build_status(self.NOW, True, [], {}, 5, None)
        self.assertEqual(full["overall"], "green")
        self.assertEqual(full["scan_state"], "full")
        # partial-scan + 无告警 = yellow(绝不能 green),ok 被压成 False
        part = O.build_status(self.NOW, True, [], {}, 5, None, scan_state="partial-scan")
        self.assertEqual(part["overall"], "yellow")
        self.assertEqual(part["scan_state"], "partial-scan")
        self.assertFalse(part["ok"])
        # partial-scan 仍让 critical 告警点红(红 > 黄)
        red = O.build_status(self.NOW, False, [{"ident": "PL-1", "kind": "failed", "why": "x"}],
                             {}, 5, None, scan_state="partial-scan")
        self.assertEqual(red["overall"], "red")

    def _run_watchdog(self, extra_env, *args):
        d = self._tf.mkdtemp()
        fx = os.path.join(d, "issues.json")
        with open(fx, "w") as f:
            json.dump([
                {"id": "i1", "identifier": "PL-X07A", "title": "线小队-T01 任务A",
                 "status": "todo", "assignee_type": "agent", "assignee_id": "personal-agent-aaa"},
                {"id": "i2", "identifier": "PL-X07B", "title": "线小队-T01 任务B",
                 "status": "todo", "assignee_type": "agent", "assignee_id": "personal-agent-bbb"},
            ], f)
        env = dict(os.environ)
        env.update({
            "WATCHDOG_DRY_RUN": "1",
            "WATCHDOG_FIXTURE": fx,
            "WATCHDOG_STATE": os.path.join(d, "state.json"),
            "WATCHDOG_STATUS_F": os.path.join(d, "status.json"),
            "WATCHDOG_HEARTBEAT_F": os.path.join(d, "heartbeat.json"),
            # 隔离环境态:覆盖总开关停用标志(指向不存在的路径)与 leader 审计落盘,
            # 否则生产 watchdog.disabled 在位时本组 route 门控用例会被 WATCHDOG_DISABLED 短路。
            "WATCHDOG_DISABLE_FLAG": os.path.join(d, "nonexistent.disabled"),
            "WATCHDOG_LEADER_AUDIT_F": os.path.join(d, "leader_audit.jsonl"),
        })
        env.update(extra_env)
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "line_watchdog.py"),
             "--post", "--route", "--route-workers", "1", "--stale-min", "5", *args],
            cwd=ROOT, capture_output=True, text=True, env=env)
        return r.stdout + r.stderr

    # X07:issue 列表截断(覆盖到达上限)时,只告警 + 阻断全部 route/assign
    def test_x07_truncated_scan_blocks_route(self):
        out = self._run_watchdog({"WATCHDOG_ISSUE_LIMIT": "1"})
        self.assertIn("PARTIAL_SCAN", out)
        self.assertIn("扫描覆盖到达上限", out)          # 告警照常贴
        self.assertIn("ROUTE_SKIP", out)
        self.assertNotIn("would assign/rerun", out)     # 不做任何续派
        self.assertNotIn("ROUTE_JOB", out)

    # X10:单轮 deadline 命中时,输出 partial-scan + 停止后续 route/assign
    def test_x10_deadline_hit_blocks_route(self):
        out = self._run_watchdog({}, "--cycle-deadline-s", "0.000001")
        self.assertIn("CYCLE_DEADLINE_HIT", out)
        self.assertIn("PARTIAL_SCAN", out)
        self.assertIn("ROUTE_SKIP", out)
        self.assertNotIn("would assign/rerun", out)
        self.assertNotIn("ROUTE_JOB", out)

    # 反证:扫描完整(无截断/无 deadline)时,route 正常执行(确认不是把 route 一律关掉)
    def test_x07x10_full_scan_still_routes(self):
        out = self._run_watchdog({})
        self.assertNotIn("PARTIAL_SCAN", out)
        self.assertNotIn("ROUTE_SKIP", out)
        self.assertIn("ROUTE_JOB", out)                 # 完整扫描下续派仍照常

    # --- X07/X10 复审返工:auto-rerun 必须同样纳入 scan_blocked 门控 ---
    # 复审 FAIL 的根因:--auto-rerun 分支在 route/assign 阻断块之前执行,
    # partial-scan 下仍会输出"已自动重启(rerun)"并真的发起 rerun。
    def _run_watchdog_autorerun(self, extra_env, *args):
        """带一条"1分钟前失败 run"的 fixture 跑看门狗,并开 --auto-rerun。
        失败 run 落在 i1(PL-X07A)上;截断 limit=1 时 i1 仍在覆盖内,
        deadline 命中时全量扫描但被阻断——两种 partial-scan 都应吞掉 rerun。"""
        d = self._tf.mkdtemp()
        # 必须派给线小队(否则只是 personal_assignment,走不到失败 run 检测)。
        T01 = "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96"   # 线小队-T01
        fx = os.path.join(d, "issues.json")
        with open(fx, "w") as f:
            json.dump([
                {"id": "i1", "identifier": "PL-X07A", "title": "线小队-T01 任务A",
                 "status": "in_progress", "assignee_type": "squad", "assignee_id": T01},
                {"id": "i2", "identifier": "PL-X07B", "title": "线小队-T01 任务B",
                 "status": "in_progress", "assignee_type": "squad", "assignee_id": T01},
            ], f)
        runs_fx = os.path.join(d, "runs.json")
        with open(runs_fx, "w") as f:
            json.dump({"i1": [{"id": "run-x07a-fail",
                               "status": "failed",
                               "created_at": "2026-06-09T04:27:00Z"}]}, f)
        env = dict(os.environ)
        env.update({
            "WATCHDOG_DRY_RUN": "1",
            "WATCHDOG_NOW": "2026-06-09T04:28:00Z",
            "WATCHDOG_FIXTURE": fx,
            "WATCHDOG_RUNS_FIXTURE": runs_fx,
            "WATCHDOG_STATE": os.path.join(d, "state.json"),
            "WATCHDOG_STATUS_F": os.path.join(d, "status.json"),
            "WATCHDOG_HEARTBEAT_F": os.path.join(d, "heartbeat.json"),
            "WATCHDOG_DISABLE_FLAG": os.path.join(d, "nonexistent.disabled"),
            "WATCHDOG_LEADER_AUDIT_F": os.path.join(d, "leader_audit.jsonl"),
        })
        env.update(extra_env)
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "line_watchdog.py"),
             "--post", "--route", "--auto-rerun",
             "--route-workers", "1", "--stale-min", "5", *args],
            cwd=ROOT, capture_output=True, text=True, env=env)
        return r.stdout + r.stderr

    # X07:截断 + 失败 run + --auto-rerun → 只告警,绝不 rerun
    def test_x07_truncated_scan_blocks_auto_rerun(self):
        out = self._run_watchdog_autorerun({"WATCHDOG_ISSUE_LIMIT": "1"})
        self.assertIn("PARTIAL_SCAN", out)
        self.assertIn("失败run", out)                    # 失败 run 告警照常贴
        self.assertIn("ROUTE_SKIP", out)
        self.assertNotIn("已自动重启(rerun)", out)        # 不执行 rerun
        self.assertNotIn("would rerun(auto)", out)       # DRY 意图也不触发

    # X10:deadline 命中 + 失败 run + --auto-rerun → 只告警,绝不 rerun
    def test_x10_deadline_hit_blocks_auto_rerun(self):
        out = self._run_watchdog_autorerun({}, "--cycle-deadline-s", "0.000001")
        self.assertIn("CYCLE_DEADLINE_HIT", out)
        self.assertIn("PARTIAL_SCAN", out)
        self.assertIn("失败run", out)
        self.assertIn("ROUTE_SKIP", out)
        self.assertNotIn("已自动重启(rerun)", out)
        self.assertNotIn("would rerun(auto)", out)

    # 反证:完整扫描 + 失败 run + --auto-rerun → auto-rerun 仍照常执行
    # (DRY_RUN 下打印意图 "would rerun(auto)";证明门控不是把 auto-rerun 一律关死)
    def test_x07x10_full_scan_auto_rerun_executes(self):
        out = self._run_watchdog_autorerun({})
        self.assertNotIn("PARTIAL_SCAN", out)
        self.assertNotIn("ROUTE_SKIP", out)
        self.assertIn("失败run", out)
        self.assertIn("would rerun(auto)", out)          # 完整扫描下 auto-rerun 触发


class TestPL124P1(unittest.TestCase):
    """PL-124 P1 纯函数:U08 时钟偏移 / U21 typed 阶段事件 / U22 role-stage。"""

    NOW = __import__("datetime").datetime(2026, 6, 9, 4, 28,
                                          tzinfo=__import__("datetime").timezone.utc)

    def test_u08_future_timestamp_is_skew(self):
        sk, why, m = S.clock_skew_alert(self.NOW, ["2026-06-09T04:31:00Z"], 2.0)
        self.assertTrue(sk)
        self.assertAlmostEqual(m, 3.0, places=1)
        self.assertIn("时钟", why)

    def test_u08_within_tolerance_no_skew(self):
        sk, _, _ = S.clock_skew_alert(self.NOW, ["2026-06-09T04:29:30Z"], 2.0)
        self.assertFalse(sk)

    def test_u08_only_past_timestamps_no_skew(self):
        sk, _, _ = S.clock_skew_alert(self.NOW, ["2026-06-09T03:00:00Z", None], 2.0)
        self.assertFalse(sk)

    def test_u21_typed_event_field_beats_text(self):
        ev, typed = S.event_from_run({"event": "gate_pass"}, "无关文本不含门禁")
        self.assertEqual(ev, "gate_pass")
        self.assertTrue(typed)

    def test_u21_stage_verdict_composition(self):
        self.assertEqual(S.compose_stage_verdict("audit", "FAIL"), "audit_fail")
        self.assertEqual(S.compose_stage_verdict("gate", "pass"), "gate_pass")
        self.assertEqual(S.compose_stage_verdict("development", "done"), "dev_done")
        self.assertIsNone(S.compose_stage_verdict("audit", "running"))

    def test_u21_falls_back_to_regex(self):
        ev, typed = S.event_from_run({}, "专属审计 VERDICT: PASS")
        self.assertEqual(ev, "audit_pass")
        self.assertFalse(typed)

    def test_u22_role_for_event(self):
        self.assertEqual(S.role_for_event("gate_pass"), "qwen_gate")
        self.assertEqual(S.role_for_event("dev_done"), "claude_write")
        self.assertEqual(S.role_for_event("audit_fail"), "codex_audit")
        self.assertIsNone(S.role_for_event("not_an_event"))


class TestPL124P2(unittest.TestCase):
    """PL-124 P2:主机级健康/权限/资源/版本探针 + 总开关/canary/轮ledger/释放门。"""

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 6, 9, 4, 28, tzinfo=timezone.utc)

    # ---- U15 + U70 权限漂移 ----
    def test_u15_cli_unavailable(self):
        ks = {k for k, _, _ in S.permission_drift_alert(False, [])}
        self.assertIn("cli_permission_drift", ks)
        self.assertEqual(S.permission_drift_alert(True, []), [])

    def test_u70_file_drift(self):
        a = S.permission_drift_alert(True, [
            {"path": "/a", "exists": False},
            {"path": "/b", "exists": True, "owner": "stranger", "writable": True},
            {"path": "/c", "exists": True, "owner": "fleet", "writable": False}])
        self.assertEqual(len(a), 3)
        self.assertTrue(all(k == "permission_drift" for k, _, _ in a))
        self.assertEqual(S.permission_drift_alert(True, [
            {"path": "/ok", "exists": True, "owner": "root", "writable": True}]), [])

    # ---- U59 + U60 资源压力 ----
    def test_u59_u60_pressure(self):
        ks = {k for k, _, _ in S.resource_pressure_alert(3.0, 95.0)}
        self.assertEqual(ks, {"memory_pressure", "disk_pressure"})
        self.assertEqual(S.resource_pressure_alert(70.0, 80.0), [])
        self.assertEqual(S.resource_pressure_alert(None, None), [])

    def test_u60_cleanup_candidates(self):
        names = {e["name"] for e in S.cleanup_candidates(
            [{"name": "x.bak.pl1"}, {"name": "line_watchdog.py"},
             {"name": "a.log.3"}, {"name": "s.tmp.9"}, {"name": "watchdog_state.json"}])}
        self.assertEqual(names, {"x.bak.pl1", "a.log.3", "s.tmp.9"})

    # ---- U61 记忆清理定时 ----
    def test_u61_cleanup_due(self):
        now = self._now()
        self.assertTrue(S.memory_cleanup_due(None, now)[0])
        self.assertTrue(S.memory_cleanup_due("2026-06-09T01:00:00Z", now, 2.0)[0])
        self.assertFalse(S.memory_cleanup_due("2026-06-09T03:30:00Z", now, 2.0)[0])

    # ---- U68 CLI 版本 pin ----
    def test_u68_version_drift(self):
        self.assertEqual([k for k, _, _ in S.cli_version_drift_alert("multica dev", "")],
                         ["cli_version_unpinned"])
        self.assertEqual([k for k, _, _ in S.cli_version_drift_alert("multica 2", "multica dev")],
                         ["cli_version_drift"])
        self.assertEqual(S.cli_version_drift_alert("multica dev", "multica dev"), [])

    # ---- U69 出口/代理 ----
    def test_u69_proxy(self):
        down = lambda u: (False, "x")
        up = lambda u: (True, "x")
        self.assertEqual([k for k, _, _ in S.proxy_health_alert([{"name": "P", "url": "http://x:1"}], down)],
                         ["proxy_down"])
        self.assertEqual(S.proxy_health_alert([{"name": "P", "url": "http://x:1"}], up), [])
        self.assertEqual(len(S.proxy_health_alert(
            [{"url": "http://x:1"}, {"url": "http://x:1"}], down)), 1)

    # ---- U71 图片压缩/token ----
    def test_u71_image_oversize(self):
        big = S.image_oversize_alert([{"filename": "a.png", "size_bytes": 5 * 1024 * 1024}])
        self.assertEqual([k for k, _, _ in big], ["image_oversize"])
        self.assertEqual(S.image_oversize_alert(
            [{"filename": "a.png", "size_bytes": 100},
             {"filename": "b.txt", "size_bytes": 9 * 1024 * 1024}]), [])

    # ---- U72 总开关 + canary ----
    def test_u72_disable(self):
        self.assertTrue(O.watchdog_disabled({"WATCHDOG_DISABLED": "1"})[0])
        self.assertTrue(O.watchdog_disabled({"WATCHDOG_DISABLED": "true"})[0])
        self.assertFalse(O.watchdog_disabled({})[0])

    def test_u72_canary(self):
        self.assertTrue(O.canary_allows("abc", 100))
        self.assertFalse(O.canary_allows("abc", 0))
        self.assertEqual(O.canary_allows("id-5", 30), O.canary_allows("id-5", 30))
        ratio = sum(O.canary_allows("id-%d" % i, 30) for i in range(300))
        self.assertTrue(45 <= ratio <= 135)

    # ---- U51 每轮 ledger ----
    def test_u51_cycle_ledger(self):
        cyc = E.build_cycle_ledger(self._now(), 12,
            [{"kind": "zombie"}, {"kind": "zombie"}, {"kind": "disk_pressure"}],
            "full", [{"ok": True}, {"ok": False}], "green", 30)
        self.assertEqual(cyc["alert_kinds"], {"zombie": 2, "disk_pressure": 1})
        self.assertEqual((cyc["routed_ok"], cyc["routed_fail"]), (1, 1))
        self.assertEqual(cyc["scanned"], 12)

    def test_u51_cycle_ledger_write(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="cyc-")
        cyc = E.build_cycle_ledger(self._now(), 1, [], "full")
        p = E.write_cycle_ledger(cyc, ledger_dir=d)
        self.assertTrue(p and os.path.exists(p))
        with open(p) as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual(row["scan_state"], "full")

    def test_u51_emit_observability_threads_route_results(self):
        # PL-130:回归保护 —— emit_observability 必须把本轮真实 route 结果
        # 透传给 cycle ledger,否则 routed_ok/fail 恒为 0(PL-124 审计原 FAIL 点)。
        import tempfile
        from argparse import Namespace
        tmp = tempfile.mkdtemp(prefix="emit-")
        old_dir, old_hb, old_st = E.LEDGER_DIR, W.HEARTBEAT_F, W.STATUS_F
        E.LEDGER_DIR = tmp
        W.HEARTBEAT_F = os.path.join(tmp, "hb.json")
        W.STATUS_F = os.path.join(tmp, "status.json")
        try:
            a = Namespace(emit_observability=True, canary_route_pct=100.0)
            st = {"metrics": [], "closed_loop": [], "alert_sigs": {}}
            W.emit_observability(a, st, items=[], alerts=[{"kind": "failed"}],
                                 idle_results={}, closed_loop=[], cycle_ms=10,
                                 scan_ok=True, scanned=5, scan_state="full",
                                 route_results=[{"ok": True}, {"ok": False}, {"ok": True}])
            with open(os.path.join(tmp, "cycles.jsonl")) as f:
                row = json.loads(f.read().splitlines()[-1])
            self.assertEqual((row["routed_ok"], row["routed_fail"]), (2, 1))
        finally:
            E.LEDGER_DIR, W.HEARTBEAT_F, W.STATUS_F = old_dir, old_hb, old_st

    # ---- U15-U71 system_health_alerts 离线聚合 ----
    def test_sys_health_offline(self):
        fx = {"cli_ok": False, "mem_avail_pct": 2.0, "disk_used_pct": 99.0,
              "mem_cleanup_last": None, "cli_version": "multica dev",
              "files": [{"path": "/k", "exists": False}],
              "proxies": [{"name": "P", "url": "http://p:1"}], "proxy_down_urls": ["http://p:1"],
              "attachments": [{"filename": "big.png", "size_bytes": 9 * 1024 * 1024}]}
        old = os.environ.get("WATCHDOG_SYSHEALTH_FIXTURE")
        os.environ["WATCHDOG_SYSHEALTH_FIXTURE"] = json.dumps(fx)
        try:
            ks = {al["kind"] for al in W.system_health_alerts({}, None)}
        finally:
            if old is None:
                os.environ.pop("WATCHDOG_SYSHEALTH_FIXTURE", None)
            else:
                os.environ["WATCHDOG_SYSHEALTH_FIXTURE"] = old
        self.assertTrue({"cli_permission_drift", "permission_drift", "memory_pressure",
                         "disk_pressure", "memory_cleanup_due", "cli_version_unpinned",
                         "proxy_down", "image_oversize"} <= ks)


class PL132EvidenceAnchor(unittest.TestCase):
    """PL-132:旧 PASS 失效改为'基于最新有效门禁证据 run/commit'判定。

    根治 PL-128 死循环:cancelled / 纯派发 / no_action / 个人 mention / 无裁决 completed
    都**不得**使旧 PASS 失效;只有最新一条带 VERDICT 的 gate run 决定门禁结论。
    """

    PASS_RUN = {
        "id": "passrun", "status": "completed", "created_at": "2026-06-09T16:36:02Z",
        "completed_at": "2026-06-09T16:36:30Z", "agent_name": "门禁 Qwen",
        "result": {"output": "【门禁 Qwen】VERDICT: PASS\ncommit abcdef1234567\n"
                             "测试命令 python3 -m pytest;测试结果 42 passed"}}

    # ---- run_evidence_role:逐类 run 的角色判定 ----
    def test_role_cancelled_not_gate_relevant(self):
        r = E.run_evidence_role({"id": "c", "status": "cancelled",
                                 "result": {"output": "VERDICT: FAIL"}})
        self.assertEqual(r["category"], "cancelled")
        self.assertFalse(r["gate_relevant"])

    def test_role_dispatch_not_gate_relevant(self):
        r = E.run_evidence_role({"id": "d", "status": "cancelled", "kind": "comment",
                                 "trigger_summary": "🚨 看门狗自动分流(小队路由,纯脚本,不判因)"})
        # cancelled 先命中也无妨;关键是 gate_relevant=False
        self.assertFalse(r["gate_relevant"])
        r2 = E.run_evidence_role({"id": "d2", "status": "completed", "kind": "comment",
                                  "trigger_summary": "🚨 看门狗自动分流 小队路由 纯脚本,不判因",
                                  "result": {"output": "已派发"}})
        self.assertEqual(r2["category"], "dispatch")
        self.assertFalse(r2["gate_relevant"])

    def test_role_no_action_not_gate_relevant(self):
        r = E.run_evidence_role({"id": "n", "status": "completed",
            "result": {"output": "当前 PL-128 已是 cancelled,按运行规约不能重开,"
                                 "也不需要回复评论;我只记录本次触发评估。"}})
        self.assertEqual(r["category"], "no_action")
        self.assertFalse(r["gate_relevant"])

    def test_role_personal_mention_is_protocol_violation(self):
        r = E.run_evidence_role({"id": "p", "status": "completed", "kind": "comment",
            "trigger_summary": "[@Claude开发](mention://agent/be3d60e9) 你来做开发落地"})
        self.assertEqual(r["category"], "personal_mention")
        self.assertFalse(r["gate_relevant"])

    def test_role_gate_pass_is_relevant_with_commit(self):
        r = E.run_evidence_role(self.PASS_RUN)
        self.assertEqual(r["category"], "gate")
        self.assertTrue(r["gate_relevant"])
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["commit"], "abcdef1234567")

    def test_role_completed_without_verdict_not_relevant(self):
        r = E.run_evidence_role({"id": "x", "status": "completed",
            "result": {"output": "继续推进,等待审计回写 VERDICT"}})
        self.assertEqual(r["category"], "gate")
        self.assertFalse(r["gate_relevant"])   # 无 VERDICT -> 不覆盖旧 PASS

    # ---- PL-128 时间线回归:旧 PASS 后混入各种无关 run,不得使 PASS 失效 ----
    def test_pl128_timeline_regression_old_pass_survives_noise(self):
        runs = [
            # 旧 PASS 之后:个人 mention run、纯派发 run、no_action run、cancelled run、
            # 无 VERDICT 的 completed run —— 全是 PL-128 死循环里反复刷屏的"无关 run"。
            {"id": "mention", "status": "completed", "created_at": "2026-06-09T18:17:35Z",
             "trigger_summary": "[@Claude开发](mention://agent/be3d60e9) 你来做开发落地"},
            {"id": "route",   "status": "cancelled", "created_at": "2026-06-09T18:18:02Z",
             "trigger_summary": "🚨 看门狗自动分流(小队路由,纯脚本,不判因)"},
            {"id": "noact",   "status": "completed", "created_at": "2026-06-09T18:18:36Z",
             "result": {"output": "PL-128 已是 cancelled,未重开、未评论,仅记录 no_action"}},
            {"id": "bare",    "status": "completed", "created_at": "2026-06-09T17:10:00Z",
             "result": {"output": "继续推进,等待审计"}},
            self.PASS_RUN,
        ]
        d = E.evidence_gate_decision(runs)
        self.assertEqual(d["status"], "pass")
        self.assertEqual(d["anchor"]["run_id"], "passrun")
        issue = {"id": "i-128", "identifier": "PL-128", "status": "in_progress"}
        alerts = W.audit_issue_evidence(issue, runs, "development", check_trust=False)
        kinds = [a.get("kind") for a in alerts]
        self.assertNotIn("stale_evidence", kinds)
        self.assertNotIn("gate_fail_rework", kinds)

    # ---- 真实新门禁 PASS:识别并产出 latest_valid_evidence_* metadata ----
    def test_real_new_pass_recognized_and_metadata(self):
        d = E.evidence_gate_decision([self.PASS_RUN])
        self.assertEqual(d["status"], "pass")
        md = E.evidence_metadata_from_anchor(d["anchor"])
        self.assertEqual(md["gate_status"], "pass")
        self.assertEqual(md["latest_valid_evidence_run_id"], "passrun")
        self.assertEqual(md["latest_valid_evidence_commit"], "abcdef1234567")
        self.assertEqual(md["latest_valid_evidence_at"], "2026-06-09T16:36:30Z")

    def test_newer_pass_replaces_older_pass(self):
        older = dict(self.PASS_RUN)
        newer = {"id": "pass2", "status": "completed", "created_at": "2026-06-09T19:00:00Z",
                 "completed_at": "2026-06-09T19:00:30Z", "agent_name": "门禁 Qwen",
                 "result": {"output": "【门禁 Qwen】VERDICT: PASS commit 99aa776655ff3"}}
        d = E.evidence_gate_decision([newer, older])
        self.assertEqual(d["anchor"]["run_id"], "pass2")
        self.assertEqual(d["anchor"]["commit"], "99aa776655ff3")

    # ---- 真实新门禁 FAIL:输出返工点(gate_fail_rework),不再用旧 PASS 覆盖话术 ----
    def test_real_new_fail_reports_rework_not_stale(self):
        runs = [
            {"id": "failrun", "status": "completed", "created_at": "2026-06-09T19:00:00Z",
             "completed_at": "2026-06-09T19:00:30Z", "agent_name": "门禁 Qwen",
             "result": {"output": "【门禁 Qwen】VERDICT: FAIL\n登录跳转用例未通过"}},
            self.PASS_RUN,
        ]
        d = E.evidence_gate_decision(runs)
        self.assertEqual(d["status"], "fail")
        self.assertEqual(d["anchor"]["run_id"], "failrun")
        issue = {"id": "i-f", "identifier": "PL-F", "status": "in_review"}
        alerts = W.audit_issue_evidence(issue, runs, "gate", check_trust=False)
        kinds = [a.get("kind") for a in alerts]
        self.assertIn("gate_fail_rework", kinds)
        self.assertNotIn("stale_evidence", kinds)
        why = next(a["why"] for a in alerts if a["kind"] == "gate_fail_rework")
        self.assertIn("FAIL", why)
        self.assertIn("返工", why)

    # ---- evidence_missing:做了门禁动作但缺裁决,决策层标 evidence_missing(可去重冷却) ----
    def test_completed_without_verdict_is_evidence_missing(self):
        runs = [{"id": "halfdone", "status": "completed", "created_at": "2026-06-09T19:00:00Z",
                 "result": {"output": "继续推进开发,等待门禁回写结论"}}]
        d = E.evidence_gate_decision(runs)
        self.assertEqual(d["status"], "evidence_missing")
        self.assertEqual(d["missing_run"]["id"], "halfdone")
        # audit 不重复告警(交由 BOM-2 最新 run 检测);此处确认 audit 不报 stale/gate_fail。
        issue = {"id": "i-m", "identifier": "PL-M", "status": "in_progress"}
        alerts = W.audit_issue_evidence(issue, runs, "development", check_trust=False)
        kinds = [a.get("kind") for a in alerts]
        self.assertNotIn("stale_evidence", kinds)
        self.assertNotIn("gate_fail_rework", kinds)
        # 经 detect() 全链:BOM-2 最新 run 检测产出 evidence_missing(去重冷却)。
        _of = W.fetch_runs; _oc = W.last_comment_age
        W.fetch_runs = lambda iid: runs
        W.last_comment_age = lambda iid: 1.0
        try:
            issue2 = {"id": "i-m2", "identifier": "PL-M2", "status": "in_progress",
                      "assignee_id": list(W.DEFAULT_SQUADS.values())[0]}
            dkinds = {a["kind"] for a in W.detect([issue2], 40.0, 5.0) if a["ident"] == "PL-M2"}
            self.assertIn("evidence_missing", dkinds)
        finally:
            W.fetch_runs = _of; W.last_comment_age = _oc

    def test_only_noise_runs_no_evidence_no_false_stale(self):
        # 全是无关 run,没有任何 PASS:不得报 stale_evidence/gate_fail_rework
        runs = [
            {"id": "c", "status": "cancelled", "created_at": "2026-06-09T19:00:00Z"},
            {"id": "n", "status": "completed", "created_at": "2026-06-09T18:00:00Z",
             "result": {"output": "只记录 no_action,未改状态"}},
        ]
        d = E.evidence_gate_decision(runs)
        self.assertEqual(d["status"], "no_evidence")
        issue = {"id": "i-z", "identifier": "PL-Z", "status": "in_progress"}
        alerts = W.audit_issue_evidence(issue, runs, "development", check_trust=False)
        kinds = [a.get("kind") for a in alerts]
        self.assertNotIn("stale_evidence", kinds)
        self.assertNotIn("gate_fail_rework", kinds)


class XCrossFail8(unittest.TestCase):
    """PL-139 CROSS 叶子 8 项 FAIL 补齐:X23/X25/X26/X34/X35/X46/X48/X61。

    每项:纯函数判定 + 看门狗集成,全离线、确定性。
    """
    import datetime as _dt
    NOW = _dt.datetime(2026, 6, 9, 4, 28, tzinfo=_dt.timezone.utc)

    # ── X23:leader 轮换/小队重建时,并发 route 必须按目标小队代际锁定,不套到新代际 ──
    def test_x23_generation_changes_with_leader(self):
        g1 = O.squad_generation("sq-1", "leaderA")
        g2 = O.squad_generation("sq-1", "leaderB")   # leader 轮换
        g3 = O.squad_generation("sq-2", "leaderA")   # 小队重建(新 squad_id)
        self.assertNotEqual(g1, g2)
        self.assertNotEqual(g1, g3)
        self.assertEqual(g1, O.squad_generation("sq-1", "leaderA"))  # 稳定可重算

    def test_x23_route_guard_blocks_stale_generation(self):
        g_old = O.squad_generation("sq-1", "leaderA")
        g_new = O.squad_generation("sq-1", "leaderB")
        ok, _ = O.route_generation_guard(g_old, g_old)
        self.assertTrue(ok)
        bad, reason = O.route_generation_guard(g_old, g_new)
        self.assertFalse(bad)
        self.assertIn("代际", reason)
        # 缺代际(老作业)向后兼容放行
        self.assertTrue(O.route_generation_guard("", g_new)[0])

    def test_x23_execute_route_skips_on_generation_change(self):
        os.environ.pop("WATCHDOG_DRY_RUN", None)
        sid = list(W.DEFAULT_SQUADS.values())[0]
        job = {"iid": "i1", "ident": "PL-A", "squad": sid, "route": "brain",
               "alerts": [], "gen": "deadbeef0000", "seq": 1}  # 与当前代际不符
        _called = {"route": False}
        _orig = W.route_to_squad
        W.route_to_squad = lambda *a, **k: _called.__setitem__("route", True) or (True, "")
        try:
            res = W.execute_route_job(job, timeout=5)
        finally:
            W.route_to_squad = _orig
        self.assertTrue(res["gen_skipped"])
        self.assertFalse(res["ok"])
        self.assertFalse(_called["route"])   # 代际不符:绝不真正 assign/rerun

    # ── X25:状态/去重 key 必须含 squad id + generation,不能只用 T 编号 ──
    def test_x25_state_key_includes_squad_and_generation(self):
        g1 = O.squad_generation("sq", "L1")
        g2 = O.squad_generation("sq", "L2")
        k1 = O.state_key("PL-1", "sq", g1, "iid-1", "failed")
        k2 = O.state_key("PL-1", "sq", g2, "iid-1", "failed")  # 仅 leader 代际变
        self.assertNotEqual(k1, k2)                            # 代际变 → key 变,告警可重触发
        self.assertIn("sq", k1)
        self.assertIn(g1, k1)

    def test_x25_alert_key_changes_after_leader_rotation(self):
        sid = list(W.DEFAULT_SQUADS.values())[0]
        al = {"ident": "PL-1", "squad": sid, "iid": "iid-1", "kind": "failed"}
        W._BRAIN_CACHE = {sid: ("leaderA", "线主脑-T01")}
        key_a = W.alert_state_key(al)
        W._BRAIN_CACHE = {sid: ("leaderB", "线主脑-T01")}   # leader 轮换
        key_b = W.alert_state_key(al)
        W._SQUAD_CACHE = W._BRAIN_CACHE = None              # 复位缓存
        self.assertNotEqual(key_a, key_b)

    # ── X26:leader 映射变更必须写入审计日志 ──
    def test_x26_diff_leader_map(self):
        d = O.diff_leader_map({"a": "L1", "b": "L2"}, {"a": "L1", "b": "L9", "c": "L3"})
        by = {e["squad"]: e for e in d}
        self.assertEqual(by["b"]["change"], "rotated")
        self.assertEqual(by["c"]["change"], "added")
        self.assertNotIn("a", by)   # 未变不记
        self.assertEqual(O.diff_leader_map({"a": "L1"}, {"a": "L1"}), [])

    def test_x26_append_leader_audit_writes_jsonl(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "leader_audit.jsonl")
        changes = O.diff_leader_map({"a": "L1"}, {"a": "L2"})
        n = O.append_leader_audit(changes, path, now=self.NOW)
        self.assertEqual(n, 1)
        with open(path) as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertEqual(rec["change"], "rotated")
        self.assertEqual(rec["from"], "L1")
        self.assertEqual(rec["to"], "L2")
        self.assertTrue(rec["ts"])
        # 再次变更追加(可重放历史)
        O.append_leader_audit(O.diff_leader_map({"a": "L2"}, {"a": "L3"}), path, now=self.NOW)
        with open(path) as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    # ── X34:parent done + child FAIL 必须自动打回父任务 ──
    def test_x34_parent_reopen_on_child_fail(self):
        dec = S.parent_reopen_decision("done", ["blocked", "done"])  # 子任务 blocked=FAIL
        self.assertTrue(dec["reopen"])
        self.assertEqual(dec["to_status"], "in_progress")
        self.assertTrue(S.parent_reopen_decision("cancelled", ["blocked"])["reopen"])
        # 子任务最新关键 run failed/cancelled 的更强 FAIL 信号
        self.assertTrue(S.parent_reopen_decision("done", ["done"], child_failed_runs=True)["reopen"])

    def test_x34_no_reopen_when_clean_or_merely_open(self):
        self.assertFalse(S.parent_reopen_decision("done", ["done", "done"])["reopen"])
        self.assertFalse(S.parent_reopen_decision("in_progress", ["blocked"])["reopen"])  # 父非收口态
        # 仅"未收口(in_progress/todo)"不强行打回(交 parent_done_child_open 告警)
        self.assertFalse(S.parent_reopen_decision("done", ["in_progress"])["reopen"])
        # 真收口(done_real)不打回(防自燃)
        self.assertFalse(S.parent_reopen_decision("done", ["blocked"], parent_done_real=True)["reopen"])

    def test_x34_watchdog_reopen_on_blocked_child_else_open_alert(self):
        sid = list(W.DEFAULT_SQUADS.values())[0]
        _of = W.fetch_runs
        W.fetch_runs = lambda iid: []
        try:
            # 子任务 blocked(FAIL)→ parent_reopen
            parent = {"id": "p1", "identifier": "PL-P", "status": "done", "assignee_id": sid}
            child_fail = {"id": "c1", "identifier": "PL-C", "status": "blocked",
                          "assignee_id": sid, "parent_issue_id": "p1"}
            k = {a["kind"] for a in W.detect([parent, child_fail], 40.0, 5.0)}
            self.assertIn("parent_reopen", k)
            self.assertNotIn("parent_done_child_open", k)
            ra = next(a for a in W.detect([parent, child_fail], 40.0, 5.0)
                      if a["kind"] == "parent_reopen")
            self.assertEqual(ra["reopen_to"], "in_progress")
            # 子任务仅 in_progress(未收口非FAIL)→ parent_done_child_open(不打回)
            child_open = {"id": "c2", "identifier": "PL-C2", "status": "in_progress",
                          "assignee_id": sid, "parent_issue_id": "p1"}
            k2 = {a["kind"] for a in W.detect([parent, child_open], 40.0, 5.0)}
            self.assertIn("parent_done_child_open", k2)
            self.assertNotIn("parent_reopen", k2)
        finally:
            W.fetch_runs = _of

    # ── X35:backlog→todo 刚提升时给 claim grace window ──
    def test_x35_claim_grace_window(self):
        # 刚从 backlog 提升、转入 3 分钟 < 宽限 10 → 宽限中
        self.assertTrue(S.in_claim_grace("backlog", "2026-06-09T04:25:00Z", self.NOW, 10))
        # 已超宽限 → 不再宽限
        self.assertFalse(S.in_claim_grace("backlog", "2026-06-09T04:10:00Z", self.NOW, 10))
        # 上一轮就是 todo(非 backlog→todo)→ 不给额外宽限
        self.assertFalse(S.in_claim_grace("todo", "2026-06-09T04:25:00Z", self.NOW, 10))
        # 宽限关闭
        self.assertFalse(S.in_claim_grace("backlog", "2026-06-09T04:27:00Z", self.NOW, 0))

    def test_x35_watchdog_suppresses_fresh_todo_no_claim(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW
        sid = list(W.DEFAULT_SQUADS.values())[0]
        issue = {"id": "t1", "identifier": "PL-T", "status": "todo",
                 "assignee_id": sid, "updated_at": "2026-06-09T04:25:00Z"}
        _of, _oc = W.fetch_runs, W.last_comment_age
        W.fetch_runs = lambda iid: []
        W.last_comment_age = lambda iid: 99.0
        try:
            # 刚 backlog→todo:宽限内,无 todo_no_claim
            fresh = W.detect([issue], 40.0, 5.0, status_hist={"t1": "backlog"})
            self.assertNotIn("todo_no_claim", {a["kind"] for a in fresh})
            # 上一轮已是 todo:照常报无人认领
            stale = W.detect([issue], 40.0, 5.0, status_hist={"t1": "todo"})
            self.assertIn("todo_no_claim", {a["kind"] for a in stale})
        finally:
            W.fetch_runs, W.last_comment_age = _of, _oc

    # ── X46:截图上传延迟时不得立即判 evidence_missing ──
    def test_x46_evidence_grace_active(self):
        # 声称截图、完成 3 分钟内、宽限 10 → 宽限中(暂不判无效)
        self.assertTrue(E.evidence_grace_active("2026-06-09T04:25:00Z", self.NOW, 10,
                                                claims_artifact=True))
        # 超 10 分钟 → 不再宽限
        self.assertFalse(E.evidence_grace_active("2026-06-09T04:10:00Z", self.NOW, 10,
                                                 claims_artifact=True))
        # 没声称要传图(纯空结果)→ 不给宽限
        self.assertFalse(E.evidence_grace_active("2026-06-09T04:27:00Z", self.NOW, 10,
                                                 claims_artifact=False))
        self.assertTrue(E.claims_artifact_pending("截图见附件"))
        self.assertFalse(E.claims_artifact_pending("只是代码核查"))

    def test_x46_watchdog_grace_then_alert(self):
        os.environ["WATCHDOG_NOW"] = FX_NOW
        sid = list(W.DEFAULT_SQUADS.values())[0]
        issue = {"id": "e1", "identifier": "PL-E", "status": "in_progress", "assignee_id": sid}
        # 声称截图但无附件、无 URL → 本应 evidence_unverified
        run_recent = [{"id": "r1", "status": "completed", "result": "完成,截图见附件",
                       "created_at": "2026-06-09T04:26:00Z"}]  # 2 分钟前(宽限内)
        run_old = [{"id": "r1", "status": "completed", "result": "完成,截图见附件",
                    "created_at": "2026-06-09T04:10:00Z"}]     # 18 分钟前(超宽限)
        _oc = W.last_comment_age
        W.last_comment_age = lambda iid: 1.0
        try:
            W.fetch_runs = lambda iid: run_recent
            self.assertNotIn("evidence_unverified",
                             {a["kind"] for a in W.detect([issue], 40.0, 5.0)})  # 上传宽限内
            W.fetch_runs = lambda iid: run_old
            self.assertIn("evidence_unverified",
                          {a["kind"] for a in W.detect([issue], 40.0, 5.0)})     # 超宽限照常判
        finally:
            W.last_comment_age = _oc

    # ── X48:证据 hash / 附件 hash 必须可重算 ──
    def test_x48_hash_recomputable_and_stable(self):
        h1 = E.evidence_hash("完成 VERDICT: PASS\n截图见附件")
        h2 = E.evidence_hash("完成 VERDICT: PASS\r\n截图见附件")  # 归一化换行后相同
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertNotEqual(h1, E.evidence_hash("完成 VERDICT: FAIL"))

    def test_x48_attachment_digest_order_independent(self):
        a1 = {"id": "1", "filename": "a.png", "content_type": "image/png", "size_bytes": 10}
        a2 = {"id": "2", "filename": "b.png", "content_type": "image/png", "size_bytes": 20}
        d_ab = E.attachments_digest([a1, a2])["digest"]
        d_ba = E.attachments_digest([a2, a1])["digest"]
        self.assertEqual(d_ab, d_ba)                       # 顺序无关
        self.assertEqual(E.attachments_digest([])["digest"],
                         E.attachments_digest([])["digest"])  # 空集合稳定
        # checksum 优先于元数据
        self.assertEqual(E.attachment_hash({"checksum": "abc"}),
                         E.attachment_hash({"checksum": "abc", "filename": "x.png"}))

    def test_x48_fingerprint_match_and_tamper(self):
        text = "完成 VERDICT: PASS"
        atts = [{"id": "1", "filename": "a.png", "content_type": "image/png", "size_bytes": 10}]
        fp = E.evidence_fingerprint(text, atts)
        self.assertEqual(E.fingerprint_matches(fp, text, atts)["match"], True)
        # 正文被篡改 → 不匹配
        self.assertFalse(E.fingerprint_matches(fp, text + " 改了", atts)["evidence_match"])
        # 附件被替换 → 不匹配
        self.assertFalse(E.fingerprint_matches(
            fp, text, [{"id": "9", "filename": "z.png"}])["attachments_match"])

    def test_x48_ledger_entry_carries_fingerprint(self):
        runs = [{"id": "r1", "status": "completed", "result": "VERDICT: PASS https://x/pull/1",
                 "created_at": "2026-06-09T04:00:00Z"}]
        ledger = E.build_ledger("i1", "PL-1", runs)
        e = ledger["entries"][0]
        self.assertIn("evidence_sha256", e)
        self.assertIn("attachments_sha256", e)
        self.assertEqual(len(e["evidence_sha256"]), 64)

    # ── X61:并发 route 结果顺序不稳定时必须写 sequence id ──
    def test_x61_assign_and_sort_sequence(self):
        jobs = [{"iid": "c"}, {"iid": "a"}, {"iid": "b"}]
        O.assign_sequence_ids(jobs)
        seqs = {j["iid"]: j["seq"] for j in jobs}
        self.assertEqual(seqs, {"a": 1, "b": 2, "c": 3})    # 稳定键排序赋 seq
        # 乱序到达的结果按 seq 复原
        arrived = [{"seq": 3, "iid": "c"}, {"seq": 1, "iid": "a"}, {"seq": 2, "iid": "b"}]
        ordered = O.sort_by_sequence(arrived)
        self.assertEqual([r["seq"] for r in ordered], [1, 2, 3])

    def test_x61_route_jobs_carry_seq(self):
        sid = list(W.DEFAULT_SQUADS.values())[0]
        al = {"ident": "PL-1", "iid": "i1", "squad": sid, "kind": "failed",
              "run_id": "", "why": "x", "agent": ""}
        jobs = W.build_route_jobs([al])
        self.assertTrue(all("seq" in j for j in jobs))
        self.assertTrue(all("gen" in j for j in jobs))   # X23:作业带代际


if __name__ == "__main__":
    unittest.main(verbosity=2)
