#!/usr/bin/env python3
"""line_partial.py — PL-140 X PARTIAL 16 项复核补齐(纯函数库)。

背景:PL-126 把 X01-X72 逐项核对,其中 16 项判 PARTIAL = "检测/库就绪但有真实残缺"。
本模块把这些残缺里**脚本可独立闭环**的那一半补成纯函数 + 离线单测;真正需平台/凭据/
外部通道的另一半在 PL-140 结论里如实标 BLOCKED,不在此假装闭环。

为什么单独成文件:PL-140 复核期间 line_observe.py / line_watchdog.py / line_evidence.py
正被并行任务(PL-139 X FAIL 补齐)改动,新增逻辑落到独立模块可避免并发改同一文件互相
覆盖;待并发收口后由收口角色按需 merge 进 line_observe(每个函数注明归属层)。

设计同 line_observe / line_evidence:全部纯函数 + 可注入依赖,import 无副作用,离线可单测。

覆盖(对应 PL-126 PARTIAL 的"剩余风险"):
  X08  route 前重读 assignee 防 TOCTOU
  X11  schema drift 落独立证据文件
  X14  覆盖截断"预计漏扫数量"量化
  X24/X27  按成员维度统计每线人数/工作/闲置/卡死
  X38  阶段转换 from/to/event/source 逐轮持久化
  X53  watchdog/dispatch 跨脚本统一事务锁
  X54  从 CLI 输出自动探测 rate-limit(不再只靠注入)
  X63  心跳停摆 → 外部 watcher 可消费的告警
  X67  watchdog_state / status.json / heartbeat.json 三者互校验
  X71  状态来源防篡改签名(HMAC)
  X72  P0 告警人工确认(ack)闭环账本(外部通知通道仍 BLOCKED)
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone

try:  # 复用既有纯函数,避免逻辑漂移
    import line_observe as _O
    _canonical = _O._canonical
    _iso = _O._iso
    _parse_ts = _O._parse_ts
    verify_state = _O.verify_state
    heartbeat_stale = _O.heartbeat_stale
except Exception:  # pragma: no cover - 独立导入兜底
    _O = None

    def _canonical(payload):
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _iso(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def _parse_ts(ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return None

    def verify_state(d):
        return False

    def heartbeat_stale(hb, now, threshold_min):
        if not hb:
            return True, None
        ts = _parse_ts(hb.get("last_successful_scan") or hb.get("ts"))
        if ts is None:
            return True, None
        age = (now - ts).total_seconds() / 60.0
        return age > threshold_min, age

try:
    import line_evidence as _E
    _severity_of = _E.severity_of
except Exception:  # pragma: no cover
    _severity_of = lambda kind: "medium"


# ── X14:覆盖截断时把"预计漏扫数量"量化 ───────────────────────────────────────
def coverage_estimate(returned, limit, total_hint=None):
    """X14:量化覆盖截断的影响范围 + 预计漏扫数量。

    returned   = 本轮实际取到的 issue 数
    limit      = 本轮请求硬上限(WATCHDOG_ISSUE_LIMIT)
    total_hint = 可选,二次 count 探测拿到的真实总数;有则精确,无则给保守下界。

    返回 {truncated, returned, limit, est_missed, exact, note}。
    截断判据:returned >= limit(正好抓满一页 → 极可能还有更多)。
    """
    returned = int(returned or 0)
    limit = int(limit or 0)
    truncated = limit > 0 and returned >= limit
    if not truncated:
        return {"truncated": False, "returned": returned, "limit": limit,
                "est_missed": 0, "exact": True, "note": "未截断,全量已覆盖"}
    if total_hint is not None:
        miss = max(0, int(total_hint) - returned)
        return {"truncated": True, "returned": returned, "limit": limit,
                "est_missed": miss, "exact": True,
                "note": "真实总数 %d,已覆盖 %d,精确漏扫 %d" % (int(total_hint), returned, miss)}
    return {"truncated": True, "returned": returned, "limit": limit,
            "est_missed": 1, "exact": False,
            "note": "已覆盖 %d 条(满上限 %d),预计漏扫 ≥1 条;接 count 探测可精确量化" % (returned, limit)}


# ── X11:schema drift 落独立证据文件 ──────────────────────────────────────────
def schema_drift_evidence(unknown_states, known_states, now=None):
    """X11:把检出的 schema 漂移(出现未登记的 issue 状态)规整成证据记录。纯函数。"""
    now = now or datetime.now(timezone.utc)
    unknown = sorted(set(unknown_states or []))
    return {
        "ts": _iso(now),
        "kind": "schema_drift",
        "unknown_states": unknown,
        "known_states": sorted(set(known_states or [])),
        "drift_count": len(unknown),
        "why": "出现未登记的 issue 状态 %s,脚本枚举可能漏扫该类任务,需登记或由平台 schema 补全" % unknown,
    }


def write_schema_drift_evidence(evidence, evidence_dir, now=None):
    """X11:把 schema_drift 证据落到独立文件 <dir>/schema_drift_<ts>.json(可追溯)。
    与告警分离:告警刷屏会被轮转,独立证据文件永久留底。返回路径或 None。"""
    if not evidence or not evidence.get("unknown_states"):
        return None
    now = now or datetime.now(timezone.utc)
    try:
        os.makedirs(evidence_dir, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        path = os.path.join(evidence_dir, "schema_drift_%s.json" % stamp)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(evidence, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


# ── X38:阶段转换记 from/to/event/source,逐轮持久化 ──────────────────────────
def stage_transition_entry(ident, iid, from_stage, to_stage, event, source, now=None):
    """X38:把一次阶段转换规整成可追溯记录(from/to/event/source/ts)。纯函数。"""
    now = now or datetime.now(timezone.utc)
    return {"ts": _iso(now), "ident": ident, "iid": iid, "from": from_stage,
            "to": to_stage, "event": event, "source": source}


def diff_stage_state(prev_stage_state, new_stage_state, now=None):
    """X38:对比上一轮与本轮 stage_state({iid: {stage, ident, source, event}}),
    产出本轮发生的阶段转换列表(from != to 才算一次转换)。供逐轮持久化。"""
    prev = prev_stage_state or {}
    new = new_stage_state or {}
    out = []
    for iid, cur in new.items():
        cur = cur or {}
        old = prev.get(iid) or {}
        frm, to = old.get("stage"), cur.get("stage")
        if to and frm != to:
            out.append(stage_transition_entry(
                cur.get("ident") or old.get("ident"), iid, frm, to,
                cur.get("event") or "stage_change", cur.get("source") or "watchdog", now))
    return out


def append_jsonl(entries, path, keep=500):
    """原子追加若干 JSON 记录到 path(.jsonl,滚动保留最近 keep 行)。返回路径或 None。
    X38 阶段转换 / X72 ack 账本等"逐轮落盘"复用此追加器。"""
    if not path or not entries:
        return None
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        for e in entries:
            lines.append(json.dumps(e, ensure_ascii=False))
        if keep and len(lines) > keep:
            lines = lines[-keep:]
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


# ── X54:从 CLI 输出自动探测 rate-limit 信号 ──────────────────────────────────
_RATE_LIMIT_RE = re.compile(
    r"(429\b|rate[ _-]?limit|ratelimit|too many requests|rate exceeded|"
    r"quota exceeded|请求过于频繁|访问频繁|限流)", re.IGNORECASE)


def detect_rate_limit(*texts):
    """X54:在 CLI stdout/stderr 文本里探测 rate-limit 信号。任一命中→True。"""
    for t in texts:
        if t and _RATE_LIMIT_RE.search(str(t)):
            return True
    return False


def rate_limited_signal(env, *texts):
    """X54:综合判定本轮是否受限 —— 环境注入(WATCHDOG_RATE_LIMITED)或自动探测命中。
    供 line_observe.adaptive_workers(base, rate_limited_signal(env, stderr)) 直接消费。"""
    if str((env or {}).get("WATCHDOG_RATE_LIMITED", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    return detect_rate_limit(*texts)


# ── X67:watchdog_state / status.json / heartbeat.json 三者互校验 ─────────────
def cross_validate(state, status, heartbeat, ts_skew_min=10.0):
    """X67:三份持久化产物互校验,返回不一致项列表(空=一致)。

    校验:1) state checksum 自洽;2) status.ts 与 heartbeat.ts 时间接近(同一轮);
    3) status.scan_state=full 但 heartbeat.ok=False → 自相矛盾;4) 缺失/无时间戳各记一条。
    供外部监督/审计判"看门狗自报状态是否可信"。
    """
    issues = []
    if state is None:
        issues.append("watchdog_state 缺失")
    elif isinstance(state, dict) and "checksum" in state and not verify_state(state):
        issues.append("watchdog_state checksum 不符(文件损坏或被篡改)")
    if status is None:
        issues.append("status.json 缺失")
    if heartbeat is None:
        issues.append("heartbeat.json 缺失")
    s_ts = _parse_ts((status or {}).get("ts"))
    h_ts = _parse_ts((heartbeat or {}).get("ts"))
    if status is not None and s_ts is None:
        issues.append("status.json 无有效 ts")
    if heartbeat is not None and h_ts is None:
        issues.append("heartbeat.json 无有效 ts")
    if s_ts and h_ts:
        skew = abs((s_ts - h_ts).total_seconds()) / 60.0
        if skew > ts_skew_min:
            issues.append("status 与 heartbeat 时间相差 %.1f 分钟(>%.0f),疑似有一份未更新" % (skew, ts_skew_min))
    if status is not None and heartbeat is not None:
        if status.get("scan_state") == "full" and heartbeat.get("ok") is False:
            issues.append("status 标 full 扫描但 heartbeat.ok=False,自相矛盾")
    return issues


# ── X71:状态来源防篡改签名(HMAC,可追溯且可校验) ──────────────────────────
def _sign_body(payload):
    return {k: v for k, v in (payload or {}).items() if k != "sig"}


def sign_payload(payload, key):
    """X71:对 payload(去掉 sig 字段)做 HMAC-SHA256,返回十六进制签名。"""
    body = _canonical(_sign_body(payload))
    return hmac.new(str(key).encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_status(payload, key):
    """X71:返回带 sig 字段的新 payload(原 payload 不变)。key 为空则原样返回(签名关闭)。"""
    if not key:
        return dict(payload or {})
    out = dict(payload or {})
    out.pop("sig", None)
    out["sig"] = sign_payload(out, key)
    return out


def verify_payload_signature(payload, key):
    """X71:校验 payload 的 sig 是否由 key 签出(防篡改)。无 sig/无 key/被改 → False。"""
    if not key or not isinstance(payload, dict):
        return False
    sig = payload.get("sig")
    if not sig:
        return False
    return hmac.compare_digest(str(sig), sign_payload(payload, key))


def load_sign_key(env, path_env="WATCHDOG_SIGN_KEY_FILE", inline_env="WATCHDOG_SIGN_KEY"):
    """X71:从环境读签名密钥 —— 优先 inline,其次密钥文件;都没有返回 ""(签名关闭)。"""
    env = env or {}
    inline = str(env.get(inline_env, "")).strip()
    if inline:
        return inline
    p = str(env.get(path_env, "")).strip()
    if p and os.path.exists(p):
        try:
            return open(p, "r", encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


# ── X53:watchdog 与 dispatch 跨脚本统一事务锁(同一锁文件,互斥) ────────────
COORD_LOCK_DEFAULT = "/home/fleet/line-config/line_coordination.lock"


def acquire_coord_lock(path=None):
    """X53:获取跨脚本协调锁(非阻塞 flock)。成功返回打开的 fd(持有锁),
    被别的进程占用返回 None。watchdog 与 line_dispatch 用同一 path,避免一个在打回、
    一个在续派同时操作同一 issue。fcntl 不可用时返回 devnull fd(退化为不阻塞)。"""
    path = path or COORD_LOCK_DEFAULT
    try:
        import fcntl
    except Exception:  # pragma: no cover - 非 POSIX
        return open(os.devnull, "w")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


def release_coord_lock(fd):
    """释放协调锁。"""
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fd.close()
    except Exception:
        pass


# ── X08:route 前重读 assignee,防 TOCTOU ─────────────────────────────────────
def assignee_unchanged(expected_assignee_id, current_assignee_id):
    """X08:执行 route/assign/rerun 前确认 issue 的 assignee 仍是扫描时看到的那个。
    被别的流程刚改动(已被人接走/已重派)→ False,本轮跳过该 route 避免误派/覆盖。"""
    return str(expected_assignee_id or "") == str(current_assignee_id or "")


def route_precheck(expected_assignee_id, fetch_issue, iid):
    """X08:执行 route 前回拉 issue 确认 assignee 未变。fetch_issue(iid)->issue dict(可注入)。
    返回 (ok, reason)。拉取失败 → 保守放行(ok=True),不因读失败漏掉真异常的续派。"""
    try:
        cur = fetch_issue(iid) or {}
    except Exception as e:  # pragma: no cover - 防御
        return True, "重读 assignee 失败(%s),保守放行" % str(e)[:80]
    cur_aid = cur.get("assignee_id")
    if assignee_unchanged(expected_assignee_id, cur_aid):
        return True, ""
    return False, "assignee 在扫描后已被改动(%s→%s),跳过本轮 route 避免误派/覆盖" % (
        (expected_assignee_id or "")[:8], (cur_aid or "")[:8])


# ── X24/X27:按成员维度统计每线人数 / 工作 / 闲置 / 卡死 ──────────────────────
def member_headcount(members_by_squad, items, alerts=None,
                     active_st=("todo", "in_progress", "in_review")):
    """X24/X27:每线岗位人数 + 工作/闲置/卡死人数。

    members_by_squad: {squad_name: [{id, name, role}, ...]}(`multica squad member list` 注入)
    items: issue 列表(含 assignee_id);alerts: detect() 输出(含 squad=owner_id)。
    工作 = 该成员是某活跃 issue 的 assignee;卡死 = 该成员名下有命中告警的 issue;
    闲置 = 岗位人数 - 工作人数。返回 {squad: {headcount, by_role, working, idle, stuck}}。
    """
    alerts = alerts or []
    stuck_owners = set(a.get("squad") for a in alerts if a.get("squad"))
    active_owner_ids = set(i.get("assignee_id") for i in (items or [])
                           if i.get("status") in active_st and i.get("assignee_id"))
    out = {}
    for squad, members in (members_by_squad or {}).items():
        members = members or []
        by_role = {}
        working = stuck = 0
        for m in members:
            role = m.get("role") or "member"
            by_role[role] = by_role.get(role, 0) + 1
            mid = m.get("id")
            if mid and mid in active_owner_ids:
                working += 1
            if mid and mid in stuck_owners:
                stuck += 1
        head = len(members)
        out[squad] = {"headcount": head, "by_role": by_role, "working": working,
                      "idle": max(0, head - working), "stuck": stuck}
    return out


# ── X63:心跳停摆 → 外部 watcher(systemd timer)可消费的告警 ────────────────
def heartbeat_alert(hb, now, threshold_min):
    """X63:外部监督用。心跳超过阈值未推进 → 返回告警 dict;否则 None。
    看门狗自身停摆时它发不出告警,必须由进程外 watcher 读心跳判定 —— 本函数 + 配套
    systemd timer 即那个外部 watcher(退出码非 0 让 timer/告警链可感知)。"""
    stale, age = heartbeat_stale(hb, now, threshold_min)
    if not stale:
        return None
    age_txt = ("%.1f 分钟" % age) if age is not None else "未知(无心跳)"
    return {
        "kind": "watchdog_silent", "severity": "critical",
        "age_min": age, "threshold_min": threshold_min,
        "why": "看门狗心跳停摆:距上次成功扫描 %s(阈值 %s 分钟),cron/进程可能已死,需人工介入" % (
            age_txt, threshold_min),
    }


# ── X72:P0 告警人工确认(ack)闭环账本(外部通知通道仍需平台/webhook) ───────
def p0_ack_entry(alert, now=None):
    """X72:把一条 P0(critical)告警登记为"待人工确认"。纯函数。"""
    now = now or datetime.now(timezone.utc)
    return {
        "ts": _iso(now), "ident": alert.get("ident"), "kind": alert.get("kind"),
        "severity": _severity_of(alert.get("kind")), "why": alert.get("why", ""),
        "acked": False, "acked_by": None, "acked_at": None,
    }


def pending_p0_acks(alerts, acked_keys=None):
    """X72:从本轮告警里筛出 P0(critical)且尚未被人工 ack 的,作为"未闭环 P0"。
    acked_keys: 已确认的 (ident|kind) 集合。返回待确认 P0 告警列表。"""
    acked = set(acked_keys or [])
    out = []
    for a in alerts or []:
        if _severity_of(a.get("kind")) != "critical":
            continue
        key = "%s|%s" % (a.get("ident"), a.get("kind"))
        if key in acked:
            continue
        out.append(a)
    return out


def _heartbeat_check_main(argv):
    """X63 外部 watcher 入口:由独立 systemd timer 调用(看门狗死了它仍能跑)。
    读 watchdog_heartbeat.json,心跳停摆→打印告警 + 落 fallback 文件 + 退出码 2(外部可感知)。

    用法:line_partial.py heartbeat-check [心跳文件] [阈值分钟]
    """
    hb_path = argv[0] if len(argv) > 0 else "/home/fleet/line-config/watchdog_heartbeat.json"
    threshold = float(argv[1]) if len(argv) > 1 else 10.0
    hb = None
    if os.path.exists(hb_path):
        try:
            hb = json.load(open(hb_path))
        except Exception:
            hb = None
    al = heartbeat_alert(hb, datetime.now(timezone.utc), threshold)
    if not al:
        print("HEARTBEAT_OK 看门狗心跳正常(阈值 %s 分钟)" % threshold)
        return 0
    line = "WATCHDOG_SILENT %s" % al["why"]
    print(line)
    try:  # 落本地兜底告警(与看门狗 PL-94 贴台失败同一通道),外部 watcher 也不丢
        if _E is not None and hasattr(_E, "append_fallback"):
            _E.append_fallback(line, "heartbeat_silent")
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "heartbeat-check":
        sys.exit(_heartbeat_check_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        now = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
        assert coverage_estimate(50, 50)["truncated"] and coverage_estimate(50, 50)["est_missed"] == 1
        assert coverage_estimate(50, 50, total_hint=120)["est_missed"] == 70
        assert detect_rate_limit("HTTP 429 Too Many Requests")
        assert not detect_rate_limit("all good")
        k = "secret"
        s = sign_status({"overall": "red"}, k)
        assert verify_payload_signature(s, k)
        s2 = dict(s); s2["overall"] = "green"
        assert not verify_payload_signature(s2, k)  # 篡改后签名失配
        assert assignee_unchanged("a", "a") and not assignee_unchanged("a", "b")
        print("line_partial selfcheck OK")
