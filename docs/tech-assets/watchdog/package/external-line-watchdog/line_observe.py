#!/usr/bin/env python3
"""line_observe.py — PL-104 C 段(并发/持久化/可观测)纯函数库。

设计同 line_evidence.py:全部为纯函数 + 可注入依赖,默认离线可单测,
import 时不做任何网络/agent/磁盘副作用。看门狗(line_watchdog.py)按需 import。

覆盖 BOM C 段 U41-U64 + X41-X64 的可独立成函数的部分:
- 并发抓取 / 自适应并发 / 退避          U42 U43 / X45 X53 X54 X61 X65
- 单轮 deadline + 扫描 cursor/resume     U44 U45 / X62
- 统一锁域 / 结构化 route result          U46 U47 / X55 X56 X58
- 告警分块                                U48 / X58
- logrotate                              U49
- watchdog_status.json + 画布红灯         U50 U57 / X50
- evidence ledger 每轮 / 闭环链路          U51 U55 / X43 X47
- state schema version + 迁移 + 损坏恢复   U52 U54 / X37
- metrics / 历史计数 / 线级统计            U53 U63 U64 / X64
- last_successful_scan 心跳 + 外部监督     U56 U58 / X63
- severity / owner / SLA                  U62 / X56
"""
import hashlib
import hmac
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import line_evidence as _E  # 复用 SEVERITY/severity_of,避免两份级别表漂移
    _severity_of = _E.severity_of
except Exception:  # pragma: no cover - 离线/独立导入兜底
    _severity_of = lambda kind: "medium"


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# U42/U43/X45/X53/X54/X61/X65  并发抓取 + 自适应并发 + 退避
# ─────────────────────────────────────────────────────────────────────────────
def fetch_concurrent(keys, fetch_fn, workers=8):
    """并发对每个 key 调 fetch_fn(key),返回 {key: result}(键稳定,顺序无关)。

    X61:结果按 key 收敛,不依赖完成顺序,复盘可重放。
    单 worker / 单 key 退化为串行,保证离线测试确定性。fetch_fn 抛错的 key
    返回 (None, err) 不影响其它 key。返回的是 {key: result_or_exc}。
    """
    keys = list(keys)
    if not keys:
        return {}
    workers = max(1, min(int(workers or 1), len(keys)))
    if workers == 1:
        return {k: _safe_call(fetch_fn, k) for k in keys}
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_fn, k): k for k in keys}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                out[k] = fut.result()
            except Exception as e:  # pragma: no cover - 防御
                out[k] = _Err(str(e)[:200])
    return out


class _Err(object):
    __slots__ = ("err",)

    def __init__(self, err):
        self.err = err


def _safe_call(fn, k):
    try:
        return fn(k)
    except Exception as e:  # pragma: no cover
        return _Err(str(e)[:200])


