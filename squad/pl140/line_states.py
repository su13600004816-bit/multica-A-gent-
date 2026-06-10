#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线机制统一状态机 + 纯函数判定库(BOM 漏洞补齐核心)。

设计原则:本模块**全部是纯函数 / 常量**,不做任何平台写操作、不发网络请求,
所以可以被 line_watchdog.py / line_reset.py 复用,也可以被离线回归测试直接调用。

覆盖 PL-104/PL-105 BOM 对照表:
  BOM-1 统一状态机:见下方 STATES + classify_run / page_evidence_state / gate_state / done_real_state。
  BOM-2 run 结果漏洞:classify_run / is_invalid_completion / consecutive_cancelled。
  BOM-4 页面验收漏洞:scan_evidence / page_evidence_state。
  BOM-5 门禁误触发漏洞:is_gate_misrouted / gate_state。
  BOM-6 收口漏洞:done_real_state(证据不全 → done_real_blocked)。
  BOM-8 调度职责漏洞:ROLES 常量 + describe_roles()。
阶段推进(BOM-3)与告警去重(BOM-7)在 line_watchdog.py 里用本库的判定 + 去重签名实现。
"""
import re
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# BOM-1 统一状态机:所有阶段的合法状态,集中一处枚举(脚本/配置都引用这里,不再各写各的)。
# ─────────────────────────────────────────────────────────────────────────────
STATES = {
    "development": [
        "development_started",
        "development_completed",
        "development_cancelled",
        "development_failed",
    ],
    "audit": [
        "audit_started",
        "audit_pass",
        "audit_fail",
        "audit_cancelled",
    ],
    "gate": [
        "gate_started",
        "gate_pass",
        "gate_fail",
        "gate_misrouted",
        "gate_cancelled",
    ],
    "page_evidence": [
        "page_evidence_required",
        "page_evidence_missing",
        "page_evidence_pass",
        "page_evidence_fail",
    ],
    "done": [
        "done_real_allowed",
        "done_real_blocked",
    ],
}
ALL_STATES = {s for group in STATES.values() for s in group}

# 任一阶段进入以下状态 → 绝不允许自动 done/reset,必须回返工或危机处理(BOM-1 验收)。
BLOCKING_STATES = {
    "development_cancelled", "development_failed",
    "audit_fail", "audit_cancelled",
    "gate_fail", "gate_misrouted", "gate_cancelled",
    "page_evidence_missing", "page_evidence_fail",
    "done_real_blocked",
}


def is_blocking(state):
    """该状态是否阻断收口(不允许 done/reset)。"""
    return state in BLOCKING_STATES


# ─────────────────────────────────────────────────────────────────────────────
# BOM-8 调度职责边界:落到常量,脚本/文档统一引用,避免再次混线。
# ─────────────────────────────────────────────────────────────────────────────
ROLES = {
    "claude_write": {
        "model": "claude", "name": "开发(写/返工)",
        "duty": "写代码/修复/给页面 URL 或阻塞原因。不自审、不自证视觉门禁。",
    },
    "codex_audit": {
        "model": "codex", "name": "专属审计",
        "duty": "实审代码与运行证据,只给 PASS/FAIL。不写业务代码。",
    },
    "qwen_gate": {
        "model": "qwen", "name": "真机/视觉门禁",
        "duty": "读图判定真机/视觉是否真达成,VERDICT PASS/FAIL。不由 Claude 自证。本地 qwen API,不新增 @门禁-Qwen 占位触发。",
    },
    "deepseek_dig": {
        "model": "deepseek", "name": "深挖人",
        "duty": "FAIL 后递增 deepdig 根因+方案,不替代审计、不写代码。",
    },
    "line_brain": {
        "model": "codex", "name": "线主脑",
        "duty": "阶段推进/放行判定,不得停在『我已派出』口头状态。",
    },
    "cc": {
        "model": "claude", "name": "cc 总调度",
        "duty": "拆活/派发/脚本机制落地,不直接写业务代码。",
    },
    "cx": {
        "model": "codex", "name": "cx 审计总管",
        "duty": "只盯审计结果与卡点汇总,不直接写业务代码。",
    },
}


def describe_roles():
    lines = ["职责边界(BOM-8):"]
    for k, v in ROLES.items():
        lines.append("  - %s[%s/%s]:%s" % (k, v["model"], v["name"], v["duty"]))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 通用工具
# ─────────────────────────────────────────────────────────────────────────────
def age_min(ts, now=None):
    """ISO 时间戳距今分钟数;无法解析返回 None。"""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - t).total_seconds() / 60.0


def _txt(*vals):
    return " ".join(str(v) for v in vals if v)


# ─────────────────────────────────────────────────────────────────────────────
# BOM-2 run 结果判定
# ─────────────────────────────────────────────────────────────────────────────
# 哪些 run 算「关键 run」——能代表阶段推进的真实工作(comment/direct 触发的开发/审计/门禁)。
CRITICAL_KINDS = {"comment", "direct"}

# 一个 completed run 必须回写的「必要结果」标记之一,否则视为无效完成。
RESULT_MARKERS = re.compile(
    r"(VERDICT|PASS|FAIL|https?://|截图|screenshot|playwright|"
    r"PR[\s#:]|pull request|阻塞|BLOCK|完成|done|已推送|commit)",
    re.IGNORECASE,
)


def classify_run(run):
    """把一条 run 映射到 development_* 状态(BOM-1/BOM-2)。

    关键:cancelled 且 result 为空 → development_cancelled(不是被忽略的正常态);
    completed 但没有任何必要结果回写 → development_failed(无效完成,不算交付)。
    """
    st = (run.get("status") or "").lower()
    result = run.get("result")
    has_result = bool(result and str(result).strip())
    if st == "running":
        return "development_started"
    if st == "failed":
        return "development_failed"
    if st == "cancelled":
        # 取消且无任何结果回写 = 异常断点(PL-89 f70159f8 样本)
        return "development_cancelled"
    if st == "completed":
        if not has_result or not RESULT_MARKERS.search(str(result)):
            # completed 但缺 VERDICT/PR/截图/URL 等 → 无效完成,等同失败处理
            return "development_failed"
        return "development_completed"
    # todo/queued/其它:还没真正开始
    return "development_started"


def is_invalid_completion(run):
    """completed 但缺必要结果(VERDICT/PR/截图/URL...) → True(BOM-2)。"""
    if (run.get("status") or "").lower() != "completed":
        return False
    result = run.get("result")
    if not (result and str(result).strip()):
        return True
    return not bool(RESULT_MARKERS.search(str(result)))


def critical_runs(runs):
    """按时间正序返回关键 run(comment/direct);kind 缺失时也算关键(保守)。"""
    crit = [r for r in runs if (r.get("kind") or "comment") in CRITICAL_KINDS]
    return sorted(crit, key=lambda r: r.get("created_at") or "")


def latest_critical_run(runs):
    crit = critical_runs(runs)
    return crit[-1] if crit else None


def consecutive_cancelled(runs):
    """从最新往回数,连续 cancelled 的关键 run 数量(BOM-2/BOM-7『连续取消派危机』)。"""
    crit = critical_runs(runs)
    n = 0
    for r in reversed(crit):
        if (r.get("status") or "").lower() == "cancelled":
            n += 1
        else:
            break
    return n


# ─────────────────────────────────────────────────────────────────────────────
# BOM-4 页面 evidence gate
# ─────────────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s)\]}>'\"，。、]+", re.IGNORECASE)
_SHOT_RE = re.compile(r"(截图|screenshot|playwright|浏览器|browser|\.png|\.jpg|\.jpeg|\.webp|attachment|附件图)", re.IGNORECASE)
# 只有这些 = 技术审计,不能直接 DONE_REAL(BOM-4)
_TECH_ONLY_RE = re.compile(r"(build|typecheck|tsc|编译|代码核查|代码审计|路由核查|lint)", re.IGNORECASE)


def scan_evidence(text):
    """扫描一段文字,返回 {has_url, has_screenshot, tech_only}。"""
    t = text or ""
    has_url = bool(_URL_RE.search(t))
    has_shot = bool(_SHOT_RE.search(t))
    tech_only = bool(_TECH_ONLY_RE.search(t)) and not (has_url or has_shot)
    return {"has_url": has_url, "has_screenshot": has_shot, "tech_only": tech_only}


def page_evidence_state(is_page_task, evidence_text, user_counterexample=False):
    """页面/视觉任务的 evidence gate(BOM-4)。

    - 用户反例截图最高优先级 → page_evidence_fail。
    - 非页面任务 → 视为 pass(本 gate 不约束)。
    - 页面任务必须 真实 URL + 截图/浏览器证据,缺则 page_evidence_missing。
    - 只有 build/typecheck/代码审计 → 不算视觉证据,page_evidence_missing。
    """
    if user_counterexample:
        return "page_evidence_fail"
    if not is_page_task:
        return "page_evidence_pass"
    ev = scan_evidence(evidence_text)
    if ev["has_url"] and ev["has_screenshot"]:
        return "page_evidence_pass"
    return "page_evidence_missing"


# ─────────────────────────────────────────────────────────────────────────────
# BOM-5 门禁误触发识别
# ─────────────────────────────────────────────────────────────────────────────
# 占位门禁岗的典型「不能执行」回复 —— 必须判 gate_misrouted,绝不当完成/通过。
_MISROUTE_RE = re.compile(
    r"(不能执行|无法执行|不能冒充|不是千问|不是 ?qwen|误触发|占位|请用 ?API|请走 ?API|"
    r"请调用.*API|cannot (run|execute)|placeholder|not authorized|无权)",
    re.IGNORECASE,
)
_VERDICT_PASS_RE = re.compile(r"VERDICT[:：\s]*PASS", re.IGNORECASE)
_VERDICT_FAIL_RE = re.compile(r"VERDICT[:：\s]*FAIL", re.IGNORECASE)


def is_gate_misrouted(text):
    """门禁回复是占位岗『不能执行/误触发/请用API』 → True(BOM-5)。"""
    t = text or ""
    if _MISROUTE_RE.search(t):
        # 即便文中带了 PASS 字样,占位岗的不能执行优先判误触发
        return True
    return False


def gate_state(gate_text, evidence_present=True):
    """把一段门禁回复映射到 gate_* 状态(BOM-1/BOM-5)。"""
    t = gate_text or ""
    if not t.strip():
        return "gate_started"  # 还没拿到门禁结果
    if is_gate_misrouted(t):
        return "gate_misrouted"
    if not evidence_present:
        # 无截图/URL 的视觉门禁直接 FAIL(BOM-4 配合)
        return "gate_fail"
    if _VERDICT_FAIL_RE.search(t):
        return "gate_fail"
    if _VERDICT_PASS_RE.search(t):
        return "gate_pass"
    # 有回复但没有规范 VERDICT 行 → 不认作通过
    return "gate_fail"


# ─────────────────────────────────────────────────────────────────────────────
# BOM-3 阶段推进(deadline / 状态机驱动):正常阶段完成后,下一环节必须在
# deadline 内出现;否则即便上一阶段是「正常完成」也要告警/重派。
#
# 旧版 stage_stale 只覆盖「最新 run cancelled/failed 的卡死」,漏掉了
# 「上一阶段正常完成但下一环节不自动推进」这条铁律(审计 FAIL 阻断点)。
# 这里把流水线建成状态机:每个『阶段产出事件』映射到一个『下一环节 + deadline』,
# 由 line_watchdog.py 在每轮扫描时按 deadline 判定是否超时未推进。
# ─────────────────────────────────────────────────────────────────────────────
# 真门禁标记:用强标记(【门禁/门禁 Qwen/qwen/千问/gate),避免审计文里『可进门禁』
# 这类顺带提及被误判成门禁回写。真门禁由 line_bridge.py 回写为『【门禁 Qwen】...』。
_GATE_PHASE_RE = re.compile(r"(【门禁|门禁\s*qwen|\bqwen\b|千问|\bgate\b)", re.IGNORECASE)
_AUDIT_PHASE_RE = re.compile(r"(专属审计|代码审计|审计\s*codex|\baudit\b|审计\s*VERDICT|审计结论)", re.IGNORECASE)
_PAGE_PHASE_RE = re.compile(r"(页面|画布|视觉|canvas|browser|playwright|真机)", re.IGNORECASE)
_PR_PHASE_RE = re.compile(r"(PR\s*[#:]|/pull|pull request|已推送|已\s*push|已合并|交付)", re.IGNORECASE)

# 阶段产出事件 → (下一环节标签, 超时原因模板)。{d}=deadline 分钟。
# 状态机铁律:只要进入这些事件,下一环节就必须在 deadline 内出现一个新 run / 状态变化。
STAGE_EXPECTATION = {
    "dev_done":   ("审计", "开发交付完成(in_review)后 {d} 分钟内未启动专属审计 run"),
    "audit_pass": ("门禁", "专属审计 PASS 后 {d} 分钟内未跑真门禁(line_bridge.py gate)"),
    "gate_pass":  ("收口", "门禁 PASS 后 {d} 分钟内未 done/reset 收口"),
    "gate_fail":  ("返工", "门禁 FAIL 后 {d} 分钟内未派返工 run"),
    "audit_fail": ("返工", "专属审计 FAIL 后 {d} 分钟内未派返工 run"),
    "page_fail":  ("返工", "页面视觉 FAIL 后 {d} 分钟内未派返工 run"),
}


def classify_event(text):
    """把一段 run/门禁回写文本映射到流水线阶段事件(BOM-3)。

    返回 STAGE_EXPECTATION 的键之一,或 'gate_misrouted'(占位岗,另有专属告警),
    或 None(非阶段产出/还在进行)。

    注意:这里用于**推进检测**(下一环节是否启动),与 done_gate 的严格收口判定
    是两件事 —— 即便某 run 自证『门禁 PASS』,这里也只用它来追问『那 gate/收口呢』;
    真正放行收口仍由 line_done_gate.py 只认 line_bridge.py 的真门禁回写。
    """
    t = text or ""
    if not t.strip():
        return None
    gate = bool(_GATE_PHASE_RE.search(t))
    audit = bool(_AUDIT_PHASE_RE.search(t))
    pass_ = bool(_VERDICT_PASS_RE.search(t))
    fail_ = bool(_VERDICT_FAIL_RE.search(t))
    if gate and is_gate_misrouted(t):
        return "gate_misrouted"
    if gate:
        if fail_:
            return "gate_fail"
        if pass_:
            return "gate_pass"
    if audit:
        if fail_:
            return "audit_fail"
        if pass_:
            return "audit_pass"
    if _PAGE_PHASE_RE.search(t) and fail_:
        return "page_fail"
    # 开发交付:有 PR/URL 证据且不是门禁/审计回写
    if (_PR_PHASE_RE.search(t) or _URL_RE.search(t)) and not gate and not audit:
        return "dev_done"
    return None


def stage_progress_overdue(event, signal_at, has_newer_run, now,
                           deadline_min, issue_done=False):
    """deadline 驱动的下一环节推进判定(BOM-3 核心,纯函数,可单测)。

    入参:
      event          : 最新『阶段产出事件』(classify_event 的结果)
      signal_at      : 该事件发生时间(ISO)
      has_newer_run  : 该事件之后是否已有更新的 run(=下一环节已启动 → 不算超时)
                       注意:**只认新 run / 状态变化,不认评论** —— 防『我已派出』口头状态。
      now            : 当前时间(datetime)
      deadline_min   : 下一环节必须在多少分钟内出现
      issue_done     : issue 是否已 done(gate_pass→done 已完成则不再告警)

    返回 dict(event, next, elapsed, reason) 若超时未推进;否则 None。
    """
    if event not in STAGE_EXPECTATION:
        return None
    if event == "gate_pass" and issue_done:
        return None          # 门禁 PASS 且已收口,无需告警
    if has_newer_run:
        return None          # 下一环节已经有新 run 在动,未停滞
    elapsed = age_min(signal_at, now)
    if elapsed is None or elapsed <= deadline_min:
        return None          # 还在 deadline 内,给推进留时间
    nxt, tmpl = STAGE_EXPECTATION[event]
    return {"event": event, "next": nxt, "elapsed": elapsed,
            "reason": tmpl.format(d=int(deadline_min))}


# ─────────────────────────────────────────────────────────────────────────────
# U21/U22 typed 阶段事件 + role-stage 状态字段
#
# 旧实现只用 classify_event(正则匹配 run 文本)推断阶段 —— 文本一改格式就漏判,
# 也无法把"当前处于哪个 role/阶段"落成可持久化的状态字段。这里:
#  U21:优先读 run 上的**结构化(typed)字段**(event/stage/phase + verdict),
#       只有结构化缺失时才回退到 classify_event 正则,避免纯文本脆弱匹配。
#  U22:把阶段事件归一到 role,供 line_watchdog 写进 state 的 stage_state 字段
#       (典型 role/阶段不再每轮从文本重算,可追溯、可比对回退)。
# ─────────────────────────────────────────────────────────────────────────────
# 阶段产出事件(+占位岗误触发)→ 负责该阶段的 role(见 ROLES)。
STAGE_ROLE = {
    "dev_done": "claude_write",
    "audit_pass": "codex_audit",
    "audit_fail": "codex_audit",
    "gate_pass": "qwen_gate",
    "gate_fail": "qwen_gate",
    "gate_misrouted": "qwen_gate",
    "page_fail": "qwen_gate",
}
_TYPED_EVENTS = set(STAGE_EXPECTATION) | {"gate_misrouted"}


def role_for_event(event):
    """阶段事件 → role 键(STAGE_ROLE),未知事件返回 None。"""
    return STAGE_ROLE.get(event)


def compose_stage_verdict(stage, verdict):
    """把结构化的 (stage, verdict) 组合成阶段事件键。无法识别返回 None。"""
    s = (stage or "").strip().lower()
    v = (verdict or "").strip().lower()
    v_pass = v in ("pass", "passed", "ok", "success", "succeeded")
    v_fail = v in ("fail", "failed", "blocked", "reject", "rejected")
    if s in ("development", "dev", "develop", "开发"):
        if v in ("done", "completed", "complete", "delivered", "交付") or v_pass:
            return "dev_done"
    if s in ("audit", "review", "审计", "专属审计"):
        if v_pass:
            return "audit_pass"
        if v_fail:
            return "audit_fail"
    if s in ("gate", "门禁"):
        if v_pass:
            return "gate_pass"
        if v_fail:
            return "gate_fail"
    if s in ("page", "page_evidence", "visual", "视觉", "真机"):
        if v_fail:
            return "page_fail"
    return None


def typed_event_from_fields(run):
    """U21:只从 run 的结构化字段读阶段事件(不碰文本正则),读不到返回 None。

    识别两种结构:
      ① 直接事件字段 run["event"]/["stage_event"]/["phase_event"] 或 result.event,
         值须是合法阶段事件键(STAGE_EXPECTATION ∪ {gate_misrouted});
      ② stage/phase + verdict 组合字段(compose_stage_verdict)。
    """
    if not isinstance(run, dict):
        return None
    res = run.get("result")
    src = res if isinstance(res, dict) else {}
    for holder in (run, src):
        for key in ("event", "stage_event", "phase_event"):
            v = holder.get(key)
            if isinstance(v, str) and v.strip().lower() in _TYPED_EVENTS:
                return v.strip().lower()
    stage = run.get("stage") or run.get("phase") or src.get("stage") or src.get("phase")
    verdict = run.get("verdict") or src.get("verdict")
    return compose_stage_verdict(stage, verdict)


def event_from_run(run, fallback_text=""):
    """U21:返回 (event, typed)。优先结构化字段(typed=True),否则回退正则(typed=False)。"""
    ev = typed_event_from_fields(run)
    if ev:
        return ev, True
    return classify_event(fallback_text), False


def clock_skew_alert(now, server_timestamps, tolerance_min=2.0):
    """U08:本地时钟相对平台时间戳的偏移探测(纯函数)。

    看门狗所有 age/zombie/deadline 判定都用本地 now 减平台回写的 created_at/updated_at。
    若本地时钟落后于平台(或平台时钟超前),会出现"未来时间戳":server_ts > now。
    取所有 server 时间戳里最新的一条,若它比 now 还晚出 tolerance 分钟以上 → 时钟偏移,
    age 计算不可信(僵尸/卡死会被低估、推进 deadline 会误判)。

    返回 (skewed: bool, why: str, skew_min: float|None)。
    只判可靠方向(server 在未来):本地领先所有 server 数据无法与"近期无活动"区分,不误报。
    """
    latest = None
    for ts in server_timestamps or []:
        m = age_min(ts, now)          # 正=过去,负=未来(server 比 now 晚)
        if m is None:
            continue
        if latest is None or m < latest:
            latest = m                # 取最"未来"的一条(age 最小/最负)
    if latest is None:
        return False, "", None
    if latest < -abs(tolerance_min):
        skew = -latest
        return True, ("本地时钟疑似落后平台 %.1f 分钟(存在未来时间戳的 run/issue),"
                      "age/僵尸/卡死/deadline 判定将被低估,需校时(NTP)后再信任时间类告警" % skew), skew
    return False, "", None


# ─────────────────────────────────────────────────────────────────────────────
# X34  L3 状态机 × L5 路由续派:父任务已 done 但子任务 FAIL → 必须自动打回父任务
#
# 旧的 parent_done_child_open 只『告警』父子矛盾,停在口头。X34 铁律:父任务被收口
# (done/cancelled)后,若发现子任务处于失败/阻塞/未收口态,父任务的『已完成』是假的,
# 必须自动把父任务打回 in_progress 重做(符合 CLAUDE.md 状态纪律:FAIL→in_progress,
# 严禁 done/cancelled 当返工)。这里给出纯判定;真正的 status 回退动作由 watchdog 执行。
# ─────────────────────────────────────────────────────────────────────────────
# 子任务的"失败/未收口"态:这些都说明父任务不该是 done。
CHILD_FAIL_STATES = {"blocked"}          # 平台层显式失败/阻塞态
CHILD_OPEN_STATES = {"todo", "in_progress", "in_review"}  # 仍在进行,父不该已完成
CHILD_DONE_STATES = {"done", "cancelled"}


def parent_reopen_decision(parent_status, child_states, parent_done_real=False,
                           child_failed_runs=False):
    """X34:父任务是否需要被自动打回(reopen)。

    入参:
      parent_status     : 父任务当前状态
      child_states      : 子任务状态列表
      parent_done_real  : 父任务是否已过 done_gate 真收口(真收口不打回,防自燃)
      child_failed_runs : 是否有子任务最新关键 run 为 failed/cancelled(更强的 FAIL 信号)

    规则:父在 {done, cancelled} 且未真收口,且存在子任务处于 FAIL(blocked / 失败 run)
    或仍 OPEN(todo/in_progress/in_review)→ reopen 到 in_progress。
    返回 {reopen: bool, to_status, reason, fail_children, open_children}。
    """
    states = list(child_states or [])
    fail_children = [s for s in states if s in CHILD_FAIL_STATES]
    open_children = [s for s in states if s in CHILD_OPEN_STATES]
    reopen = False
    reason = ""
    # X34 只在『子任务 FAIL』时自动打回(blocked / 子任务最新关键 run failed-cancelled)。
    # 仅『子任务未收口(in_progress/todo/in_review)』属父子矛盾,由 parent_done_child_open
    # 单独告警提醒线主脑,不强行打回父任务(避免把"正在干的子任务"当失败误打回)。
    if parent_status in CHILD_DONE_STATES and not parent_done_real:
        if child_failed_runs or fail_children:
            reopen = True
            reason = ("父任务已 %s,但有子任务 FAIL(blocked/失败run),父『完成』为假,"
                      "自动打回 in_progress 重做" % parent_status)
    return {
        "reopen": reopen,
        "to_status": "in_progress" if reopen else None,
        "reason": reason,
        "fail_children": fail_children,
        "open_children": open_children,
    }


# ─────────────────────────────────────────────────────────────────────────────
# X35  L3 状态机 × L6 并发时序:状态刚从 backlog → todo 时必须给 claim grace window
#
# 任务刚被 promote(backlog→todo)的瞬间,认领/起 run 需要时间。旧逻辑 todo 无 run 且
# age>stale_min 立刻报 todo_no_claim,会把"刚放出来还没来得及接"误判成"无人认领"。
# 这里给一个 claim grace 窗口:从 backlog 转入 todo 起的 grace_min 分钟内,抑制 no_claim 告警。
# ─────────────────────────────────────────────────────────────────────────────
def in_claim_grace(prev_status, transition_at, now, grace_min):
    """X35:某 todo 任务是否仍在『刚从 backlog 提升』的认领宽限窗口内。

    入参:
      prev_status   : 上一轮该 issue 的状态(看门狗 state 里记的);None=没历史
      transition_at : 进入 todo 的时间(ISO,通常用 issue.updated_at);None=不可解析
      now           : 当前时间
      grace_min     : 宽限分钟数

    判定为宽限中(返回 True)需同时:上一轮是 backlog(确属 backlog→todo 转变)
    且距转入时间未超过 grace_min。无 prev 历史时,保守起见也按『转入时间在 grace 内』给宽限,
    避免看门狗刚上线/state 丢失时把所有新 todo 误判无人认领。
    """
    if grace_min is None or grace_min <= 0:
        return False
    elapsed = age_min(transition_at, now)
    if elapsed is None or elapsed > grace_min:
        return False
    # 有历史:必须是 backlog→todo 才算"刚提升";其它来源(本就 todo)不给额外宽限。
    if prev_status is not None and prev_status != "backlog":
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# BOM-6 DONE_REAL 收口判定
# ─────────────────────────────────────────────────────────────────────────────
def done_real_state(facts):
    """收口铁律(BOM-6):必须同时满足所有条件才允许 done/reset,否则 blocked。

    facts 期望键(缺省按未通过处理,保守阻断):
      development_completed: bool   开发交付完成
      audit_pass: bool              专属审计 PASS
      gate_pass: bool               Qwen/DeepSeek 门禁 PASS
      is_page_task: bool            是否页面/视觉任务
      page_evidence_pass: bool      页面任务真实视觉 evidence PASS(非页面任务可忽略)
      user_counterexample: bool     用户最新反例截图(有则一票否决)

    返回 (state, reasons[])。state ∈ {done_real_allowed, done_real_blocked}。
    """
    reasons = []
    if facts.get("user_counterexample"):
        reasons.append("用户最新反例截图存在,一票否决")
    if not facts.get("development_completed"):
        reasons.append("开发交付未完成")
    if not facts.get("audit_pass"):
        reasons.append("专属审计未 PASS")
    if not facts.get("gate_pass"):
        reasons.append("Qwen/DeepSeek 门禁未 PASS")
    if facts.get("is_page_task") and not facts.get("page_evidence_pass"):
        reasons.append("页面任务缺真实视觉 evidence(URL+截图)")
    if reasons:
        return "done_real_blocked", reasons
    return "done_real_allowed", []


# ─────────────────────────────────────────────────────────────────────────────
# PL-124 P2:主机级健康 / 权限 / 资源 / 版本探针(纯函数,离线可测)
# 真实采样由 line_watchdog 注入;这里只做判定,便于 fixture 单测、不触真 proc/df/网络。
# ─────────────────────────────────────────────────────────────────────────────
def permission_drift_alert(cli_ok, file_facts, expected_user="fleet"):
    """U15 + U70:multica CLI 权限 + root/fleet 文件权限漂移探针。

    cli_ok: multica CLI 本轮是否可调用(False=issue/run 读取与派单都会失败 → 看门狗会假绿)。
    file_facts: [{"path","exists","owner","writable"}...] 关键脚本/状态文件采样。
    返回 [(kind, label, why)...]。
      U15 → cli_permission_drift(CLI 失效,读取/派单全失败);
      U70 → permission_drift(关键文件缺失 / 属主漂移 / fleet 不可写,cron 停摆)。
    """
    out = []
    if cli_ok is False:
        out.append(("cli_permission_drift", "WATCHDOG-PERM",
                    "multica CLI 不可调用:本轮所有 issue/run 读取与派单都会失败,"
                    "看门狗可能输出假健康态,需先修复 CLI 权限/登录再信任本轮结果"))
    for f in file_facts or []:
        path = f.get("path", "?")
        if not f.get("exists", True):
            out.append(("permission_drift", "WATCHDOG-PERM",
                        "关键文件缺失:%s(被移走/删除会让 cron 巡查或状态持久化停摆)" % path))
            continue
        owner = f.get("owner")
        if owner and expected_user and owner not in (expected_user, "root"):
            out.append(("permission_drift", "WATCHDOG-PERM",
                        "关键文件属主漂移:%s 属主=%s(期望 %s 或 root),cron 可能无法读写"
                        % (path, owner, expected_user)))
        if f.get("writable") is False:
            out.append(("permission_drift", "WATCHDOG-PERM",
                        "关键文件 fleet 不可写:%s,看门狗无法更新状态/落盘,会反复重报或丢状态" % path))
    return out


def resource_pressure_alert(mem_avail_pct, disk_used_pct,
                            mem_floor_pct=8.0, disk_ceil_pct=90.0):
    """U59 + U60:内存 / 磁盘压力探针(纯函数)。

    mem_avail_pct 低于 floor → 机器无力干活(OOM/偷跑/假工作);
    disk_used_pct 达到 ceil → 日志/cache/.bak 积压拖垮服务。返回 [(kind,label,why)...]。
    """
    out = []
    if mem_avail_pct is not None and mem_avail_pct < mem_floor_pct:
        out.append(("memory_pressure", "WATCHDOG-RES",
                    "可用内存仅 %.1f%%(低于 %.0f%% 阈值):agent 可能 OOM/偷跑/假工作,"
                    "需排查吃内存进程或扩容" % (mem_avail_pct, mem_floor_pct)))
    if disk_used_pct is not None and disk_used_pct >= disk_ceil_pct:
        out.append(("disk_pressure", "WATCHDOG-RES",
                    "磁盘已用 %.1f%%(达 %.0f%% 阈值):日志/cache/.bak 积压会拖垮服务,"
                    "需按白名单清理(*.bak.*、*.backup.*、*.log.[1-9]、__pycache__、临时 ledger)"
                    % (disk_used_pct, disk_ceil_pct)))
    return out


def cleanup_candidates(entries, whitelist_globs=None):
    """U60 辅助:从目录清单挑出白名单内可安全清理的文件(纯函数,只挑不删)。

    entries: [{"name","size"}...];只回名字匹配白名单模式的项,绝不碰源码/状态文件。
    """
    import fnmatch
    globs = whitelist_globs or ("*.bak.*", "*.backup.*", "*.bak-*",
                                "*.log.[1-9]", "*.tmp.*", "*.tmp", "*.pyc")
    out = []
    for e in entries or []:
        n = e.get("name", "")
        if any(fnmatch.fnmatch(n, g) for g in globs):
            out.append(e)
    return out


def memory_cleanup_due(last_cleanup_ts, now, interval_hours=2.0):
    """U61:审计/深挖上下文记忆清理是否到期(每 interval_hours 一次)。

    last_cleanup_ts: 上次清理 ISO 时间戳(None/不可解析=到期)。
    返回 (due: bool, elapsed_hours: float|None, why: str)。
    上下文记忆不定期清理会爆掉 → 后续审计/深挖空转或假工作。
    """
    if not last_cleanup_ts:
        return True, None, "上下文记忆从未清理(无 marker),已到期(首轮)"
    m = age_min(last_cleanup_ts, now)
    if m is None:
        return True, None, "上次清理时间戳不可解析,按到期处理"
    hrs = m / 60.0
    if hrs >= interval_hours:
        return True, hrs, "上下文记忆距上次清理已 %.1fh(≥%.1fh),到期需清理" % (hrs, interval_hours)
    return False, hrs, ""


def cli_version_drift_alert(observed, pinned):
    """U68:Multica CLI 版本 pin 漂移探针(纯函数)。

    observed: 实测 `multica --version` 首行;pinned: 期望 pin 值(env 注入)。
    pinned 空 → 未 pin(漏洞本身,提示去 pin);observed≠pinned → schema 可能变,告警做兼容测试。
    返回 [(kind,label,why)...]。
    """
    out = []
    obs = (observed or "").strip()
    pin = (pinned or "").strip()
    if not pin:
        out.append(("cli_version_unpinned", "WATCHDOG-CLIVER",
                    "Multica CLI 版本未 pin(WATCHDOG_CLI_VERSION_PIN 未设):CLI 升级改 schema 时"
                    "脚本会误判,建议固定到当前已验证版本『%s』" % (obs or "未知")))
        return out
    if obs and obs != pin:
        out.append(("cli_version_drift", "WATCHDOG-CLIVER",
                    "Multica CLI 版本漂移:实测『%s』≠ pin『%s』,输出 schema 可能已变,"
                    "需做兼容测试再放行" % (obs, pin)))
    return out


def proxy_health_alert(proxies, prober):
    """U69:美国代理/网络出口健康探针。

    proxies: [{"name","url"}...](从 HTTPS_PROXY/HTTP_PROXY 取);prober(url)->(ok, info)。
    出口不通 → 外部 API/模型访问失败,看门狗无法区分是任务问题还是网络问题(门禁/深挖假故障)。
    返回 [(kind,label,why)...]。
    """
    out = []
    seen = set()
    for p in proxies or []:
        url = p.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        ok, info = prober(url)
        if not ok:
            out.append(("proxy_down", "WATCHDOG-PROXY",
                        "网络出口/代理不可用:%s(%s),外部 API/模型访问会失败,"
                        "外部 BLOCKED 与任务异常无法区分" % (p.get("name") or url, info)))
    return out


def image_oversize_alert(attachments, max_bytes=2 * 1024 * 1024):
    """U71:图片压缩 / token 成本保护探针(纯函数)。

    attachments: [{"filename","content_type","size_bytes"}...];超阈值图片应压缩后再传。
    聚合成一条告警(列出前几张超限图),避免逐图刷屏。返回 [(kind,label,why)...]。
    """
    big = []
    for a in attachments or []:
        ct = (a.get("content_type") or "")
        name = (a.get("filename") or "")
        is_img = ct.startswith("image/") or name.lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"))
        size = a.get("size_bytes") or 0
        if is_img and size > max_bytes:
            big.append((name or str(a.get("id", "?")), size))
    if not big:
        return []
    desc = ",".join("%s(%.1fMB)" % (n, b / 1024 / 1024) for n, b in big[:5])
    more = "" if len(big) <= 5 else " 等 %d 张" % len(big)
    return [("image_oversize", "WATCHDOG-IMG",
             "存在超阈值未压缩图片(>%.1fMB):%s%s,直接上传会持续吃 token,需压缩/缩放后再传"
             % (max_bytes / 1024 / 1024, desc, more))]


# ─────────────────────────────────────────────────────────────────────────────
# 简单 CLI:打印状态机 / 职责边界(供文档/排查引用)
# ─────────────────────────────────────────────────────────────────────────────
def _main(argv):
    if len(argv) >= 2 and argv[1] == "roles":
        print(describe_roles())
        return 0
    if len(argv) >= 2 and argv[1] == "states":
        for group, ss in STATES.items():
            print("%s: %s" % (group, ", ".join(ss)))
        print("blocking: %s" % ", ".join(sorted(BLOCKING_STATES)))
        return 0
    print("用法: line_states.py [roles|states]")
    print("(本模块主要作为库被 line_watchdog.py / line_reset.py import)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