def adaptive_workers(base, rate_limited, floor=2, ceil=16):
    """U43/X54:探到 rate-limit 信号时把并发减半(下取整),否则用 base;夹在 [floor,ceil]。"""
    base = int(base or floor)
    w = max(1, base // 2) if rate_limited else base
    return max(floor, min(ceil, w))


def backoff_delay(attempt, base=2.0, cap=60.0, factor=2.0):
    """U65/X65/X56:确定性指数退避(无 jitter,可测)。attempt 从 0 起。"""
    attempt = max(0, int(attempt))
    return float(min(cap, base * (factor ** attempt)))


# ─────────────────────────────────────────────────────────────────────────────
# U44  单轮全局 deadline
# ─────────────────────────────────────────────────────────────────────────────
class CycleDeadline(object):
    """单轮硬性时间预算。start/now 用单调时钟秒数(time.monotonic());

    budget_s<=0 / None 表示不限时(默认行为不变)。
    """

    def __init__(self, start, budget_s):
        self.start = float(start)
        self.budget_s = float(budget_s) if budget_s else 0.0

    def remaining(self, now):
        if self.budget_s <= 0:
            return float("inf")
        return self.budget_s - (float(now) - self.start)

    def expired(self, now):
        return self.remaining(now) <= 0


# ─────────────────────────────────────────────────────────────────────────────
# U45/X62  扫描 cursor / resume(中断后下轮接着扫,不每轮从头)
# ─────────────────────────────────────────────────────────────────────────────
def slice_by_cursor(sorted_ids, cursor, limit):
    """从 cursor 处取最多 limit 个 id;返回 (batch, next_cursor, wrapped)。

    limit<=0 视为不分片:取全量,next_cursor=0,wrapped=True。
    取到末尾则 next_cursor=0(下轮重新一轮);否则 next_cursor 指向下一段起点。
    cursor 越界自动归零(id 列表变化也安全)。
    """
    n = len(sorted_ids)
    if n == 0:
        return [], 0, True
    if not limit or limit <= 0:
        return list(sorted_ids), 0, True
    cur = cursor if isinstance(cursor, int) and 0 <= cursor < n else 0
    end = cur + limit
    batch = sorted_ids[cur:end]
    if end >= n:
        return batch, 0, True
    return batch, end, False


# ─────────────────────────────────────────────────────────────────────────────
# U52/X37  state schema 版本 + 迁移
# ─────────────────────────────────────────────────────────────────────────────
STATE_SCHEMA_VERSION = 2

_STATE_DEFAULTS = {
    "routed_run_ids": [],
    "alert_sigs": {},
    "idle_reminded": {},
    "scan_cursor": 0,
    "metrics": [],
    "closed_loop": [],
    "route_retry": {},
    "stage_state": {},  # U22:role-stage 状态字段(每活跃 issue 当前阶段+role)
    "status_hist": {},  # X35:每 issue 上一轮状态(判 backlog→todo 提升,给认领宽限)
    "leader_map": {},   # X26:上一轮 squad_id→leader_id 映射(对比出 leader 轮换写审计)
}


def migrate_state(raw):
    """把任意旧版 state dict 迁移到当前 schema_version,补齐缺失键,不丢已有数据。

    v0(无 schema_version)= A/B 段三键结构;v1->v2 增 scan_cursor/metrics/closed_loop/route_retry。
    迁移是幂等的:已是 v2 也安全过一遍。
    """
    d = dict(raw or {})
    for k, default in _STATE_DEFAULTS.items():
        if k not in d:
            d[k] = json.loads(json.dumps(default))  # 深拷贝默认值
    d["schema_version"] = STATE_SCHEMA_VERSION
    return d


# ─────────────────────────────────────────────────────────────────────────────
# U54  state 损坏恢复:checksum + 备份 + 原子写
# ─────────────────────────────────────────────────────────────────────────────
def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def state_checksum(payload):
    """对 payload(不含 checksum 字段)算 sha256,用于校验文件未被截断/损坏。"""
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def pack_state(payload):
    p = dict(payload)
    p.pop("checksum", None)
    p["checksum"] = state_checksum(p)
    return p


def verify_state(d):
    if not isinstance(d, dict) or "checksum" not in d:
        return False
    return d.get("checksum") == state_checksum(d)


def load_state_resilient(path, backup_path=None, require_checksum=False):
    """U54:优先主文件;解析失败/校验和不符则回退备份;再不行返回迁移后的空 state。

    返回 (state, source) — source ∈ {'primary','backup','fresh'}。
    require_checksum=False 时容忍无 checksum 的旧文件(A/B 段写的)以平滑升级。
    """
    for src, p in (("primary", path), ("backup", backup_path)):
        if not p or not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if "checksum" in d and not verify_state(d):
            continue  # 损坏:试下一个来源
        if require_checksum and "checksum" not in d:
            continue
        return migrate_state(d), src
    return migrate_state({}), "fresh"


def save_state_resilient(path, payload, backup_path=None):
    """先把现有主文件转存为备份,再带 checksum 原子写主文件(替换失败不破坏旧文件)。"""
    try:
        if backup_path and os.path.exists(path):
            shutil.copy2(path, backup_path)
    except Exception:
        pass
    packed = pack_state(payload)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(packed, f, ensure_ascii=False)
    os.replace(tmp, path)
    return packed


# ─────────────────────────────────────────────────────────────────────────────
# U62/X56  severity / owner / SLA
# ─────────────────────────────────────────────────────────────────────────────
# kind -> 责任方(与 watchdog KIND_META 的 route 对齐:crisis=危机处理Codex / brain=线主脑)
OWNER_OF = {
    "zombie": "危机处理Codex", "failed": "危机处理Codex", "terminal_conflict": "危机处理Codex",
    "cancelled": "线主脑", "evidence_missing": "线主脑", "evidence_unverified": "线主脑",
    "gate_misrouted": "线主脑", "stage_stale": "线主脑", "no_run": "线主脑",
    "todo_no_claim": "线主脑", "in_progress_no_run": "线主脑", "blocked_stale": "线主脑",
    "personal_assignment": "线主脑", "route_unconfirmed": "线主脑", "stale_evidence": "线主脑",
    "probe_down": "运维/平台", "device_offline": "运维/平台", "handoff_fallback": "运维/平台",
    "coverage_limit": "运维/平台", "schema_drift": "运维/平台",
    "issue_fetch_failed": "运维/平台", "squad_discovery_failed": "运维/平台",
    "run_fetch_failed": "运维/平台",
}

# severity -> SLA 分钟(超过即视为违约,需升级)
SLA_MIN = {"critical": 10.0, "high": 30.0, "medium": 60.0, "low": 240.0}


def sla_for(kind):
    """U62:返回 {severity, owner, sla_min}。未知 kind 给安全默认(medium/线主脑/60)。"""
    sev = _severity_of(kind)
    return {
        "severity": sev,
        "owner": OWNER_OF.get(kind, "线主脑"),
        "sla_min": SLA_MIN.get(sev, 60.0),
    }


def sla_breached(kind, age_min):
    if age_min is None:
        return False
    return age_min > sla_for(kind)["sla_min"]


def route_retry_meta(prev, now, base_backoff=2.0, cap=60.0, reason=None):
    """X56:ROUTE_RETRY_ARMED 带失败次数 + 下次重试时间。

    prev = 上轮该 key 的 {failcount,...} 或 None。返回新的
    {failcount, delay_s, next_retry_ts[, reason]}。

    B-PL142:reason 区分退避来源 —— route 失败("route_failed")与续派未确认
    ("unconfirmed")。未确认场景应传更大的 base/cap(跨多个 2 分钟周期),
    避免同一 gate_fail_rework/evidence_missing 每轮重复 POST/route。
    """
    failcount = int((prev or {}).get("failcount", 0)) + 1
    delay = backoff_delay(failcount - 1, base=base_backoff, cap=cap)
    nxt = now.timestamp() + delay if now else None
    meta = {
        "failcount": failcount,
        "delay_s": delay,
        "next_retry_ts": _iso(datetime.fromtimestamp(nxt, tz=timezone.utc)) if nxt else None,
    }
    if reason:
        meta["reason"] = reason
    return meta


# B-PL142:续派未确认(ROUTE_UNCONFIRMED)退避参数 —— 首轮 2 分钟、指数退避封顶
# 30 分钟,跨多个看门狗周期(默认 2 分钟/轮),杜绝每轮重复补派;达到上限即放弃
# 自动补派(仅保留贴台,转人工/危机)。
UNCONFIRMED_BACKOFF_BASE = 120.0
UNCONFIRMED_BACKOFF_CAP  = 1800.0
UNCONFIRMED_MAX_RETRY    = 5


def retry_due(meta, now):
    """B-PL142:退避是否到点(now >= next_retry_ts)。

    meta 缺失 / 无 next_retry_ts / now 缺失 -> False(不到点,不补派)。
    """
    if not meta or now is None:
        return False
    nxt = _parse_ts(meta.get("next_retry_ts"))
    if nxt is None:
        return False
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return now >= nxt


def retry_exhausted(meta, max_failcount=UNCONFIRMED_MAX_RETRY):
    """B-PL142:退避次数是否已耗尽(达到上限,放弃自动补派)。"""
    return bool(meta) and int(meta.get("failcount", 0)) >= max_failcount


# ─────────────────────────────────────────────────────────────────────────────
# U53/U63/U64/X64  metrics / 线级统计 / 历史计数
# ─────────────────────────────────────────────────────────────────────────────
def line_metrics(items, squads, alerts, active_st=("todo", "in_progress", "in_review"),
                 backlog_st="backlog", blocking_st=("blocked",)):
    """U63:每线 active/backlog/blocked/done + 告警计数(按 severity)。

    squads: {squad_name: set(owner_ids)} ;alerts: detect() 输出(含 squad=owner_id)。
    """
    # alert -> 严重级别,按 squad(owner_id)聚合
    alert_by_owner = {}
    for al in alerts:
        sid = al.get("squad")
        sev = _severity_of(al.get("kind"))
        b = alert_by_owner.setdefault(sid, {"total": 0, "by_severity": {}})
        b["total"] += 1
        b["by_severity"][sev] = b["by_severity"].get(sev, 0) + 1
    out = {}
    for name, owners in squads.items():
        owners = set(owners)
        sq = [i for i in items if i.get("assignee_id") in owners]
        active = sum(1 for i in sq if i.get("status") in active_st)
        backlog = sum(1 for i in sq if i.get("status") == backlog_st)
        blocked = sum(1 for i in sq if i.get("status") in blocking_st)
        done = sum(1 for i in sq if i.get("status") in ("done", "cancelled"))
        al = {"total": 0, "by_severity": {}}
        for sid in owners:
            b = alert_by_owner.get(sid)
            if not b:
                continue
            al["total"] += b["total"]
            for k, v in b["by_severity"].items():
                al["by_severity"][k] = al["by_severity"].get(k, 0) + v
        out[name] = {
            "active": active, "backlog": backlog, "blocked": blocked, "done": done,
            "total": len(sq), "alerts": al["total"], "alerts_by_severity": al["by_severity"],
            "idle": active == 0 and (backlog > 0),
        }
    return out


def cycle_metrics(now, ok, cycle_ms, scanned, alerts, line_stats, route_results=None):
    """U53/U64:单轮指标快照(可入历史)。"""
    by_kind = {}
    by_sev = {}
    for al in alerts:
        k = al.get("kind")
        by_kind[k] = by_kind.get(k, 0) + 1
        sev = _severity_of(k)
        by_sev[sev] = by_sev.get(sev, 0) + 1
    route_ok = route_fail = 0
    for r in (route_results or []):
        if r.get("ok"):
            route_ok += 1
        else:
            route_fail += 1
    return {
        "ts": _iso(now),
        "ok": bool(ok),
        "cycle_ms": int(cycle_ms),
        "scanned": int(scanned),
        "alerts_total": len(alerts),
        "alerts_by_kind": by_kind,
        "alerts_by_severity": by_sev,
        "route_ok": route_ok,
        "route_fail": route_fail,
        "lines": {n: {"active": s["active"], "backlog": s["backlog"],
                      "blocked": s["blocked"], "alerts": s["alerts"]}
                  for n, s in line_stats.items()},
    }


def append_capped(history, record, cap=500):
    h = list(history or [])
    h.append(record)
    if cap and len(h) > cap:
        h = h[-cap:]
    return h


# ─────────────────────────────────────────────────────────────────────────────
# U55/X43/X47  闭环链路 alert -> route -> rerun -> verdict
# ─────────────────────────────────────────────────────────────────────────────
def closed_loop_entry(now, alert, route_result):
    """U55:把一次『告警→路由→二次确认』压成一条可追责链记录。"""
    rr = route_result or {}
    confirmed = rr.get("confirmed")
    return {
        "ts": _iso(now),
        "ident": alert.get("ident"),
        "iid": alert.get("iid"),
        "kind": alert.get("kind"),
        "severity": _severity_of(alert.get("kind")),
        "owner": OWNER_OF.get(alert.get("kind"), "线主脑"),
        "routed": bool(rr.get("ok")),
        "route_err": rr.get("route_err") or rr.get("err") or "",
        # X43:route 成功后必须确认新 run/新证据;None=未开启确认,True/False=确认结果
        "rerun_confirmed": confirmed,
        "confirm_info": rr.get("confirm_info", ""),
        "verdict": ("confirmed" if confirmed is True
                    else "unconfirmed" if confirmed is False
                    else "routed" if rr.get("ok") else "route_failed"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# U50/U57/X50  watchdog_status.json + 画布红灯
# ─────────────────────────────────────────────────────────────────────────────
_RED_SEVERITIES = ("critical", "high")


def build_status(now, ok, alerts, line_stats, cycle_ms, last_success_ts,
                 user_counter_idents=None, scan_state="full"):
    """U50/U57:画布/投产状态板可直接消费的状态 JSON。

    红灯规则(U57):任一 critical/high 告警 → red;X50:用户反例覆盖 agent PASS
    的 issue 强制 red(即使该 issue 当前无 watchdog 告警)。无红灯且本轮 OK → green。

    X07/X10:scan_state != "full"(如 "partial-scan")表示本轮扫描不完整
    (覆盖截断 / 单轮 deadline 命中),据此扫描不得收口为 green —— 至少黄灯,
    并把 scan_state 写进状态板供画布/审计辨识半截扫描。
    """
    user_counter_idents = set(user_counter_idents or [])
    red = []
    for al in alerts:
        sev = _severity_of(al.get("kind"))
        if sev in _RED_SEVERITIES:
            red.append({"ident": al.get("ident"), "kind": al.get("kind"),
                        "severity": sev, "why": al.get("why", ""), "source": "watchdog"})
    for ident in sorted(user_counter_idents):
        red.append({"ident": ident, "kind": "user_counterexample", "severity": "critical",
                    "why": "用户反例覆盖 agent PASS,强制返工", "source": "user"})
    sev_roll = {}
    for al in alerts:
        s = _severity_of(al.get("kind"))
        sev_roll[s] = sev_roll.get(s, 0) + 1
    partial = scan_state != "full"
    if red:
        overall = "red"
    elif ok and not partial:
        overall = "green"
    else:
        overall = "yellow"   # 半截扫描(partial-scan)即便无红灯也不得判 green
    return {
        "ts": _iso(now),
        "overall": overall,
        "ok": bool(ok) and not partial,
        "scan_state": scan_state,
        "cycle_ms": int(cycle_ms),
        "last_successful_scan": last_success_ts,
        "alert_total": len(alerts),
        "severity_rollup": sev_roll,
        "red_lights": red,
        "lines": line_stats,
        "schema_version": 1,
    }


def write_json_atomic(obj, path):
    if not path:
        return None
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# U56/U58/X63  last_successful_scan 心跳 + 外部停摆判定
# ─────────────────────────────────────────────────────────────────────────────
def build_heartbeat(now, ok, cycle_ms, scanned, alert_total, prev=None):
    """U56:每轮写心跳;只有本轮成功(issue 抓取未失败)才推进 last_successful_scan。"""
    prev = prev or {}
    last_success = _iso(now) if ok else prev.get("last_successful_scan")
    return {
        "ts": _iso(now),
        "ok": bool(ok),
        "cycle_ms": int(cycle_ms),
        "scanned": int(scanned),
        "alert_total": int(alert_total),
        "last_successful_scan": last_success,
        "consecutive_fail": 0 if ok else int(prev.get("consecutive_fail", 0)) + 1,
    }


def heartbeat_stale(hb, now, threshold_min):
    """U58/X63:外部监督用 —— 距上次成功扫描超过阈值即判看门狗停摆。返回 (stale, age_min)。"""
    if not hb:
        return True, None
    ts = _parse_ts(hb.get("last_successful_scan") or hb.get("ts"))
    if ts is None:
        return True, None
    age = (now - ts).total_seconds() / 60.0
    return age > threshold_min, age


# ─────────────────────────────────────────────────────────────────────────────
# U48/X58  告警分块 / route 刷屏汇总
# ─────────────────────────────────────────────────────────────────────────────
def chunk_lines(lines, max_chars, header=""):
    """U48:把告警行按 max_chars 分块(不切断单行)。返回 [body, ...]。

    header 每块都带,并加 (i/n) 角标。单行超 max_chars 时该行独占一块(不丢)。
    max_chars<=0 或总长在限内 → 单块。
    """
    lines = list(lines)
    body = "\n".join(lines)
    if max_chars <= 0 or len(body) + len(header) <= max_chars:
        return [(header + "\n" + body).strip() if header else body]
    chunks = []
    cur = []
    cur_len = 0
    budget = max_chars - len(header) - 16  # 给 header + 角标留余量
    budget = max(budget, 1)
    for ln in lines:
        add = len(ln) + 1
        if cur and cur_len + add > budget:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += add
    if cur:
        chunks.append(cur)
    n = len(chunks)
    out = []
    for i, c in enumerate(chunks, 1):
        head = "%s (%d/%d)" % (header, i, n) if header else "(%d/%d)" % (i, n)
        out.append((head + "\n" + "\n".join(c)).strip())
    return out


def summarize_route_lines(route_lines, max_lines):
    """X58:route 行超过 max_lines 时折叠为『前 N 行 + 其余计数』,不每轮刷全屏。"""
    route_lines = list(route_lines)
    if max_lines <= 0 or len(route_lines) <= max_lines:
        return route_lines
    head = route_lines[:max_lines]
    rest = len(route_lines) - max_lines
    return head + ["… 其余 %d 条 route 已折叠(避免刷屏,详见 metrics/状态板)" % rest]


# ─────────────────────────────────────────────────────────────────────────────
# U49  logrotate
# ─────────────────────────────────────────────────────────────────────────────
def rotate_log(path, max_bytes, keep=5):
    """U49:日志超过 max_bytes 即轮转 path -> path.1 -> ... -> path.keep,最旧丢弃。

    返回是否发生轮转。max_bytes<=0 关闭。文件不存在/未超限不动。
    """
    if max_bytes <= 0 or not path or not os.path.exists(path):
        return False
    try:
        if os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False
    try:
        oldest = "%s.%d" % (path, keep)
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src = "%s.%d" % (path, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (path, i + 1))
        os.replace(path, "%s.1" % path)
        open(path, "w").close()  # 重建空文件,cron 继续 append
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# PL-124 U72:安全止损 —— 总开关(disable) + canary 灰度路由 + 回滚(纯函数)
# ─────────────────────────────────────────────────────────────────────────────
def _now():
    """与 line_watchdog.now_utc 对齐:支持 WATCHDOG_NOW 定格(回归确定性),否则取真实 UTC。"""
    nv = os.environ.get("WATCHDOG_NOW")
    if nv:
        try:
            return datetime.fromisoformat(str(nv).replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_until(v):
    """把 until/expires 值解析成 aware datetime:支持 ISO8601 或 epoch 秒。"""
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        if v.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except Exception:
        pass
    return _parse_ts(v)


def parse_disable_flag(text, mtime_dt=None):
    """PL-137:解析 watchdog.disabled 标志内容,向后兼容两种写法。

      1) 旧式自由文本(单行)            -> reason=该文本,无到期(=手动停用,删标志才恢复)。
      2) 结构化多行 `key: value`/`key=value`,可识别键:
           reason                       停用原因(贴进状态板/日志,便于审计)
           until / expires / expires_at 到期时刻(ISO8601 或 epoch 秒)——到点自动恢复
           ttl_min / ttl                相对标志文件 mtime 的存活分钟数,到点自动恢复
    返回 dict(reason, until: datetime|None, structured: bool)。
    """
    reason = ""
    until = None
    ttl_min = None
    structured = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            sep = ":"
        elif "=" in line:
            sep = "="
        else:
            sep = None
        if sep is None:
            # 非 key:value 行 —— 当作旧式自由文本 reason(只取第一段)
            if not reason:
                reason = line
            continue
        k, v = line.split(sep, 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "reason":
            reason, structured = v, True
        elif k in ("until", "expires", "expires_at", "expire"):
            until, structured = _parse_until(v), True
        elif k in ("ttl_min", "ttl"):
            try:
                ttl_min, structured = float(v), True
            except Exception:
                pass
    if until is None and ttl_min is not None and mtime_dt is not None:
        from datetime import timedelta
        until = mtime_dt + timedelta(minutes=ttl_min)
    return {"reason": reason, "until": until, "structured": structured}


def watchdog_disabled(env, flag_path=None):
    """U72 / PL-137:总开关(临时停用)+ 恢复条件。停用判定优先级:

      1. env['WATCHDOG_DISABLED'] 真值 -> 停用(环境变量,取消该变量即恢复)。
      2. flag 文件存在 -> 解析内容(parse_disable_flag):
           - 带 until/ttl_min 且已过期 -> 视为"已到期自动恢复"(返回 disabled=False,
             reason 说明已恢复)。禁用因此永远是临时的,不会吞掉恢复条件;过期标志
             文件由运维按 RECOVERY 口径删除即可。
           - 未过期 / 无到期(旧式自由文本)-> 停用,reason 带恢复口径。

    停用时看门狗本轮只读巡查:不贴台、不 route/assign/rerun、不写收口证据,仅刷新心跳/状态板。
    返回 (disabled: bool, reason: str)。disabled=False 时 reason 非空表示"标志已到期自动恢复"。
    """
    raw = str((env or {}).get("WATCHDOG_DISABLED", "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True, "WATCHDOG_DISABLED=%s(环境变量总开关,取消该变量即恢复)" % raw
    if flag_path and os.path.exists(flag_path):
        try:
            text = open(flag_path, encoding="utf-8").read()
        except Exception:
            text = ""
        try:
            mtime_dt = datetime.fromtimestamp(os.path.getmtime(flag_path), tz=timezone.utc)
        except Exception:
            mtime_dt = None
        info = parse_disable_flag(text, mtime_dt=mtime_dt)
        until = info.get("until")
        if until is not None:
            if _now() >= until:
                return False, "停用标志 %s 已到期(until=%s),本轮自动恢复巡查/续派" % (
                    flag_path, _iso(until))
            reason = info.get("reason") or "临时停用"
            return True, "%s;到期自动恢复 until=%s(标志 %s)" % (reason, _iso(until), flag_path)
        reason = info.get("reason") or "存在停用标志文件"
        return True, "%s(标志 %s;无到期=手动停用,删除标志即恢复)" % (reason, flag_path)
    return False, ""


def write_disabled_state(reason, heartbeat_path, status_path, now=None):
    """PL-137:停用期间也刷新心跳/状态板(disabled 标记),让监控看见"停用但存活",
    并为恢复验证提供状态签名(overall=disabled)。返回 (heartbeat, status)。"""
    now = now or _now()
    hb = {"ts": _iso(now), "ok": True, "disabled": True, "disabled_reason": reason,
          "cycle_ms": 0, "scanned": 0, "alert_total": 0}
    write_json_atomic(hb, heartbeat_path)
    st = {"ts": _iso(now), "overall": "disabled", "ok": True, "disabled": True,
          "disabled_reason": reason, "scan_state": "disabled", "schema_version": 1}
    write_json_atomic(st, status_path)
    return hb, st


def canary_allows(issue_id, pct):
    """U72:canary 灰度路由。pct=路由放量百分比(0-100)。

    对 issue_id 做稳定哈希取 [0,100) 桶,桶 < pct 才放行路由;其余只告警不路由。
    pct>=100 全量(默认),pct<=0 全部只读。哈希稳定 → 同一 issue 每轮判定一致,可平滑放量。
    返回 bool(是否允许对该 issue 执行 route/assign/rerun)。
    """
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return True
    if p >= 100:
        return True
    if p <= 0:
        return False
    if not issue_id:
        return True
    h = int(hashlib.sha1(str(issue_id).encode("utf-8")).hexdigest(), 16) % 100
    return h < p


# ─────────────────────────────────────────────────────────────────────────────
# X23/X25/X26  L2 身份拓扑 × L6 并发 / L7 持久化:squad 代际(generation)锁 +
# 状态 key 带 squad+generation + leader 映射变更审计日志
#
# 背景:线小队的 leader 会轮换;squad 也可能 archived 后用同一 T 编号重建。
# 只用 T 编号(squad name)或裸 squad_id 做 key / 路由目标,会在以下场景出事:
#   - 路由作业在 leader 轮换的瞬间并发执行,结果套到了新 leader 上(X23);
#   - 去重/提醒状态 key 不含代际,leader 轮换后旧状态误命中新代际(X25);
#   - leader 映射悄悄变了却无任何审计痕迹,事后无法复盘谁在何时换的(X26)。
# 代际(generation)= squad_id + 当前 leader_id 的稳定指纹:leader 一变,代际就变。
# ─────────────────────────────────────────────────────────────────────────────
def squad_generation(squad_id, leader_id):
    """X23/X25:把 (squad_id, leader_id) 压成稳定的代际指纹(短 sha1)。

    leader 轮换 → 指纹变;squad 重建拿到新 squad_id → 指纹变。
    leader_id 缺失时用 '-' 占位(仍随 squad_id 稳定),不抛错。
    """
    raw = "%s|%s" % (squad_id or "-", leader_id or "-")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def route_generation_guard(captured_gen, current_gen):
    """X23:路由执行前的代际校验。作业在 build 时记录目标 squad 的代际(captured_gen),
    真正 assign/rerun 前再取一次当前代际(current_gen)比对。

    代际不一致 = leader 在并发窗口内轮换 / squad 已重建 → 本次路由作废,不得套到新代际上,
    应重新基于新代际评估。返回 (ok: bool, reason: str)。captured_gen 为空(老作业无代际)
    时放行(向后兼容),只在两者都有且不等时拦截。
    """
    if not captured_gen or not current_gen:
        return True, ""
    if captured_gen == current_gen:
        return True, ""
    return False, ("目标小队代际已变(build时=%s,执行时=%s):leader 轮换/小队重建,"
                   "本次路由作废,需基于新代际重新评估" % (captured_gen, current_gen))


def state_key(ident, squad_id, generation, iid, kind):
    """X25:去重/提醒状态 key 必须带 squad_id + generation,不能只用 T 编号/ident。

    顺序与含义:ident(任务编号,可复用)| squad(归属维度)| gen(leader 代际)|
    iid(issue UUID,代际/重建标识)| kind(异常类型)。任一维度变化都生成新 key,
    保证 leader 轮换 / squad 重建后告警能重新触发,而不是被旧 key 静默吞掉。
    """
    return "%s|%s|%s|%s|%s" % (
        ident, squad_id or "-", generation or "-", iid or "-", kind)


def diff_leader_map(prev_map, cur_map):
    """X26:对比两轮的 leader 映射 {squad_id: leader_id},返回变更条目列表。

    每条:{squad, from, to, change}。change ∈ {added, removed, rotated}。
    prev/cur 任一为 None 视为空。无变化返回 []。纯函数,不写盘。
    """
    prev = dict(prev_map or {})
    cur = dict(cur_map or {})
    out = []
    for sid in sorted(set(prev) | set(cur)):
        p = prev.get(sid)
        c = cur.get(sid)
        if p == c:
            continue
        if p is None:
            change = "added"
        elif c is None:
            change = "removed"
        else:
            change = "rotated"
        out.append({"squad": sid, "from": p, "to": c, "change": change})
    return out


def append_leader_audit(entries, path, now=None, keep=2000):
    """X26:把 leader 映射变更追加进审计日志(JSONL)。entries=diff_leader_map 的结果。

    每行一条带时间戳的记录,可重放/可追责。entries 为空则不写、返回 0。
    keep 控制日志条数上限(超出截前)。返回写入条数。
    """
    if not entries or not path:
        return 0
    ts = _iso(now) if now else None
    lines = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except Exception:
            lines = []
    for e in entries:
        rec = dict(e)
        rec["ts"] = ts
        lines.append(json.dumps(rec, ensure_ascii=False, sort_keys=True))
    if keep and len(lines) > keep:
        lines = lines[-keep:]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return len(entries)


# ─────────────────────────────────────────────────────────────────────────────
# X61  L6 并发 × L7 持久化:并发 route 结果顺序不稳定 → 写 sequence id
#
# run_route_jobs 用 ThreadPoolExecutor + as_completed,结果到达顺序随线程调度抖动,
# 每轮复盘看到的 route 顺序都不一样,难以重放同一轮。给每个作业在 build 时按稳定键
# 分配一个确定的 sequence id(seq),结果带回该 seq,落盘/复盘按 seq 排序即可稳定重放。
# ─────────────────────────────────────────────────────────────────────────────
def assign_sequence_ids(jobs, key=None):
    """X61:对作业列表按稳定键排序并赋予 1..N 的 seq(原地写 job['seq'],也返回该列表)。

    默认稳定键 = (iid, squad);可注入 key(job)->sortable 覆盖。seq 与到达顺序无关,
    同一批作业每轮得到同样的 seq,可作为复盘/落盘的稳定序号。
    """
    if key is None:
        def key(j):
            return (str(j.get("iid") or ""), str(j.get("squad") or ""))
    ordered = sorted(list(jobs), key=key)
    for i, j in enumerate(ordered, 1):
        try:
            j["seq"] = i
        except Exception:  # pragma: no cover - job 不是 dict 的防御
            pass
    return ordered


def sort_by_sequence(results):
    """X61:把乱序到达的并发 route 结果按 seq 稳定排序(无 seq 的排最后,保持原相对序)。"""
    res = list(results or [])
    have = [r for r in res if isinstance(r, dict) and r.get("seq") is not None]
    miss = [r for r in res if not (isinstance(r, dict) and r.get("seq") is not None)]
    have.sort(key=lambda r: r.get("seq"))
    return have + miss


if __name__ == "__main__":  # 简易自检
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        now = datetime(2026, 6, 9, 4, 28, tzinfo=timezone.utc)
        st, src = load_state_resilient("/nonexistent.json")
        assert src == "fresh" and st["schema_version"] == STATE_SCHEMA_VERSION
        assert adaptive_workers(8, True) == 4 and adaptive_workers(8, False) == 8
        b, c, w = slice_by_cursor(["a", "b", "c", "d"], 0, 2)
        assert b == ["a", "b"] and c == 2 and not w
        # X23/X25:代际锁 + 状态 key 带代际
        g1 = squad_generation("sq", "leaderA")
        g2 = squad_generation("sq", "leaderB")
        assert g1 != g2
        assert route_generation_guard(g1, g1)[0] and not route_generation_guard(g1, g2)[0]
        assert state_key("PL-1", "sq", g1, "iid", "failed") != state_key("PL-1", "sq", g2, "iid", "failed")
        # X26:leader 映射 diff
        d = diff_leader_map({"sq": "a"}, {"sq": "b"})
        assert d and d[0]["change"] == "rotated"
        # X61:稳定 seq + 复盘排序
        js = assign_sequence_ids([{"iid": "b"}, {"iid": "a"}])
        assert [j["iid"] for j in js] == ["a", "b"] and js[0]["seq"] == 1
        rs = sort_by_sequence([{"seq": 2}, {"seq": 1}])
        assert [r["seq"] for r in rs] == [1, 2]
        print("line_observe selfcheck OK")
