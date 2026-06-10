#!/usr/bin/env python3
# 线机制看门狗(纯脚本,无模型):动态扫描所有 线小队-TNN 的活跃任务,检测并去重分流。
# 检测维度(PL-104 BOM 深挖补齐):
#   [zombie]          run running 过久(僵尸)                              -> 派危机处理(codex)
#   [failed]          run failed                                            -> 派危机处理(codex)
#   [cancelled]       最新关键 run status=cancelled 且 result 为空           -> 提醒线主脑返工/危机(连续取消->危机)
#   [evidence_missing]最新关键 run completed 但无 VERDICT/PR/URL/截图等证据   -> 视为无效完成,提醒线主脑
#   [gate_misrouted]  近期 run 含占位岗门禁误触发("不能冒充千问/占位岗")     -> 判门禁失败,提醒线主脑走 line_bridge.py gate
#   [stage_stale]     阶段推进超时(两类,统一 kind=stage_stale):
#                     (a) 卡死:in_progress/in_review 超时无评论/无活跃 run,
#                         且最新关键 run cancelled/failed;
#                     (b) deadline 驱动的下一环节未推进(BOM-3 状态机,即便上一阶段正常完成):
#                         in_review 无审计 run / 审计 PASS 后无 gate /
#                         gate PASS 后无 done/reset / gate FAIL/页面视觉 FAIL 后无返工 run
#                                                                           -> 提醒线主脑/cc 推进
#   [idle]            线已无活跃任务但仍有 backlog/待收尾父任务               -> 提醒线主脑推进下一项
# 去重(BOM-7):按 (ident|kind) 分别签名,不同类型互不吞没;状态变化(如连续取消第2次)必须重新提醒。
# 用法:line_watchdog.py [--zombie-min N] [--stale-min N] [--post] [--route] [--auto-rerun]
# 测试钩子(仅自测用,生产不设):
#   WATCHDOG_FIXTURE=<json>          用本地 issue 列表替代真实 multica issue list
#   WATCHDOG_RUNS_FIXTURE=<json>     {issue_id: [runs...]} 替代 multica issue runs
#   WATCHDOG_COMMENTS_FIXTURE=<json> {issue_id: 最近评论ISO时间} 替代 comment list(供 stage_stale)
#   WATCHDOG_NOW=<ISO8601>           固定"现在"(定格回归样本时刻)
#   WATCHDOG_STATE=<json路径>        覆盖去重状态文件(自测隔离,默认 watchdog_state.json)
#   WATCHDOG_DRY_RUN=1               comment() 只打印不真发(不触发任何 agent/不贴台)
import re
import subprocess, json, sys, os, argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import line_states as S   # BOM-3 阶段推进状态机 + 纯判定库(classify_event/stage_progress_overdue)
import line_evidence as E # B段(U25-U40):证据验证/探针/信任/兜底纯函数库
import line_observe as O  # C段(U41-U64):并发/持久化/可观测纯函数库
import line_partial as P  # X24/X27/X63/X72:成员维度统计/心跳外部watcher/P0确认纯函数库

HANDOFF = "bc056ade-f639-41af-b5df-9c7fb6a27628"  # PL-94 看门狗告警台
STATE_F = os.environ.get("WATCHDOG_STATE", "/home/fleet/line-config/watchdog_state.json")
STATE_BAK_F = os.environ.get("WATCHDOG_STATE_BAK", STATE_F + ".bak")  # U54 损坏恢复备份
SCRIPT_LOCK_F = os.environ.get("WATCHDOG_SCRIPT_LOCK", "/home/fleet/line-config/watchdog-script.lock")
# PL-132:仅在 --post 真实运行时才回写 latest_valid_evidence_* metadata(离线/只读巡查不写台)。
WRITE_METADATA = False
# PL-137:本轮是否处于停用(只读)态 + 原因,供 emit_observability 写进状态板/心跳的 disabled 签名。
_DISABLED_STATE = [False, ""]
# C段可观测输出(默认指向 line-config;--emit-observability 才落盘,离线测试不污染)
STATUS_F = os.environ.get("WATCHDOG_STATUS_F", "/home/fleet/line-config/watchdog_status.json")
HEARTBEAT_F = os.environ.get("WATCHDOG_HEARTBEAT_F", "/home/fleet/line-config/watchdog_heartbeat.json")
CRON_LOG_F = os.environ.get("WATCHDOG_CRON_LOG", "/home/fleet/line-config/watchdog-cron.log")
ALERT_CHUNK_CHARS = int(os.environ.get("WATCHDOG_ALERT_CHUNK_CHARS", "8000"))  # U48 单条告警上限
LEDGER_DIR = os.environ.get("WATCHDOG_LEDGER_DIR", E.LEDGER_DIR)  # U30 evidence ledger 落盘目录

# 默认小队兜底:真实生产优先从 `multica squad list` 动态发现 线小队-TNN。
DEFAULT_SQUADS = {
    "线小队-T01": "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96",
    "线小队-T02": "9db04481-be45-48a1-8114-5f2b85506f78",
    "线小队-T03": "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52",
}

# 默认线主脑兜底:仅用于识别历史遗留个人 assignee,不用于发新任务。
DEFAULT_LINE_BRAIN = {
    "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96": ("514f9573-4cf9-4b99-b13f-238967893d63", "线主脑-T01"),
    "9db04481-be45-48a1-8114-5f2b85506f78": ("d1713390-6dd4-4caf-ad6a-7a55a1bfaaa9", "线主脑-T02"),
    "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52": ("b70d9b47-4243-4c91-9393-8034fe413ede", "线主脑-T03"),
}
LINE_SQUAD_RE = re.compile(r"^线小队-T\d+$")

# 完成证据标记:completed run 的 result 文本里必须至少出现一个,否则视为"无效完成"。
EVIDENCE_MARKERS = ("VERDICT", "http://", "https://", "PR #", "pr #", "/pull/",
                    "截图", "screenshot", ".png", ".jpg", "canvas", "PASS", "FAIL")
# 门禁误触发(占位岗回复"不能执行/不能冒充千问")标记。
MISROUTE_MARKERS = ("不能冒充千问", "占位岗误触发", "不能直接做千问", "不能做千问门禁", "冒充千问结果")

DEFAULT_CMD_TIMEOUT = float(os.environ.get("WATCHDOG_CMD_TIMEOUT", "30"))
DEFAULT_ROUTE_TIMEOUT = float(os.environ.get("WATCHDOG_ROUTE_TIMEOUT", "25"))
ISSUE_LIMIT = int(os.environ.get("WATCHDOG_ISSUE_LIMIT", "5000"))
# U01: 真分页步长。fetch_issues 按 --offset 翻页累加到 ISSUE_LIMIT,而不是单次 limit 截断。
ISSUE_PAGE_SIZE = int(os.environ.get("WATCHDOG_ISSUE_PAGE_SIZE", "200"))
# U19: 只对最近这么多分钟内更新过的 done/cancelled issue 做"收口矛盾(仍有 running run)"检查,
# 避免给所有历史 done issue 拉 run。
TERMINAL_SCAN_MIN = float(os.environ.get("WATCHDOG_TERMINAL_SCAN_MIN", "720"))
MIN_ROUTE_WORKERS = int(os.environ.get("WATCHDOG_MIN_ROUTE_WORKERS", "24"))
WORKERS_PER_SQUAD = int(os.environ.get("WATCHDOG_WORKERS_PER_SQUAD", "3"))
# X35: backlog→todo 刚提升的认领宽限窗口(分钟);窗口内不报 todo_no_claim。
CLAIM_GRACE_MIN = float(os.environ.get("WATCHDOG_CLAIM_GRACE_MIN", "10"))
# X46: completed run 缺证据但声称有截图/URL 时的上传宽限(分钟);窗口内不判 evidence_missing。
EVIDENCE_GRACE_MIN = float(os.environ.get("WATCHDOG_EVIDENCE_GRACE_MIN", "10"))
# X26: leader 映射变更审计日志(JSONL)。
LEADER_AUDIT_F = os.environ.get("WATCHDOG_LEADER_AUDIT_F", "/home/fleet/line-config/leader_audit.jsonl")
# X53: watchdog/dispatch 跨脚本统一事务锁(同一文件,route 与续派互斥,防并发改同一 issue)。
COORD_LOCK_F = os.environ.get("WATCHDOG_COORD_LOCK", P.COORD_LOCK_DEFAULT)
# X38: 阶段转换(from/to/event/source)逐轮持久化日志(JSONL)。
STAGE_TRANS_F = os.environ.get("WATCHDOG_STAGE_TRANS_F", "/home/fleet/line-config/stage_transitions.jsonl")
# X11: schema drift 独立证据文件落盘目录(与告警分离,永久留底可追溯)。
SCHEMA_DRIFT_DIR = os.environ.get("WATCHDOG_SCHEMA_DRIFT_DIR",
                                  "/home/fleet/line-config/memory/.evidence/schema_drift")
# X08: 本轮扫描时各 issue 的 assignee_id 快照(route 执行前回拉比对,防 TOCTOU 误派)。
_SCAN_ASSIGNEE = {}
# X54: 本轮 CLI 输出里探测到的 rate-limit 信号文本(供 adaptive_workers 自动降并发)。
_RATE_LIMIT_HINTS = []
_SQUAD_CACHE = None
_BRAIN_CACHE = None
_SQUAD_DISCOVERY_WARNING = None
_ISSUE_FETCH_WARNING = None
_RUN_FETCH_WARNINGS = {}

def squad_sort_key(item):
    name = item[0] if isinstance(item, tuple) else str(item)
    m = re.search(r"T(\d+)$", name)
    return (0, int(m.group(1))) if m else (1, name)

def acquire_script_lock():
    if os.environ.get("WATCHDOG_DRY_RUN") or os.environ.get("WATCHDOG_DISABLE_LOCK"):
        return None
    try:
        lock_dir = os.path.dirname(SCRIPT_LOCK_F)
        if lock_dir:
            os.makedirs(lock_dir, exist_ok=True)
        fh = open(SCRIPT_LOCK_F, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write("%s\n" % os.getpid())
        fh.flush()
        return fh
    except BlockingIOError:
        print("WATCHDOG_SKIP: previous direct run still active")
        return False
    except Exception as e:
        print("WATCHDOG_LOCK_WARN: %s" % str(e)[:160])
        return None

def sh(*a, timeout=None):
    if timeout is None:
        timeout = DEFAULT_CMD_TIMEOUT
    try:
        r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        class R: stdout=""; stderr=str(e); returncode=1
        r = R()
    # X54:从 CLI stdout/stderr 自动探测 rate-limit 信号(不再只靠 WATCHDOG_RATE_LIMITED 注入),
    # 命中样本留给 adaptive_workers 决定本轮是否降并发。
    try:
        if getattr(r, "returncode", 0) != 0 and P.detect_rate_limit(r.stderr, r.stdout):
            _RATE_LIMIT_HINTS.append((r.stderr or r.stdout or "")[:200])
    except Exception:
        pass
    return r

def jget(*a):
    r = sh(*a)
    try: return json.loads(r.stdout)
    except Exception: return None

def fetch_issue_brief(iid):
    """X08:route 执行前回拉单个 issue(含最新 assignee_id),供 route_precheck 比对防 TOCTOU。
    线上走 `multica issue get`;读取失败由 route_precheck 保守放行(不因读失败漏掉真异常续派)。"""
    d = jget("multica", "issue", "get", iid, "--output", "json")
    return d if isinstance(d, dict) else {}

def _squad_items_from_obj(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("squads", d.get("items", []))
    return []

def fetch_squad_items():
    global _SQUAD_DISCOVERY_WARNING
    _SQUAD_DISCOVERY_WARNING = None
    if os.environ.get("WATCHDOG_SQUAD_LIST_FAIL"):
        _SQUAD_DISCOVERY_WARNING = "forced squad list failure"
        return []
    fx = os.environ.get("WATCHDOG_SQUADS_FIXTURE")
    if fx:
        try:
            return _squad_items_from_obj(json.load(open(fx)))
        except Exception as e:
            _SQUAD_DISCOVERY_WARNING = "squad fixture read failed:%s" % str(e)[:120]
            return []
    r = sh("multica", "squad", "list", "--output", "json")
    if r.returncode != 0:
        _SQUAD_DISCOVERY_WARNING = (r.stderr or r.stdout or "squad list failed")[:200]
        return []
    try:
        d = json.loads(r.stdout)
    except Exception as e:
        _SQUAD_DISCOVERY_WARNING = "squad list json parse failed:%s" % str(e)[:120]
        return []
    return _squad_items_from_obj(d)

def load_line_squads():
    global _SQUAD_CACHE, _BRAIN_CACHE
    if _SQUAD_CACHE is not None:
        return _SQUAD_CACHE
    squads = {}
    brains = {}
    for s in fetch_squad_items():
        name = s.get("name") or ""
        sid = s.get("id") or ""
        if not sid or not LINE_SQUAD_RE.match(name) or s.get("archived_at"):
            continue
        squads[name] = sid
        leader = s.get("leader_id")
        if leader:
            brains[sid] = (leader, name.replace("线小队", "线主脑"))
    if not squads:
        squads = dict(DEFAULT_SQUADS)
        brains = dict(DEFAULT_LINE_BRAIN)
    _SQUAD_CACHE = dict(sorted(squads.items(), key=squad_sort_key))
    # 动态发现结果优先,但保留默认旧 leader 识别能力。
    merged = dict(DEFAULT_LINE_BRAIN)
    merged.update(brains)
    _BRAIN_CACHE = merged
    return _SQUAD_CACHE

def load_line_brains():
    if _BRAIN_CACHE is None:
        load_line_squads()
    return _BRAIN_CACHE or {}

def auto_route_workers():
    return max(MIN_ROUTE_WORKERS, len(load_line_squads()) * WORKERS_PER_SQUAD)

def squad_discovery_warning():
    load_line_squads()
    return _SQUAD_DISCOVERY_WARNING

def now_utc():
    nv = os.environ.get("WATCHDOG_NOW")
    if nv:
        try: return datetime.fromisoformat(nv.replace("Z", "+00:00"))
        except Exception: pass
    return datetime.now(timezone.utc)

def age_min(ts):
    if not ts: return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (now_utc() - t).total_seconds() / 60.0
    except Exception:
        return None

def load_state():
    # U52/U54:经 line_observe 迁移 + 损坏恢复(checksum 校验,坏了回退备份)。
    # 兼容 A/B 段无 checksum 旧文件(平滑升级);新增 C 段键(scan_cursor/metrics/...)。
    d, _src = O.load_state_resilient(STATE_F, STATE_BAK_F)
    return {"routed_run_ids": set(d.get("routed_run_ids", [])),
            "alert_sigs": dict(d.get("alert_sigs", {})),
            "idle_reminded": dict(d.get("idle_reminded", {})),
            "scan_cursor": int(d.get("scan_cursor", 0) or 0),
            "metrics": list(d.get("metrics", [])),
            "closed_loop": list(d.get("closed_loop", [])),
            "route_retry": dict(d.get("route_retry", {})),
            "stage_state": dict(d.get("stage_state", {})),  # U22:role-stage 状态字段
            "status_hist": dict(d.get("status_hist", {})),  # X35:每 issue 上一轮状态
            "leader_map": dict(d.get("leader_map", {}))}    # X26:上一轮 leader 映射

def save_state(st):
    try:
        data = {"routed_run_ids": list(st["routed_run_ids"])[-500:],
                "alert_sigs": st.get("alert_sigs", {}),
                "idle_reminded": st.get("idle_reminded", {}),
                "scan_cursor": int(st.get("scan_cursor", 0) or 0),
                "metrics": st.get("metrics", [])[-O_METRICS_CAP:],
                "closed_loop": st.get("closed_loop", [])[-O_CLOSEDLOOP_CAP:],
                "route_retry": st.get("route_retry", {}),
                "stage_state": st.get("stage_state", {}),  # U22:role-stage 状态字段
                "status_hist": st.get("status_hist", {}),  # X35:每 issue 上一轮状态
                "leader_map": st.get("leader_map", {})}    # X26:上一轮 leader 映射
        O.save_state_resilient(STATE_F, O.migrate_state(data), STATE_BAK_F)
    except Exception:
        pass

O_METRICS_CAP = int(os.environ.get("WATCHDOG_METRICS_CAP", "500"))
O_CLOSEDLOOP_CAP = int(os.environ.get("WATCHDOG_CLOSEDLOOP_CAP", "500"))

# ACTIVE_ST: 需要盯 run/阶段推进的活跃态。BLOCKING_ST(U02): blocked 是平台真实状态,
# 不在 ACTIVE_ST 里会被旧逻辑静默,改为单独监管(blocked 超时也要提醒线主脑)。
ACTIVE_ST = ("todo", "in_progress", "in_review")
BLOCKING_ST = ("blocked",)
DONE_ST = ("done", "cancelled")
# U02/U07/U17: 平台已知状态全集(schema 驱动的基线)。出现集合外的新状态=schema 漂移,
# 必须告警,不能静默漏扫。新增状态时在此登记或由平台 schema 校验补全。
KNOWN_STATUSES = ("todo", "in_progress", "in_review", "done", "cancelled", "backlog", "blocked")

_ISSUE_TRUNCATED = False

def _issue_items_from(d):
    return d if isinstance(d, list) else (d.get("issues", d.get("items", [])) if d else [])

def fetch_issues():
    """U01 真分页:按 --offset 翻页累加全量 issue,而不是单次 --limit 截断。

    每页 ISSUE_PAGE_SIZE 条,翻到不足一页即停;累计达到 ISSUE_LIMIT 硬上限时
    标记 _ISSUE_TRUNCATED(由 system_alerts 转成 coverage_limit 告警,显示影响范围)。
    """
    global _ISSUE_FETCH_WARNING, _ISSUE_TRUNCATED
    _ISSUE_FETCH_WARNING = None
    _ISSUE_TRUNCATED = False
    if os.environ.get("WATCHDOG_ISSUE_LIST_FAIL"):
        _ISSUE_FETCH_WARNING = "forced issue list failure"
        return []
    fx = os.environ.get("WATCHDOG_FIXTURE")
    if fx:
        try:
            d = _issue_items_from(json.load(open(fx)))
            if len(d) >= ISSUE_LIMIT:   # 离线 fixture 也尊重硬上限,触发 coverage_limit
                _ISSUE_TRUNCATED = True
                d = d[:ISSUE_LIMIT]
            return d
        except Exception as e:
            _ISSUE_FETCH_WARNING = "issue fixture read failed:%s" % str(e)[:120]
            return []
    out = []
    seen = set()
    offset = 0
    while len(out) < ISSUE_LIMIT:
        page_size = min(ISSUE_PAGE_SIZE, ISSUE_LIMIT - len(out))
        r = sh("multica", "issue", "list", "--limit", str(page_size),
               "--offset", str(offset), "--output", "json")
        if r.returncode != 0:
            # 首页失败=整轮不可信;后续页失败=部分覆盖,标记截断并停。
            msg = (r.stderr or r.stdout or "issue list failed")[:200]
            if offset == 0:
                _ISSUE_FETCH_WARNING = msg
                return []
            _ISSUE_FETCH_WARNING = "分页第 offset=%d 页失败,仅覆盖前 %d 条:%s" % (offset, len(out), msg)
            _ISSUE_TRUNCATED = True
            break
        try:
            page = _issue_items_from(json.loads(r.stdout))
        except Exception as e:
            if offset == 0:
                _ISSUE_FETCH_WARNING = "issue list json parse failed:%s" % str(e)[:120]
                return []
            _ISSUE_FETCH_WARNING = "分页第 offset=%d 页 JSON 解析失败,仅覆盖前 %d 条" % (offset, len(out))
            _ISSUE_TRUNCATED = True
            break
        new = [i for i in page if i.get("id") not in seen]
        for i in new:
            seen.add(i.get("id"))
        out.extend(new)
        # 不足一页 => 已到末尾;无新项(纯重复) => 防御性停,避免死循环。
        if len(page) < page_size or not new:
            break
        offset += len(page)
    if len(out) >= ISSUE_LIMIT:
        _ISSUE_TRUNCATED = True
    return out

_RUNS_FX = None
_RUNS_PREFETCH = {}  # U42:并发预抓取结果 {iid: runs};命中即用,避免重复串行查询

def prefetch_runs(iids, workers):
    """U42/U43/X45/X61:并发预抓取多个 issue 的 runs,结果按 iid 稳定收敛。

    fetch_runs 内部只读子进程/读 fixture,线程安全;先把 fixture 缓存预热,
    再并发,避免 _RUNS_FX 懒加载竞争。workers<=1 退化串行。
    """
    if not iids:
        return
    # 预热 fixture 缓存(单线程一次),避免并发懒初始化竞争
    fx = os.environ.get("WATCHDOG_RUNS_FIXTURE")
    if fx and _RUNS_FX is None:
        fetch_runs(iids[0])  # 触发 _RUNS_FX 加载
    res = O.fetch_concurrent(iids, fetch_runs, workers=workers)
    for iid, r in res.items():
        if isinstance(r, list):
            _RUNS_PREFETCH[iid] = r

def fetch_runs(iid):
    global _RUNS_FX
    if iid in _RUNS_PREFETCH:               # U42:命中并发预抓取缓存
        return _RUNS_PREFETCH[iid]
    fail_ids = {x.strip() for x in os.environ.get("WATCHDOG_RUNS_FAIL_IDS", "").split(",") if x.strip()}
    if iid in fail_ids:
        _RUN_FETCH_WARNINGS[iid] = "forced runs fetch failure"
        return []
    _RUN_FETCH_WARNINGS.pop(iid, None)
    fx = os.environ.get("WATCHDOG_RUNS_FIXTURE")
    if fx:
        if _RUNS_FX is None:
            try: _RUNS_FX = json.load(open(fx))
            except Exception as e:
                _RUNS_FX = {}
                _RUN_FETCH_WARNINGS[iid] = "runs fixture read failed:%s" % str(e)[:120]
        return _RUNS_FX.get(iid, [])
    r = sh("multica", "issue", "runs", iid, "--output", "json")
    if r.returncode != 0:
        _RUN_FETCH_WARNINGS[iid] = (r.stderr or r.stdout or "runs fetch failed")[:200]
        return []
    try:
        d = json.loads(r.stdout)
    except Exception as e:
        _RUN_FETCH_WARNINGS[iid] = "runs json parse failed:%s" % str(e)[:120]
        return []
    return d if isinstance(d, list) else (d.get("runs", []) if d else [])

_COMM_FX = None
def last_comment_age(iid):
    """最近一条评论的 age(分钟);取不到返回 None。供 stage_stale 判'无新评论'。"""
    global _COMM_FX
    fx = os.environ.get("WATCHDOG_COMMENTS_FIXTURE")
    if fx:
        if _COMM_FX is None:
            try: _COMM_FX = json.load(open(fx))
            except Exception: _COMM_FX = {}
        return age_min(_COMM_FX.get(iid))
    d = jget("multica", "issue", "comment", "list", iid, "--recent", "1", "--output", "json")
    cs = d if isinstance(d, list) else (d.get("comments", d.get("items", [])) if d else [])
    if not cs: return None
    # --recent 1 返回最近活跃线程;取其中最新 created_at
    ts = []
    for c in cs:
        ts.append(c.get("created_at"))
        for rep in (c.get("replies") or []):
            ts.append(rep.get("created_at"))
    ages = [a for a in (age_min(t) for t in ts) if a is not None]
    return min(ages) if ages else None

_COMM_LIST_FX = None
_PASS_RE = re.compile(r"VERDICT[:：\s]*PASS|门禁\s*PASS|\bPASS\b", re.IGNORECASE)
_DONEREAL_RE = re.compile(r"DONE_REAL|VERDICT[:：\s]*PASS|门禁\s*PASS", re.IGNORECASE)
_VERDICT_FAIL_RE = re.compile(r"VERDICT[:：\s]*FAIL|门禁[:：\s]*FAIL", re.IGNORECASE)

def fetch_recent_comments(iid, n=5):
    """U06:取最近 N 条评论(线程根+回复展平),按时间倒序返回最多 N 条;取不到=[]。

    旧逻辑(last_comment_age)只看最近 1 条,无法对多条证据归类。本函数供证据来源
    归类(comment_has_trusted_pass)用,判断收口前是否真有可信门禁/审计 PASS。
    """
    global _COMM_LIST_FX
    fx = os.environ.get("WATCHDOG_COMMENT_LIST_FIXTURE")
    if fx:
        if _COMM_LIST_FX is None:
            try: _COMM_LIST_FX = json.load(open(fx))
            except Exception: _COMM_LIST_FX = {}
        cs = _COMM_LIST_FX.get(iid, [])
    else:
        d = jget("multica", "issue", "comment", "list", iid, "--recent", str(n), "--output", "json")
        cs = d if isinstance(d, list) else (d.get("comments", d.get("items", [])) if d else [])
    flat = []
    for c in (cs or []):
        flat.append(c)
        for rep in (c.get("replies") or []):
            flat.append(rep)
    flat.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return flat[:n]

def comment_has_trusted_pass(iid, n=5):
    """U06:最近 N 条评论里是否存在可信来源(门禁/审计/PR/真机)的 PASS 背书。

    用来给 U31 信任策略兜底:开发 run 自报 PASS 时,若评论里已有真门禁/审计 PASS,
    就不误报 evidence_untrusted。
    """
    for c in fetch_recent_comments(iid, n):
        body = c.get("content") or c.get("body") or ""
        if not _PASS_RE.search(body):
            continue
        agent = str(c.get("author_name") or c.get("agent_name") or c.get("author") or "")
        src = E.classify_source(body, agent=agent)
        if E.evidence_trust(src) == "trusted":
            return True
    return False

def _issue_done_real(issue):
    """done_real 抑制(总管 2026-06-10):issue 已 done/cancelled 且最近评论有可信
    门禁/审计/DONE_REAL PASS 背书 -> 视为已据证据收口。看门狗对这类 issue 忽略
    stale_evidence(旧PASS被新run覆盖)/ terminal_conflict(done后仍有run)/ 父子矛盾,
    避免"看门狗自建 run 顶掉 PASS -> 再报警 -> 再建 run"的自燃死循环。
    例外:状态被人工回退离开 done/cancelled(则不再命中此守卫),或出现新 FAIL
    (FAIL 会被线主脑改状态,状态随之离开 done)-> 仍正常告警。"""
    if issue.get("status") not in DONE_ST:
        return False
    # 权威信号:metadata.pipeline_status == done_real(总管据真实生产证据收口时置)
    md = issue.get("metadata")
    if md is None:
        md = (jget("multica", "issue", "get", issue.get("id"), "--output", "json") or {}).get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    if isinstance(md, dict) and str(md.get("pipeline_status", "")).lower() == "done_real":
        return True
    # 兜底:最近评论有 DONE_REAL/总管 PASS 标记 且无明确 VERDICT:FAIL
    has_dr = has_fail = False
    for c in fetch_recent_comments(issue.get("id"), 40):
        b = c.get("content") or c.get("body") or ""
        if _DONEREAL_RE.search(b):
            has_dr = True
        if _VERDICT_FAIL_RE.search(b):
            has_fail = True
    return has_dr and not has_fail


def pin_evidence_metadata(issue_id, anchor):
    """PL-132:把有效证据锚点 pin 到 issue metadata(latest_valid_evidence_* / gate_status)。

    纯脚本调用 `multica issue metadata set`;失败静默(看门狗不得因 metadata 写失败卡死)。
    仅在锚点字段相对当前 metadata 有变化时写,避免每 2 分钟重复刷同一值。
    返回写入的 key 列表(可能空)。
    """
    md = E.evidence_metadata_from_anchor(anchor)
    if not md or not issue_id:
        return []
    # 读当前 metadata,只写有变化的 key(幂等,防刷屏)
    cur = {}
    try:
        d = jget("multica", "issue", "metadata", "list", issue_id, "--output", "json")
        if isinstance(d, dict):
            cur = d.get("metadata", d) if "metadata" in d else d
    except Exception:
        cur = {}
    written = []
    for k, v in md.items():
        if str(cur.get(k, "")) == str(v):
            continue
        r = sh("multica", "issue", "metadata", "set", issue_id, "--key", k, "--value", str(v))
        if r.returncode == 0:
            written.append(k)
    return written


def audit_issue_evidence(issue, runs, stage, check_trust=False, write_dir=None, pin_metadata=False):
    """PL-132:旧 PASS 失效改为"基于最新有效门禁证据 run/commit"判定,返回 alerts(可能空)。

    根治 PL-128 死循环:不再"证据之后任何更新 run 都覆盖旧 PASS"。判定只认 E.evidence_gate_decision
    给出的"最新有效门禁证据":
      - pass            : 最新有效证据=PASS,无关 run(cancelled/纯派发/no_action/个人 mention/
                          无裁决 completed)不使其失效;pin latest_valid_evidence_* metadata。
      - fail            : 最新有效证据=FAIL,emit gate_fail_rework(返工点),**不**报 stale_evidence。
      - evidence_missing/no_evidence:本函数不重复告警 —— "最新 completed run 缺证据" 由
                          BOM-2 最新 run 检测(更精确,分 empty/missing/unverified),且经看门狗
                          alert_sigs 去重/冷却,不无限 rerun。本函数只负责 PASS/FAIL 锚点判定。

    保留:
      - U30 ledger 落盘(可追溯)。
      - U31:check_trust 时,收口/门禁阶段(done/gate)的 PASS 来源必须 trusted;开发自报先查
             最近评论是否有可信门禁/审计 PASS 背书,都没有才告警。
    """
    alerts = []
    if not runs:
        return alerts
    led = E.build_ledger(issue.get("id"), issue.get("identifier"), runs)
    decision = E.evidence_gate_decision(runs)
    led["gate_decision"] = {k: decision.get(k) for k in ("status", "reason", "anchor", "missing_run")}
    status = decision["status"]
    anchor = decision.get("anchor")

    if status == "pass":
        # 最新有效证据=PASS:旧 PASS 不被无关 run 覆盖。U31 来源信任仍需校验。
        anchor_run = next((r for r in runs if r.get("id") == anchor.get("run_id")), None)
        if check_trust:
            led_entry = next((e for e in led.get("entries", []) if e.get("run_id") == anchor.get("run_id")), None)
            src = (led_entry or {}).get("source")
            if not E.passes_trust_policy(stage, src) and not comment_has_trusted_pass(issue.get("id")):
                alerts.append(_mk("evidence_untrusted", issue, anchor_run,
                    "%s 阶段 PASS 证据来源=%s,且最近评论无可信门禁/审计 PASS 背书,疑似开发自证收口" % (
                        stage, src)))
        if pin_metadata:
            pin_evidence_metadata(issue.get("id"), anchor)
    elif status == "fail":
        # 最新有效证据=FAIL:输出返工点,不再用"旧 PASS 失效"话术。
        anchor_run = next((r for r in runs if r.get("id") == anchor.get("run_id")), None)
        alerts.append(_mk("gate_fail_rework", issue, anchor_run, decision["reason"]))
        if pin_metadata:
            pin_evidence_metadata(issue.get("id"), anchor)
    # evidence_missing / no_evidence:见 docstring,交由 BOM-2 最新 run 检测,本函数不重复告警。

    # U30:落盘 ledger(有有效证据或触发告警时写,避免无谓 IO)
    if write_dir is not None and (anchor or alerts):
        E.write_ledger(led, ledger_dir=write_dir)
    return alerts

def supplement_missing_parents(items):
    """U03:子任务引用的父 issue 若被分页/扫描范围截断不在本轮 items 中,逐个二次补全
    (multica issue get),避免父子关系判定(U23/空转待推进)因缺父而漏判。返回补全的父 issue 列表。

    只补我方(线小队/线主脑)名下子任务引用的、且不在本轮 items 的父 id,避免无谓拉全平台。
    """
    have = {i.get("id") for i in items if i.get("id")}
    owners = set(load_line_squads().values()) | set(brain_to_squad().keys())
    missing = []
    seen = set()
    for i in items:
        if i.get("assignee_id") not in owners:
            continue
        p = i.get("parent_issue_id")
        if p and p not in have and p not in seen:
            seen.add(p)
            missing.append(p)
    if not missing:
        return []
    # 离线:有 issue-get fixture 就读它;只设了 issue fixture(无 get fixture)则不触真台,跳过。
    fx = os.environ.get("WATCHDOG_ISSUE_GET_FIXTURE")
    fxdata = None
    if fx:
        try: fxdata = json.load(open(fx))
        except Exception: fxdata = {}
    elif os.environ.get("WATCHDOG_FIXTURE"):
        return []
    out = []
    for pid in missing:
        if fxdata is not None:
            issue = fxdata.get(pid)
        else:
            d = jget("multica", "issue", "get", pid, "--output", "json")
            issue = d if isinstance(d, dict) and d.get("id") else (
                d.get("issue") if isinstance(d, dict) else None)
        if isinstance(issue, dict) and issue.get("id"):
            out.append(issue)
    return out

def brain_to_squad():
    # 反查:线主脑 agent_id -> 所属 squad_id。用于识别历史遗留个人 assignee。
    return {bid: sid for sid, (bid, _n) in load_line_brains().items()}

def squad_assignee_ids(sid):
    ids = {sid}
    brain = load_line_brains().get(sid)
    if brain and brain[0]:
        ids.add(brain[0])
    return ids

def resolve_squad(assignee_id):
    """把 assignee(squad 或 线主脑 agent)归一到 squad_id,供路由用。"""
    if assignee_id in set(load_line_squads().values()):
        return assignee_id
    return brain_to_squad().get(assignee_id)

def owned_issues(items, statuses):
    """我方(线小队 + 线主脑自持)名下、状态在 statuses 内的 issue,并归一 _squad。"""
    scan = set(load_line_squads().values()) | set(brain_to_squad().keys())
    out = []
    for i in items:
        if i.get("assignee_id") in scan and i.get("status") in statuses:
            i = dict(i)
            i["_squad"] = resolve_squad(i.get("assignee_id"))  # 归一,路由按此
            out.append(i)
    return out

def active_issues(items):
    # 盯:① 派给所有线小队的活跃 issue;② 派给线主脑(agent)自持的历史遗留活跃 issue。
    return owned_issues(items, ACTIVE_ST)

def compute_idle(items):
    idx = {i.get("id"): i for i in items}
    children = {}
    for i in items:
        p = i.get("parent_issue_id")
        if p:
            children.setdefault(p, []).append(i)
    results = {}
    for sname, sid in load_line_squads().items():
        owners = squad_assignee_ids(sid)
        sq = [i for i in items if i.get("assignee_id") in owners]
        active = [i for i in sq if i.get("status") in ACTIVE_ST]
        backlog = [i for i in sq if i.get("status") == "backlog"]
        cand = set()
        for i in sq:
            p = i.get("parent_issue_id")
            if p: cand.add(p)
            if i.get("id") in children: cand.add(i.get("id"))
        pending_parents = []
        for pid in cand:
            P = idx.get(pid)
            if not P or P.get("status") in DONE_ST: continue
            ch = children.get(pid, [])
            if not ch: continue
            if any(c.get("status") in ACTIVE_ST for c in ch): continue
            all_done = all(c.get("status") in DONE_ST for c in ch)
            any_backlog = any(c.get("status") == "backlog" for c in ch)
            if all_done or any_backlog:
                pending_parents.append(P)
        stopped = len(active) == 0
        has_work = len(backlog) > 0 or len(pending_parents) > 0
        results[sid] = {
            "name": sname, "idle": stopped and has_work, "stopped": stopped,
            "backlog": len(backlog), "parents": len(pending_parents),
            "sig": "b%d|p%d" % (len(backlog), len(pending_parents)),
        }
    return results

def result_text(r):
    """把 run.result 规整成可检索文本(支持 dict / str / None)。同时返回 pr_url。"""
    res = r.get("result")
    if res is None:
        return "", ""
    if isinstance(res, str):
        return res, ""
    if isinstance(res, dict):
        return str(res.get("output") or ""), str(res.get("pr_url") or "")
    return str(res), ""

def is_empty_result(r):
    txt, pr = result_text(r)
    return not txt.strip() and not pr.strip()

def has_evidence(r):
    txt, pr = result_text(r)
    if pr.strip():
        return True
    return any(m in txt for m in EVIDENCE_MARKERS)

def is_misroute(r):
    txt, _ = result_text(r)
    return any(m in txt for m in MISROUTE_MARKERS)

def leading_cancelled(runs):
    n = 0
    for r in runs:
        if r.get("status") == "cancelled": n += 1
        else: break
    return n

def comment(issue_id, body, timeout=None):
    # 自测钩子:强制贴台失败(U37 兜底回归用),不触发任何真实平台写。
    if os.environ.get("WATCHDOG_COMMENT_FAIL"):
        return False, "forced comment failure"
    if os.environ.get("WATCHDOG_DRY_RUN"):
        print("[DRY] would comment -> %s (%d 字)" % (issue_id, len(body)))
        return True, ""
    if timeout is None:
        timeout = DEFAULT_ROUTE_TIMEOUT
    try:
        p = subprocess.run(["multica", "issue", "comment", "add", issue_id, "--content-stdin"],
                           input=body, text=True, capture_output=True, timeout=timeout)
        return p.returncode == 0, (p.stderr or p.stdout or "")[:200]
    except subprocess.TimeoutExpired:
        return False, "comment timeout %.0fs" % timeout

def squad_label(sid):
    for name, val in load_line_squads().items():
        if val == sid:
            return name
    return sid or "未知小队"

def forced_route_fail_ids():
    raw = os.environ.get("WATCHDOG_ROUTE_FAIL_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}

def route_to_squad(issue_id, squad_id, timeout=None):
    """把异常 issue 重新交给线小队,不点名个人。

    assign --to-id squad_id 保证 issue assignee 是小队；rerun 只重启当前小队
    assignment。Multica 内部会用小队 leader 执行,但派工对象仍是 squad。
    """
    if not issue_id or not squad_id:
        return False, "missing issue/squad"
    if timeout is None:
        timeout = DEFAULT_ROUTE_TIMEOUT
    if issue_id in forced_route_fail_ids():
        return False, "forced route failure"
    if os.environ.get("WATCHDOG_DRY_RUN"):
        print("[DRY] would assign/rerun -> %s %s" % (issue_id, squad_label(squad_id)))
        return True, ""
    a = sh("multica", "issue", "assign", issue_id, "--to-id", squad_id, timeout=timeout)
    if a.returncode != 0:
        return False, (a.stderr or a.stdout or "")[:200]
    r = sh("multica", "issue", "rerun", issue_id, timeout=timeout)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "")[:200]
    return True, ""

def reopen_parent(issue_id, to_status="in_progress", timeout=None):
    """X34:把假收口的父任务自动打回(status → in_progress)。

    符合 CLAUDE.md 状态纪律:FAIL/返工只能回 in_progress,严禁 done/cancelled。
    DRY_RUN 下只打印意图。返回 (ok, err)。
    """
    if not issue_id:
        return False, "missing issue"
    if timeout is None:
        timeout = DEFAULT_ROUTE_TIMEOUT
    if os.environ.get("WATCHDOG_DRY_RUN"):
        print("[DRY] would reopen parent -> %s status=%s" % (issue_id, to_status))
        return True, ""
    r = sh("multica", "issue", "status", issue_id, to_status, timeout=timeout)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "")[:200]
    return True, ""

# kind -> (路由目标, 中文标签)。route: 'crisis'=危机处理codex / 'brain'=线主脑 / None=只贴台
KIND_META = {
    "zombie":           ("crisis", "僵尸run"),
    "failed":           ("crisis", "失败run"),
    "cancelled":        ("brain",  "取消run(result为空)"),
    "evidence_missing": ("brain",  "无效完成(缺VERDICT/PR/URL/截图)"),
    "gate_misrouted":   ("brain",  "门禁误触发(占位岗,未走真千问)"),
    "stage_stale":      ("brain",  "阶段卡死(超时未推进)"),
    "no_run":           ("brain",  "无人接单(无run启动)"),
    "todo_no_claim":    ("brain",  "待接单(todo 无人认领)"),
    "in_progress_no_run": ("brain", "在办无run(中途断工)"),
    "blocked_stale":    ("brain",  "blocked 滞留(超时未解阻)"),
    "terminal_conflict": ("crisis", "收口矛盾(done/cancelled 仍有 running run)"),
    "personal_assignment": ("brain", "个人派单违规"),
    "coverage_limit":   (None,     "扫描覆盖到达上限"),
    "schema_drift":     (None,     "状态schema漂移(未登记新状态)"),
    "issue_fetch_failed": (None,    "issue列表读取失败"),
    "squad_discovery_failed": (None, "线小队发现失败"),
    "run_fetch_failed": (None,     "run列表读取失败"),
    # --- B段(U25-U40)新增 ---
    "evidence_unverified": ("brain", "证据无法核验(声称截图/PR但无真实产物)"),  # U28/U29
    "stale_evidence":   ("brain",  "旧PASS失效(rerun后被新run覆盖)"),          # U32/X32(PL-132 已弃用)
    "gate_fail_rework": ("brain",  "最新有效门禁证据=FAIL(返工点,非旧PASS失效)"),  # PL-132
    "evidence_untrusted": ("brain", "PASS来源不可信(收口阶段开发自证,无门禁/审计背书)"),  # U31
    "parent_done_child_open": ("brain", "父任务已收口但子任务未收口(父子矛盾)"),  # U23
    "parent_reopen": ("brain", "父任务假收口(子任务FAIL/未收口),已自动打回 in_progress"),  # X34
    "route_unconfirmed": ("brain", "续派后未确认新run启动"),                     # U36
    "probe_down":       (None,     "模型endpoint探针故障(Qwen/DeepSeek)"),       # U25/U26
    "device_offline":   (None,     "真机探针故障(adb/scrcpy)"),                 # U27
    "handoff_fallback": (None,     "PL-94贴台失败,已落本地兜底"),               # U37
    # --- PL-124 P2(U15/U59/U60/U61/U68/U69/U70/U71)主机级系统健康/治理告警 ---
    "cli_permission_drift": (None, "multica CLI 权限漂移(读取/派单会全失败)"),    # U15
    "permission_drift":  (None,    "root/fleet 文件权限漂移(缺失/属主/不可写)"),   # U70
    "memory_pressure":   (None,    "内存压力(可用内存低于阈值)"),                 # U59
    "disk_pressure":     (None,    "磁盘压力(已用达阈值,需按白名单清理)"),        # U60
    "memory_cleanup_due": (None,   "上下文记忆清理到期(每2小时)"),               # U61
    "cli_version_unpinned": (None, "Multica CLI 版本未 pin"),                      # U68
    "cli_version_drift": (None,    "Multica CLI 版本漂移(schema 可能变)"),         # U68
    "proxy_down":        (None,    "网络出口/代理不可用"),                         # U69
    "image_oversize":    (None,    "超阈值未压缩图片(吃 token)"),                 # U71
}

def alert_route(al):
    return al.get("route_override") or KIND_META.get(al["kind"], (None,))[0]

def squad_gen(squad_id):
    """X23/X25:某 squad 当前代际指纹 = squad_id + 当前 leader_id。leader 轮换即变。"""
    if not squad_id:
        return None
    brain = load_line_brains().get(squad_id)
    leader = brain[0] if brain else None
    return O.squad_generation(squad_id, leader)

def alert_state_key(al):
    # U14 + X25:去重键带上 squad_id + 代际(generation)+ iid。
    # 归档后用同一 T 编号重建的 issue 会拿到新的 issue UUID(iid),identifier 可能复用;
    # 只用 ident|kind 会让旧告警签名误命中新 issue 被静默吞掉。X25 进一步要求 key 含
    # squad 代际(leader 轮换/小队重建即换代际),不能只用 T 编号:leader 一换,
    # 旧代际的去重状态不得套到新代际上,告警需重新触发。
    return O.state_key(
        al.get("ident"), al.get("squad"), squad_gen(al.get("squad")),
        al.get("iid"), al.get("kind"))

def alert_hint(al):
    kind = al["kind"]
    route = alert_route(al)
    if route == "crisis":
        return "读 run 判根因:配置/环境类不要盲目 rerun,瞬时类可重跑,代码类转返工。"
    return {
        "cancelled": "最新关键 run 被取消且无结果,不得自动 done/reset,应重派返工或转异常处理。",
        "evidence_missing": "run completed 但没回写 VERDICT/PR/URL/截图,按无效完成处理,应补证据或重派。",
        "gate_misrouted": "门禁被占位岗误触发,不是真千问判定。应调用 line_bridge.py gate 走真门禁。",
        "stage_stale": "阶段超时未推进,应判定并推进返工/审计/gate/收口,不能停在口头已派。",
        "todo_no_claim": "todo 长期无人认领,确认是否漏派或小队满载,需安排接单。",
        "in_progress_no_run": "已 in_progress 但无任何 run 启动,疑似认领后断工,应核实并重启或重派。",
        "blocked_stale": "blocked 长期未解阻,应核实阻塞原因是否仍成立,推进解阻或转危机。",
        "evidence_unverified": "completed 但截图/PR证据无法核验(无图片附件/无可达URL/PR号不合法),按无效完成处理,需补真实可验证证据。",
        "stale_evidence": "rerun 后旧 PASS 已被更新 run 覆盖,旧证据失效,必须重新走门禁,不得用旧 PASS 收口。",
        "gate_fail_rework": "最新一条【有效门禁证据】(带 VERDICT 的 run)为 FAIL,应据该 FAIL run 的失败点返工,不得用更早的旧 PASS 收口;这不是『旧 PASS 被无关 run 覆盖』,而是当前门禁结论就是 FAIL。",
        "evidence_untrusted": "收口/门禁阶段的 PASS 来自开发自报,最近评论也无可信门禁/审计/PR/真机背书,不得据此收口,必须走真门禁(line_bridge.py gate)或专属审计复核。",
        "parent_done_child_open": "父任务已 done/cancelled,但仍有子任务停在活跃/阻塞态,父子状态矛盾,应回退父任务或先收口子任务,不得带病收口。",
        "parent_reopen": "父任务在子任务 FAIL/未收口时被收口=假收口,已自动打回 in_progress;请据子任务失败点先收口子任务再重审父任务,不得再带病 done。",
        "route_unconfirmed": "续派(assign/rerun)后未确认有新 run 启动,疑似路由成功但无人接,需核实并重派。",
        "clock_skew": "本地时钟与平台时间戳偏移过大(出现未来时间戳),僵尸/卡死/deadline 判定会被低估,先校时(NTP)再信任时间类告警。",
        "cross_squad_collab": "父任务的未收口子任务跨多个线小队,需线主脑统一协调推进/收口,不能各队只盯自己那部分。",
    }.get(kind, "判定并推进。")

def build_route_jobs(changed):
    """把多个 changed alert 拆成可并行的分流作业。

    - 同一 issue+squad 的多个异常合并成一个作业:避免重复 comment/assign/rerun。
    - 不同 issue/squad 拆成独立作业:允许并发分流,一个卡住不拖死其它线。
    """
    jobs = {}
    for al in changed:
        route = alert_route(al)
        squad_id = al.get("squad")
        if not route or not squad_id:
            continue
        key = (al["iid"], squad_id)
        job = jobs.setdefault(key, {
            "iid": al["iid"],
            "ident": al["ident"],
            "squad": squad_id,
            "route": "brain",
            "alerts": [],
            # X08:扫描时看到的 assignee_id,执行前回拉比对,防 TOCTOU 误派。
            "expected_aid": _SCAN_ASSIGNEE.get(al["iid"]),
        })
        if route == "crisis":
            job["route"] = "crisis"
        job["alerts"].append(al)
    out = list(jobs.values())
    # X23:build 时记录目标小队代际(squad_id+leader),执行前再比对,leader 轮换则作废本次路由。
    for j in out:
        j["gen"] = squad_gen(j.get("squad"))
    # X61:按稳定键(iid,squad)赋 seq,并发结果乱序到达后可按 seq 复盘重放。
    O.assign_sequence_ids(out)
    return out

def route_comment(job):
    labels = []
    for al in job["alerts"]:
        label = KIND_META.get(al["kind"], (None, al["kind"]))[1]
        labels.append("- [%s] %s(run %s, agent %s) → %s" % (
            label, al["why"], (al.get("run_id") or "")[:8], al.get("agent") or "", alert_hint(al)))
    return (
        "🚨 **看门狗自动分流**(小队路由,纯脚本,不判因)\n\n"
        "%s / %s 同时出现 %d 个异常:\n%s\n\n"
        "派工铁律:本告警只重新交给 %s 小队 assignment/rerun,不得使用个人 agent 链接点名派工。"
        "处理完在本 issue 中文回报。"
        % (squad_label(job["squad"]), job["ident"], len(job["alerts"]), "\n".join(labels), squad_label(job["squad"]))
    )

def confirm_rerun(issue_id, since_iso, timeout=None):
    """U36:assign/rerun 后二次确认确实有新 run 启动(防『路由成功但无人接』)。

    回拉该 issue 的 runs,看是否存在 created_at 晚于路由时刻的新 run,或仍有
    running run。返回 (confirmed: bool, info)。仅在显式开启 WATCHDOG_CONFIRM_RERUN
    且非 DRY_RUN 时由 execute_route_job 调用(生产 cron 开,离线回归默认关)。
    """
    _RUNS_PREFETCH.pop(issue_id, None)  # U36:确认须看路由后的最新 runs,绕过预抓取缓存
    runs = fetch_runs(issue_id)
    if _RUN_FETCH_WARNINGS.get(issue_id):
        return False, "确认时 run 列表读取失败:%s" % _RUN_FETCH_WARNINGS.get(issue_id)
    newer = E.run_created_after(runs, since_iso)
    if newer:
        return True, "新run %s(%s)已启动" % ((newer.get("id") or "")[:8], newer.get("status"))
    running = next((r for r in runs if r.get("status") == "running"), None)
    if running:
        return True, "已有 running run %s" % ((running.get("id") or "")[:8])
    return False, "路由后未发现晚于 %s 的新 run,疑似无人接单" % since_iso


def execute_route_job(job, timeout):
    seq = job.get("seq")  # X61:稳定序号,随结果回带,落盘/复盘按它排序(与到达顺序无关)
    try:
        since = now_utc().isoformat()
        # X23:执行前再取一次目标小队当前代际,与 build 时记录的代际比对。leader 在并发
        # 窗口内轮换/小队重建 → 代际变,本次路由作废(不把结果套到新 leader 上),改为告警。
        gen_ok, gen_reason = O.route_generation_guard(job.get("gen"), squad_gen(job.get("squad")))
        if not gen_ok:
            print("ROUTE_GEN_SKIP seq=%s %s/%s -> %s" % (
                seq, squad_label(job.get("squad")), job.get("ident"), gen_reason))
            return {
                "job": job, "seq": seq,
                "ok": False, "comment_ok": False, "route_ok": False,
                "err": "", "route_err": gen_reason,
                "gen_skipped": True,
                "confirmed": None, "confirm_info": "",
            }
        # X08:route 前回拉 issue 确认 assignee 未在扫描后被改动(已被接走/已重派)。
        # DRY_RUN 离线下跳过(不触发真 multica 调用);读取失败时 route_precheck 保守放行。
        if not os.environ.get("WATCHDOG_DRY_RUN") and job.get("expected_aid"):
            pc_ok, pc_reason = P.route_precheck(job.get("expected_aid"), fetch_issue_brief, job["iid"])
            if not pc_ok:
                print("ROUTE_PRECHECK_SKIP seq=%s %s/%s -> %s" % (
                    seq, squad_label(job.get("squad")), job.get("ident"), pc_reason))
                return {
                    "job": job, "seq": seq,
                    "ok": False, "comment_ok": False, "route_ok": False,
                    "err": "", "route_err": pc_reason,
                    "gen_skipped": True,
                    "confirmed": None, "confirm_info": "",
                }
        ok, err = comment(job["iid"], route_comment(job), timeout=timeout)
        routed_ok, routed_err = route_to_squad(job["iid"], job["squad"], timeout=timeout) if ok else (False, "comment failed")
        confirmed, confirm_info = None, ""
        # U36:仅当显式开启 WATCHDOG_CONFIRM_RERUN 时确认(生产 cron 开;离线回归用 fixture runs)。
        if routed_ok and os.environ.get("WATCHDOG_CONFIRM_RERUN"):
            confirmed, confirm_info = confirm_rerun(job["iid"], since, timeout=timeout)
        return {
            "job": job,
            "seq": seq,                    # X61
            "ok": ok and routed_ok,
            "comment_ok": ok,
            "route_ok": routed_ok,
            "err": err,
            "route_err": routed_err,
            "gen_skipped": False,
            "confirmed": confirmed,        # None=未确认(未开启) / True / False
            "confirm_info": confirm_info,
        }
    except Exception as e:
        return {
            "job": job,
            "seq": seq,                    # X61
            "ok": False,
            "comment_ok": False,
            "route_ok": False,
            "err": "route job exception:%s" % str(e)[:200],
            "route_err": "route job exception",
            "gen_skipped": False,
            "confirmed": None,
            "confirm_info": "",
        }

def run_route_jobs(jobs, workers, timeout):
    if not jobs:
        return []
    if not workers or int(workers) <= 0:
        workers = auto_route_workers()
    workers = max(1, min(int(workers), len(jobs)))
    if workers == 1:
        return [execute_route_job(j, timeout) for j in jobs]
    out = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(execute_route_job, j, timeout) for j in jobs]
        for fut in as_completed(futs):
            try:
                out.append(fut.result())
            except Exception as e:
                out.append({"job": {"ident": "UNKNOWN", "squad": "", "alerts": []},
                            "seq": None,
                            "ok": False, "comment_ok": False, "route_ok": False,
                            "err": "future exception:%s" % str(e)[:200],
                            "route_err": "future exception",
                            "gen_skipped": False,
                            "confirmed": None, "confirm_info": ""})
    # X61:并发到达顺序不稳定,按 build 时赋的 seq 稳定排序,保证每轮复盘顺序一致、可重放。
    return O.sort_by_sequence(out)

def plain_alert(kind, ident, iid, why, squad=None):
    return {
        "kind": kind,
        "ident": ident,
        "iid": iid,
        "squad": squad,
        "why": why,
        "agent": "",
        "run_id": "",
        "err": "",
    }

def infer_squad_from_issue(issue):
    text = " ".join(str(issue.get(k) or "") for k in ("identifier", "title", "body", "description"))
    m = re.search(r"(?:^|[^A-Za-z0-9])T0?(\d{1,2})(?:[^0-9]|$)", text)
    if not m:
        return None
    wanted = "线小队-T%02d" % int(m.group(1))
    return load_line_squads().get(wanted)

def system_alerts(items):
    alerts = []
    if _ISSUE_FETCH_WARNING:
        alerts.append(plain_alert(
            "issue_fetch_failed", "WATCHDOG-COVERAGE", HANDOFF,
            "Multica issue list 读取失败:%s;本轮不能证明生产线健康" % _ISSUE_FETCH_WARNING))
    sw = squad_discovery_warning()
    if sw:
        alerts.append(plain_alert(
            "squad_discovery_failed", "WATCHDOG-COVERAGE", HANDOFF,
            "线小队动态发现失败,已退回默认T01-T03,新增小队可能漏扫:%s" % sw))
    # U01/X14: 真分页后,只有累计达 ISSUE_LIMIT 硬上限或翻页中途失败才算截断;
    # 告警显示已覆盖条数与影响范围,而不是把"正好抓满一页"误报成漏扫。
    if _ISSUE_TRUNCATED:
        # X14:量化截断影响范围(预计漏扫数量),不再只说"可能漏扫"。
        cov = P.coverage_estimate(len(items), ISSUE_LIMIT)
        alerts.append(plain_alert(
            "coverage_limit", "WATCHDOG-COVERAGE", HANDOFF,
            "分页未取全:%s(硬上限 WATCHDOG_ISSUE_LIMIT=%d,预计漏扫 ≥%d 条),"
            "请调高上限或缩小过滤范围" % (cov["note"], ISSUE_LIMIT, cov["est_missed"])))
    squad_ids = set(load_line_squads().values())
    brain_ids = set(brain_to_squad().keys())
    scan_owners = squad_ids | brain_ids
    # U02/U07/U17: schema 漂移 —— 我方线小队/线主脑名下出现"已知状态全集"外的新状态,
    # 说明平台加了脚本没覆盖的状态,必须告警(否则新状态任务会被静默漏扫)。
    drift_seen = set()
    for i in items:
        status = i.get("status")
        if i.get("assignee_id") not in scan_owners:
            continue
        if status and status not in KNOWN_STATUSES and status not in drift_seen:
            drift_seen.add(status)
            alerts.append(plain_alert(
                "schema_drift", "WATCHDOG-SCHEMA", HANDOFF,
                "出现未登记的 issue 状态 '%s'(不在 KNOWN_STATUSES),脚本可能漏扫该类任务,"
                "请校对平台 schema 并补登" % status))
    # X11:把本轮 schema 漂移落成独立证据文件(与会被轮转的告警分离,永久留底可追溯)。
    if drift_seen:
        try:
            ev = P.schema_drift_evidence(drift_seen, KNOWN_STATUSES, now=now_utc())
            ev_path = P.write_schema_drift_evidence(ev, SCHEMA_DRIFT_DIR, now=now_utc())
            if ev_path:
                print("SCHEMA_DRIFT_EVIDENCE drift=%d -> %s" % (ev["drift_count"], ev_path))
        except Exception as e:
            print("SCHEMA_DRIFT_EVIDENCE_SKIP %s" % str(e)[:120])
    for i in items:
        status = i.get("status")
        if status not in ACTIVE_ST and status not in BLOCKING_ST and status != "backlog":
            continue
        assignee = i.get("assignee_id")
        if i.get("assignee_type") == "agent" and assignee not in brain_ids:
            sid = infer_squad_from_issue(i)
            alerts.append(plain_alert(
                "personal_assignment", i.get("identifier") or i.get("id"), i.get("id"),
                "任务派给个人 agent=%s,违反只派线小队规则" % ((assignee or "")[:8]),
                sid if sid in squad_ids else None))
    return alerts

def probe_alerts(env, want_device=False):
    """U25/U26/U27:模型 endpoint + 真机健康探针 → 统一告警(贴 PL-94,不路由小队)。

    离线自测:设 WATCHDOG_PROBE_FIXTURE={"qwen":false,"deepseek":true,"device_online":0}
    用固定结果替代真实网络/adb 探测;并需把 QWEN_API_URL/DEEPSEEK_API_URL 设为任意非空值。
    """
    fx = os.environ.get("WATCHDOG_PROBE_FIXTURE")
    ep_prober = None
    dev_runner = None
    if fx is not None:
        try:
            cfg = json.loads(fx) if fx.strip() else {}
        except Exception:
            cfg = {}
        qurl = env.get("QWEN_API_URL", "")
        durl = env.get("DEEPSEEK_API_URL", "")

        def ep_prober(url, timeout=6, _q=qurl, _d=durl, _c=cfg):
            if url and url == _q:
                return bool(_c.get("qwen", True)), "fixture"
            if url and url == _d:
                return bool(_c.get("deepseek", True)), "fixture"
            return True, "fixture"

        def dev_runner(timeout=8, _c=cfg):
            return int(_c.get("device_online", 1)), "fixture"

    out = []
    for kind, label, why in E.health_probe_alerts(env, ep_prober, dev_runner, want_device=want_device):
        out.append(plain_alert(kind, "WATCHDOG-HEALTH", HANDOFF, why))
    return out


def stage_progress_alert(issue, runs, deadline_min):
    """BOM-3 deadline 驱动的下一环节推进检测(状态机)。

    与旧的『卡死(最新run取消/失败)』分支互补:这里覆盖**上一阶段正常完成、
    但下一环节超 deadline 不推进**的断点(in_review 无审计 / 审计PASS无gate /
    gatePASS无done / gateFAIL无返工)。返回 (signal_run, why) 或 None。

    实现:在已抓取的 runs(newest-first)里找最新一条『阶段产出事件』run,
    若其后没有更新的 run(下一环节未启动)且已超 deadline → 告警。
    只用已抓取的 runs,无额外查询(每 issue O(len(runs)),不引入 N*M)。
    """
    status = issue.get("status")
    if status not in ("in_progress", "in_review"):
        return None
    sig_event = None; sig_run = None; has_newer = False
    for idx, r in enumerate(runs):
        txt, pr = result_text(r)
        ev, _typed = S.event_from_run(r, txt or pr)  # U21:优先 typed 字段,回退正则
        if ev in S.STAGE_EXPECTATION:        # gate_misrouted/None 不是推进事件,跳过
            sig_event, sig_run = ev, r
            has_newer = idx > 0              # 该事件之后已有更新 run = 下一环节已动
            break
    if not sig_event:
        return None
    res = S.stage_progress_overdue(
        sig_event, sig_run.get("created_at"), has_newer,
        now_utc(), deadline_min, issue_done=(status == "done"))
    if not res:
        return None
    why = "%s 阶段推进超时:%s(已 %.0f 分钟未推进至『%s』)" % (
        status, res["reason"], res["elapsed"], res["next"])
    return sig_run, why


def compute_stage_states(items, subset_ids=None):
    """U22:把每个活跃 issue 当前阶段(role-stage)落成可持久化的状态字段。

    取该 issue 最新一条『阶段产出事件』run,经 S.event_from_run(U21:优先 typed 字段)
    归一为 event + role(STAGE_ROLE),写进 stage_state。这样"当前在哪个 role/阶段"
    不再每轮从文本重算,可被状态板消费、也可与上一轮比对是否回退。
    返回 {iid: {ident, event, role, typed, at, status}}。runs 命中预抓取缓存,无额外查询。
    """
    out = {}
    for i in active_issues(items):
        iid = i.get("id")
        if subset_ids is not None and iid not in subset_ids:
            continue
        if _RUN_FETCH_WARNINGS.get(iid):
            continue
        runs = fetch_runs(iid)
        for r in runs:                       # runs newest-first
            txt, pr = result_text(r)
            ev, typed = S.event_from_run(r, txt or pr)
            if ev in S.STAGE_EXPECTATION or ev == "gate_misrouted":
                out[iid] = {
                    "ident": i.get("identifier"),
                    "event": ev,
                    "role": S.role_for_event(ev),
                    "typed": typed,
                    "at": r.get("created_at"),
                    "status": i.get("status"),
                }
                break
    return out


def clock_skew_alerts(items):
    """U08:本地时钟偏移探针。聚合 issue + 已抓取 runs 的平台时间戳,与本地 now 比对。

    平台回写的 created_at/updated_at 若出现"未来时间戳"(晚于本地 now 超容差),
    说明本地时钟落后(或平台超前),则本轮所有 age/僵尸/卡死/deadline 判定都不可信。
    命中只发一条系统级告警(贴 PL-94),不逐 issue 刷屏。
    """
    tol = float(os.environ.get("WATCHDOG_CLOCK_SKEW_MIN", "2"))
    tss = []
    for i in items:
        tss.append(i.get("updated_at"))
        tss.append(i.get("created_at"))
    for iid, runs in _RUNS_PREFETCH.items():
        for r in (runs or []):
            tss.append(r.get("created_at"))
    skewed, why, _skew = S.clock_skew_alert(now_utc(), tss, tolerance_min=tol)
    if skewed:
        return [plain_alert("clock_skew", "WATCHDOG-CLOCK", HANDOFF, why)]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# PL-124 P2:主机级系统健康/权限/资源/版本探针(聚合 → PL-94 系统告警)
# 真实采样仅生产 cron 跑;离线测试用 WATCHDOG_SYSHEALTH_FIXTURE 注入,不触真 proc/df/网络。
# ─────────────────────────────────────────────────────────────────────────────
CRITICAL_FILES = [
    "/home/fleet/line-config/line_watchdog.py",
    "/home/fleet/line-config/run_watchdog.sh",
    "/home/fleet/line-config/watchdog_state.json",
    "/home/fleet/line-config/line_states.py",
]
CLEANUP_MARKER = os.environ.get("WATCHDOG_CLEANUP_MARKER",
                                "/home/fleet/line-config/memory/.cleanup_marker")


def _default_proxy_prober(url, timeout=4):
    """U69 默认出口探针:解析 host:port 做 TCP 连接,能建连即视为出口活着。返回 (ok, info)。"""
    import socket as _sk
    try:
        from urllib.parse import urlparse
        u = urlparse(url if "://" in url else "http://" + url)
        host, port = u.hostname, (u.port or 80)
        if not host:
            return False, "无法解析 host"
        with _sk.create_connection((host, port), timeout=timeout):
            return True, "TCP %s:%d ok" % (host, port)
    except Exception as e:
        return False, str(e)[:120]


def _fixture_proxy_prober(facts):
    down = set(facts.get("proxy_down_urls", []) or [])

    def _p(url, _down=down):
        return (url not in _down), ("fixture down" if url in _down else "fixture ok")
    return _p


def _sample_system_facts():
    """生产采样:内存/磁盘/CLI/文件权限/代理/清理 marker。任何子项失败都吞掉不抛。"""
    facts = {}
    try:
        mt = ma = None
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    mt = float(ln.split()[1])
                elif ln.startswith("MemAvailable:"):
                    ma = float(ln.split()[1])
        if mt and ma:
            facts["mem_avail_pct"] = 100.0 * ma / mt
    except Exception:
        pass
    try:
        import shutil as _sh
        t, _u, fr = _sh.disk_usage("/home/fleet/line-config")
        if t:
            facts["disk_used_pct"] = 100.0 * (t - fr) / t
    except Exception:
        pass
    try:
        p = subprocess.run(["multica", "--version"], capture_output=True, text=True, timeout=15)
        facts["cli_ok"] = (p.returncode == 0)
        facts["cli_version"] = ((p.stdout or "").splitlines() or [""])[0].strip()
    except Exception as e:
        facts["cli_ok"] = False
        facts["cli_version"] = ""
        facts["cli_err"] = str(e)[:120]
    files = []
    for path in CRITICAL_FILES:
        ex = os.path.exists(path)
        owner = writable = None
        if ex:
            try:
                import pwd
                owner = pwd.getpwuid(os.stat(path).st_uid).pw_name
            except Exception:
                owner = None
            writable = os.access(path, os.W_OK)
        files.append({"path": path, "exists": ex, "owner": owner, "writable": writable})
    facts["files"] = files
    facts["proxies"] = [{"name": k, "url": os.environ.get(k, "")}
                        for k in ("HTTPS_PROXY", "HTTP_PROXY") if os.environ.get(k)]
    last = None
    try:
        if os.path.exists(CLEANUP_MARKER):
            with open(CLEANUP_MARKER) as f:
                last = f.read().strip()
    except Exception:
        pass
    facts["mem_cleanup_last"] = last
    return facts


def _gather_image_attachments(items):
    out = []
    for i in items or []:
        for a in (i.get("attachments") or []):
            out.append(a)
    return out


def _touch_cleanup_marker(now_iso):
    """U61:记忆清理到期后落 marker(下次 2h 内不再重复告警)。失败静默。"""
    try:
        os.makedirs(os.path.dirname(CLEANUP_MARKER), exist_ok=True)
        tmp = "%s.tmp.%d" % (CLEANUP_MARKER, os.getpid())
        with open(tmp, "w") as f:
            f.write(now_iso)
        os.replace(tmp, CLEANUP_MARKER)
    except Exception:
        pass


def system_health_alerts(env, items=None):
    """U15/U59/U60/U61/U68/U69/U70/U71:主机级健康/权限/资源/版本/图片探针 → PL-94 系统告警。

    离线:设 WATCHDOG_SYSHEALTH_FIXTURE=<json> 注入 facts,完全不触真 proc/df/subprocess/网络。
    在线:_sample_system_facts() 真实采样;代理用 TCP 连接探;图片从 items 的 attachments 取。
    """
    fx = os.environ.get("WATCHDOG_SYSHEALTH_FIXTURE")
    offline = fx is not None
    if offline:
        try:
            facts = json.loads(fx) if fx.strip() else {}
        except Exception:
            facts = {}
        prober = _fixture_proxy_prober(facts)
        atts = facts.get("attachments") or []
    else:
        facts = _sample_system_facts()
        prober = _default_proxy_prober
        atts = _gather_image_attachments(items)

    out = []
    # U15 + U70:权限漂移
    for kind, label, why in S.permission_drift_alert(facts.get("cli_ok"), facts.get("files")):
        out.append(plain_alert(kind, label, HANDOFF, why))
    # U59 + U60:内存/磁盘压力
    for kind, label, why in S.resource_pressure_alert(
            facts.get("mem_avail_pct"), facts.get("disk_used_pct"),
            mem_floor_pct=float(env.get("WATCHDOG_MEM_FLOOR_PCT", "8")),
            disk_ceil_pct=float(env.get("WATCHDOG_DISK_CEIL_PCT", "90"))):
        out.append(plain_alert(kind, label, HANDOFF, why))
    # U61:记忆清理到期(到期告警一次后落 marker,2h 内静默;离线不落 marker)
    due, _hrs, why = S.memory_cleanup_due(
        facts.get("mem_cleanup_last"), now_utc(),
        interval_hours=float(env.get("WATCHDOG_MEM_CLEANUP_H", "2")))
    if due:
        out.append(plain_alert("memory_cleanup_due", "WATCHDOG-MEMCLEAN", HANDOFF, why))
        if not offline:
            _touch_cleanup_marker(now_utc().isoformat())
    # U68:CLI 版本 pin 漂移
    for kind, label, why in S.cli_version_drift_alert(
            facts.get("cli_version"), env.get("WATCHDOG_CLI_VERSION_PIN", "")):
        out.append(plain_alert(kind, label, HANDOFF, why))
    # U69:网络出口/代理健康
    for kind, label, why in S.proxy_health_alert(facts.get("proxies"), prober):
        out.append(plain_alert(kind, label, HANDOFF, why))
    # U71:图片压缩/token 保护
    for kind, label, why in S.image_oversize_alert(
            atts, max_bytes=int(float(env.get("WATCHDOG_IMG_MAX_MB", "2")) * 1024 * 1024)):
        out.append(plain_alert(kind, label, HANDOFF, why))
    return out


def detect(items, zombie_min, stale_min, recent_min=180.0, active_subset_ids=None,
           status_hist=None):
    """返回 alerts 列表。每个 alert: kind,ident,iid,why,squad,agent,run_id,err,sig

    active_subset_ids(U45/X62):仅处理这批活跃 issue(分片/续扫用);None=全量。
    blocked/terminal 收口扫描不分片(量小、且必须每轮看)。
    status_hist(X35):{iid: 上一轮状态},用于判 backlog→todo 提升后给认领宽限。
    """
    status_hist = status_hist or {}
    alerts = []
    for i in active_issues(items):
        if active_subset_ids is not None and i.get("id") not in active_subset_ids:
            continue
        ident = i.get("identifier"); iid = i.get("id"); squad = i.get("assignee_id")
        status = i.get("status")
        runs = fetch_runs(iid)
        if _RUN_FETCH_WARNINGS.get(iid):
            alerts.append(plain_alert(
                "run_fetch_failed", ident, iid,
                "run列表读取失败:%s;不能证明该任务有人干活" % _RUN_FETCH_WARNINGS.get(iid),
                i.get("_squad")))
            continue
        if not runs:
            comm_age = last_comment_age(iid)
            issue_age = age_min(i.get("updated_at") or i.get("created_at"))
            ref_age = comm_age if comm_age is not None else issue_age
            if ref_age is None or ref_age > stale_min:
                why_age = "未知时长" if ref_age is None else "%.0f分钟" % ref_age
                # U20: 区分"未开始(todo 无人认领)"与"中途断工(in_progress 无 run)",
                # 二者根因/处置不同,不再混成同一个 no_run。
                if status == "todo":
                    # X35:刚从 backlog 提升到 todo 的认领宽限 —— 窗口内不报无人认领
                    # (认领/起 run 需要时间,旧逻辑会把"刚放出来还没接"误判成"无人认领")。
                    if S.in_claim_grace(status_hist.get(iid),
                                        i.get("updated_at") or i.get("created_at"),
                                        now_utc(), CLAIM_GRACE_MIN):
                        print("CLAIM_GRACE_SKIP %s(刚 backlog→todo,认领宽限 %.0f 分钟内)" % (
                            ident, CLAIM_GRACE_MIN))
                    else:
                        alerts.append(_mk("todo_no_claim", i, None, "todo 已%s无人认领(无run启动)" % why_age))
                elif status == "in_progress":
                    alerts.append(_mk("in_progress_no_run", i, None, "in_progress 已%s无任何run(疑似断工)" % why_age))
                else:
                    alerts.append(_mk("no_run", i, None, "%s 已%s无任何run启动" % (status, why_age)))
            continue
        latest = runs[0] if runs else None

        # --- 逐 run:僵尸/失败 ---
        # recent_min 窗口:只报近期失败,避免把数小时前的历史失败当成当前异常反复刷屏
        # (扩到线主脑自持 issue 后,老 issue 历史里常有早已处理的 failed run)。
        for r in runs:
            stt = r.get("status"); am = age_min(r.get("created_at"))
            if stt == "running" and am is not None and am > zombie_min:
                alerts.append(_mk("zombie", i, r, "僵尸(running %.0f分钟)" % am))
            elif stt == "failed" and (am is None or am <= recent_min):
                alerts.append(_mk("failed", i, r, "失败run(%.0f分钟前)" % (am or 0)))

        # --- 最新关键 run:取消/无效完成(BOM-2) ---
        if latest:
            lst = latest.get("status")
            if lst == "cancelled" and is_empty_result(latest):
                lc = leading_cancelled(runs)
                why = "最新run取消且result为空" + (";连续取消%d次" % lc if lc >= 2 else "")
                a = _mk("cancelled", i, latest, why)
                a["streak"] = lc
                # 连续取消升级为危机
                if lc >= 2:
                    a["route_override"] = "crisis"
                alerts.append(a)
            elif lst == "completed":
                # PL-132:no_action/纯派发/个人 mention 触发的 completed run 不是开发/门禁完成,
                # 不得当"无效完成(缺证据)"告警(否则 PL-128 那种每轮 latest 都是协调 run 会反复误报)。
                _role_cat = E.run_evidence_role(latest)["category"]
                if _role_cat in ("no_action", "dispatch", "personal_mention"):
                    pass
                elif is_empty_result(latest):
                    alerts.append(_mk("evidence_missing", i, latest, "最新run completed 但 result 为空"))
                elif not has_evidence(latest):
                    alerts.append(_mk("evidence_missing", i, latest, "最新run completed 但无 VERDICT/PR/URL/截图证据"))
                else:
                    # U28/U29:有证据标记,但截图/PR 证据无法核验(文本占位/裸PR号) -> 无效完成
                    _txt, _pr = result_text(latest)
                    _combo = ("%s %s" % (_txt, _pr)).strip()
                    reasons = E.audit_completed_evidence(_combo)
                    # X46:声称有截图/URL 但本轮核验不到,且 run 刚完成(上传宽限内)→ 截图可能
                    # 还在异步上传/落库,先不判无效完成,等下一轮附件到位再核(避免误报触发返工)。
                    _fin = latest.get("finished_at") or latest.get("updated_at") or latest.get("created_at")
                    if reasons and E.evidence_grace_active(
                            _fin, now_utc(), EVIDENCE_GRACE_MIN,
                            claims_artifact=E.claims_artifact_pending(_combo)):
                        print("EVIDENCE_GRACE_SKIP %s(声称截图/URL,上传宽限 %.0f 分钟内,暂不判 evidence_unverified)" % (
                            ident, EVIDENCE_GRACE_MIN))
                    elif reasons:
                        alerts.append(_mk("evidence_unverified", i, latest,
                                          "最新run completed 但证据无法核验:" + ";".join(reasons)))

        # --- 门禁误触发(BOM-5):近期(recent_min 内)run 命中占位岗误触发标记 ---
        for r in runs[:5]:
            am = age_min(r.get("created_at"))
            if r.get("status") == "completed" and is_misroute(r) and (am is None or am <= recent_min):
                alerts.append(_mk("gate_misrouted", i, r, "占位岗门禁误触发,未走真千问 line_bridge.py gate"))
                break

        # --- U30/U31/U32 证据审计:in_review=门禁阶段(PASS 须可信);其它活跃态只查新鲜度 ---
        ev_stage = "gate" if status == "in_review" else "development"
        alerts.extend(audit_issue_evidence(
            i, runs, ev_stage, check_trust=(status == "in_review"),
            write_dir=LEDGER_DIR, pin_metadata=WRITE_METADATA))

        # --- 阶段推进(BOM-3):两条互补路径,统一 kind=stage_stale ---
        if status in ("in_progress", "in_review"):
            stale_hit = False
            # (a) 卡死:无活跃 run + 最新关键 run 取消/失败 + 超过 stale_min 无新评论
            run_age = age_min(latest.get("created_at")) if latest else None
            comm_age = last_comment_age(iid)
            has_active_run = any(r.get("status") == "running" for r in runs)
            latest_bad = latest is not None and latest.get("status") in ("cancelled", "failed")
            stale_comment = (comm_age is None) or (comm_age > stale_min)
            stale_run = (run_age is None) or (run_age > stale_min)
            if (not has_active_run) and latest_bad and stale_comment and stale_run:
                why = "%s 超 %.0f 分钟未推进(最新run=%s,无活跃run)" % (
                    status, comm_age if comm_age is not None else (run_age or 0), latest.get("status"))
                alerts.append(_mk("stage_stale", i, latest, why))
                stale_hit = True
            # (b) deadline 驱动:上一阶段正常完成,但下一环节超时未推进(状态机)
            if not stale_hit:
                prog = stage_progress_alert(i, runs, stale_min)
                if prog:
                    prun, pwhy = prog
                    alerts.append(_mk("stage_stale", i, prun, pwhy))

    # --- U02:blocked 滞留检测(blocked 是平台真实状态,旧逻辑只看 ACTIVE_ST 会静默) ---
    for i in owned_issues(items, BLOCKING_ST):
        age = age_min(i.get("updated_at") or i.get("created_at"))
        if age is None or age > stale_min:
            why_age = "未知时长" if age is None else "%.0f分钟" % age
            alerts.append(_mk("blocked_stale", i, None, "blocked 已%s未解阻" % why_age))

    # --- U23:父任务已 done/cancelled 但仍有未收口子任务(父子矛盾,独立检测)---
    children_idx = {}
    for c in items:
        p = c.get("parent_issue_id")
        if p:
            children_idx.setdefault(p, []).append(c)
    for i in owned_issues(items, DONE_ST):
        if _issue_done_real(i):
            continue  # done_real 父任务:已据证据收口,不就子任务未收口报父子矛盾(防自燃)
        ch = children_idx.get(i.get("id"), [])
        open_ch = [c for c in ch if c.get("status") not in DONE_ST]
        if open_ch:
            kids = ",".join("%s(%s)" % (c.get("identifier"), c.get("status")) for c in open_ch[:5])
            more = "等%d个" % len(open_ch) if len(open_ch) > 5 else ""
            # X34:父任务已 done/cancelled 但子任务 FAIL(blocked)或未收口 → 父『完成』为假,
            # 不只告警,要自动打回父任务到 in_progress(动作由 main 在 --route 下执行)。
            dec = S.parent_reopen_decision(
                i.get("status"), [c.get("status") for c in ch], parent_done_real=False)
            if dec["reopen"]:
                a = _mk("parent_reopen", i, None,
                        "%s(子任务:%s%s)" % (dec["reason"], kids, more))
                a["reopen_to"] = dec["to_status"]
                alerts.append(a)
            else:
                alerts.append(_mk("parent_done_child_open", i, None,
                    "父任务已 %s 但仍有 %d 个未收口子任务:%s%s" % (
                        i.get("status"), len(open_ch), kids, more)))

    # --- U13:跨小队协作建模 —— 同一父任务的未收口子任务分散在 ≥2 个线小队 ---
    # 旧模型假设"一线=一队、父子同队",跨队协作(一个父任务由多队合干)不被建模,
    # 单队视图各自只盯自己那几个子任务、互相看不到对方进度,父任务整体推进会漏判。
    # 这里把跨队归属显式建模成一条告警,交线主脑统一协调(squad=None → 走 PL-94,不误派单队)。
    owner_squads = set(load_line_squads().values()) | set(brain_to_squad().keys())
    items_by_id = {i.get("id"): i for i in items}
    for pid, ch in children_idx.items():
        mine = [c for c in ch
                if c.get("status") not in DONE_ST and c.get("assignee_id") in owner_squads]
        sq_map = {}
        for c in mine:
            sid = resolve_squad(c.get("assignee_id"))
            if sid:
                sq_map.setdefault(sid, []).append(c)
        if len(sq_map) >= 2:
            parent = items_by_id.get(pid)
            pident = parent.get("identifier") if parent else "(父%s)" % (pid or "")[:8]
            squads_desc = ",".join(
                "%s(%d项)" % (squad_label(sid), len(cs)) for sid, cs in sq_map.items())
            alerts.append(plain_alert(
                "cross_squad_collab", pident, pid,
                "父任务的未收口子任务跨 %d 个线小队协作:%s,需线主脑统一协调推进/收口"
                "(单队视图会互相漏判对方进度)" % (len(sq_map), squads_desc),
                None))

    # --- U19/X04:收口矛盾 —— done/cancelled 的 issue 仍有 running run(表面完成,后台还在跑) ---
    for i in owned_issues(items, DONE_ST):
        if _issue_done_real(i):
            continue  # done_real:已据证据收口,抑制 terminal_conflict/stale_evidence(防自燃),除非状态被回退/新FAIL
        age = age_min(i.get("updated_at") or i.get("created_at"))
        if age is not None and age > TERMINAL_SCAN_MIN:
            continue  # 只查近期收口的,历史 done 不反复拉 run
        runs = fetch_runs(i.get("id"))
        if _RUN_FETCH_WARNINGS.get(i.get("id")):
            continue
        running = next((r for r in runs if r.get("status") == "running"), None)
        if running:
            ram = age_min(running.get("created_at"))
            alerts.append(_mk("terminal_conflict", i, running,
                "issue 状态=%s 但仍有 running run(%s,%s)" % (
                    i.get("status"),
                    (running.get("id") or "")[:8],
                    "未知时长" if ram is None else "%.0f分钟" % ram)))
        # --- U30/U31/U32:done/cancelled 收口阶段证据审计(PASS 须可信来源,旧 PASS 不得被新失败覆盖)---
        if i.get("status") == "done":
            alerts.extend(audit_issue_evidence(
                i, runs, "done", check_trust=True, write_dir=LEDGER_DIR, pin_metadata=WRITE_METADATA))
    return alerts

def _mk(kind, issue, run, why):
    return {
        "kind": kind, "ident": issue.get("identifier"), "iid": issue.get("id"),
        "squad": issue.get("_squad") or resolve_squad(issue.get("assignee_id")), "why": why,
        "agent": (run.get("agent_id") or "")[:8] if run else "",
        "run_id": (run.get("id") or "") if run else "",
        "err": (run.get("error") or "" if run else "")[:400],
    }

def squads_owner_map():
    """{squad_name: set(owner_ids)} —— 含小队 id 与线主脑 agent id。供线级 metrics。"""
    return {name: squad_assignee_ids(sid) for name, sid in load_line_squads().items()}


def fetch_squad_members():
    """X24/X27:拉每条线的岗位成员 -> {squad_name: [{id, name, role}, ...]},供
    line_partial.member_headcount 做成员维度统计(每线人数/工作/闲置/卡死)。

    离线/测试:WATCHDOG_MEMBERS_FIXTURE 指向 json({squad_name 或 squad_id: [member记录]});
    在线:逐队 `multica squad member list <sid> --output json`,把 member_id 归一成 id。
    任一队拉取失败静默跳过(不得因成员探查失败卡死可观测落盘)。"""
    fx = os.environ.get("WATCHDOG_MEMBERS_FIXTURE")
    fxdata = None
    if fx:
        try:
            fxdata = json.load(open(fx))
        except Exception:
            fxdata = {}

    def _norm(recs):
        out = []
        for m in recs or []:
            mid = m.get("id") or m.get("member_id")
            if not mid:
                continue
            out.append({"id": mid, "name": m.get("name") or mid,
                        "role": m.get("role") or "member",
                        "member_type": m.get("member_type") or "agent"})
        return out

    out = {}
    for name, sid in load_line_squads().items():
        if fxdata is not None:
            recs = fxdata.get(name)
            if recs is None:
                recs = fxdata.get(sid, [])
        else:
            recs = jget("multica", "squad", "member", "list", sid, "--output", "json") or []
        out[name] = _norm(recs)
    return out


def emit_observability(a, st, items, alerts, idle_results, closed_loop, cycle_ms, scan_ok, scanned,
                       scan_state="full", route_results=None):
    """U50/U53/U55/U56/U63:写状态板 JSON / 心跳 / metrics / 闭环链(仅 --emit-observability)。

    metrics/closed_loop 入 state(带 cap);status/heartbeat 落独立 JSON 供画布消费。
    """
    if not a.emit_observability:
        return
    now = now_utc()
    # X07/X10:partial-scan(覆盖截断 / deadline 命中)= 本轮扫描不完整,
    # 不得推进 last_successful_scan,也不得把状态板判成 green。
    full = (scan_state == "full")
    line_stats = O.line_metrics(items, squads_owner_map(), alerts)
    # U53/U64:单轮指标入历史(带 cap)
    rec = O.cycle_metrics(now, scan_ok and not alerts and full, cycle_ms, scanned, alerts, line_stats)
    st["metrics"] = O.append_capped(st.get("metrics", []), rec, cap=O_METRICS_CAP)
    # U55:闭环链入 state(带 cap)
    st["closed_loop"] = closed_loop[-O_CLOSEDLOOP_CAP:]
    # U56/U58:心跳(只有本轮 issue 抓取成功且扫描完整才推进 last_successful_scan)
    prev_hb = {}
    try:
        prev_hb = json.load(open(HEARTBEAT_F))
    except Exception:
        pass
    hb = O.build_heartbeat(now, scan_ok and full, cycle_ms, scanned, len(alerts), prev_hb)
    if _DISABLED_STATE[0]:
        hb["disabled"] = True
        hb["disabled_reason"] = _DISABLED_STATE[1]
    O.write_json_atomic(hb, HEARTBEAT_F)
    # U50/U57/X50:状态板(画布红灯)
    status = O.build_status(now, scan_ok and not alerts, alerts, line_stats, cycle_ms,
                            hb.get("last_successful_scan"), scan_state=scan_state)
    # PL-137:停用态在状态板打 disabled 签名(overall=disabled),供画布/审计辨识"停用但存活"。
    if _DISABLED_STATE[0]:
        status["disabled"] = True
        status["disabled_reason"] = _DISABLED_STATE[1]
        status["overall"] = "disabled"
    # X24/X27:成员维度统计接入主路径(每线岗位人数/工作/闲置/卡死),写进状态板供画布消费。
    try:
        member_stats = P.member_headcount(fetch_squad_members(), items, alerts)
        status["member_stats"] = member_stats
        print("MEMBER_STATS lines=%d total_headcount=%d" % (
            len(member_stats), sum(v.get("headcount", 0) for v in member_stats.values())))
    except Exception as e:
        print("MEMBER_STATS_SKIP 成员维度统计失败(不阻断可观测落盘):%s" % str(e)[:120])
    # X71:状态板防篡改签名 —— 配了密钥(WATCHDOG_SIGN_KEY[_FILE])就给状态板盖 HMAC-SHA256
    # sig 字段,画布/审计侧可 verify_payload_signature 校验来源未被改。
    # (脚本侧签名已并入现役落盘;画布消费端验签接入仍属平台侧,见 PL-140 BLOCKED 结论。)
    try:
        sign_key = P.load_sign_key(os.environ)
        if sign_key:
            status = P.sign_status(status, sign_key)
            print("STATUS_SIGNED 状态板已签名(HMAC-SHA256,sig 字段)")
    except Exception as e:
        print("STATUS_SIGN_SKIP %s" % str(e)[:120])
    O.write_json_atomic(status, STATUS_F)
    # X67:watchdog_state / status.json / heartbeat.json 三者互校验,落进可观测输出。
    # 自报状态自相矛盾(时间不同步 / full 扫描但心跳 not ok / checksum 不符)时告警留痕。
    try:
        xv = P.cross_validate(st, status, hb)
        if xv:
            print("CROSS_VALIDATE_WARN 三文件互校不一致 %d 项:%s" % (len(xv), "; ".join(xv)))
            E.append_fallback("watchdog 三文件互校不一致:%s" % "; ".join(xv), "cross_validate")
        else:
            print("CROSS_VALIDATE_OK state/status/heartbeat 三者一致")
    except Exception as e:
        print("CROSS_VALIDATE_SKIP %s" % str(e)[:120])
    # U51:每轮 evidence ledger 落账(per-cycle 全局快照,后续变更可形式化追责)
    cyc = E.build_cycle_ledger(now, scanned, alerts, scan_state,
                               route_results=route_results,
                               gate_status=status.get("overall"),
                               canary_pct=getattr(a, "canary_route_pct", None))
    cyc_path = E.write_cycle_ledger(cyc)
    print("OBSERVABILITY_EMITTED status=%s lines=%d metrics=%d closed_loop=%d cycle_ledger=%s" % (
        status["overall"], len(line_stats), len(st["metrics"]), len(st["closed_loop"]),
        "ok" if cyc_path else "fail"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zombie-min", type=float, default=40.0, help="run running 超过这么多分钟=僵尸")
    ap.add_argument("--stale-min", type=float, default=5.0, help="阶段超过这么多分钟无推进=卡死")
    ap.add_argument("--recent-min", type=float, default=180.0, help="失败/门禁误触发只报这么多分钟内的,避免历史失败刷屏")
    ap.add_argument("--post", action="store_true", help="把告警贴到 PL-94")
    ap.add_argument("--route", action="store_true", help="把新异常分流给对应线小队")
    ap.add_argument("--route-workers", type=int, default=0, help="分流并发数;0=自动 max(24, 线小队数*3)")
    ap.add_argument("--route-timeout", type=float, default=DEFAULT_ROUTE_TIMEOUT, help="每个分流动作的单步超时秒数")
    ap.add_argument("--auto-rerun", action="store_true", help="(默认不用)盲目自动重启")
    ap.add_argument("--probe", action="store_true", help="U25/U26:探 Qwen/DeepSeek endpoint 健康并纳入告警")
    ap.add_argument("--probe-device", action="store_true", help="U27:同时探真机(adb)在线;需 --probe")
    # ===== C段(U41-U64)新增 =====
    ap.add_argument("--fetch-workers", type=int, default=8, help="U42:并发预抓取 runs 的线程数")
    ap.add_argument("--max-active-per-cycle", type=int, default=0,
                    help="U45:单轮最多处理多少活跃 issue(0=全量);超出的下轮按 cursor 续扫")
    ap.add_argument("--cycle-deadline-s", type=float, default=0.0,
                    help="U44:单轮硬性时间预算秒数(0=不限时);超时跳过收口扫描并记录")
    ap.add_argument("--emit-observability", action="store_true",
                    help="U50/U53/U56:写 watchdog_status.json / 心跳 / metrics / 闭环链")
    ap.add_argument("--alert-chunk-chars", type=int, default=ALERT_CHUNK_CHARS,
                    help="U48:单条 PL-94 告警最大字符数,超长自动分块")
    ap.add_argument("--sys-health", action="store_true",
                    help="U15/U59/U60/U61/U68/U69/U70/U71:主机级权限/内存/磁盘/CLI版本/代理/图片探针纳入告警")
    ap.add_argument("--canary-route-pct", type=float,
                    default=float(os.environ.get("WATCHDOG_CANARY_ROUTE_PCT", "100")),
                    help="U72:canary 灰度路由放量百分比(0-100);<100 时只对哈希命中的 issue 续派,其余只告警")
    a = ap.parse_args()
    # PL-132:只有真实 --post 运行才回写 latest_valid_evidence_* metadata(只读巡查/dry-run 不写)。
    global WRITE_METADATA
    WRITE_METADATA = bool(a.post)
    lock_handle = acquire_script_lock()
    if lock_handle is False:
        return

    # ===== U72:总开关(disable)—— 误报/误路由快速止损。停用时只读巡查,不做任何续派 =====
    disabled, dis_reason = O.watchdog_disabled(
        os.environ, os.environ.get("WATCHDOG_DISABLE_FLAG",
                                    "/home/fleet/line-config/watchdog.disabled"))
    global _DISABLED_STATE
    if disabled:
        # PL-137:停用=真只读巡查。统一所有入口(cron/systemd/手动)的"停用"语义:
        # 不贴台(--post 失效)、不 route/assign/rerun、不回写收口证据;仅刷新心跳/状态板,
        # 让监控看见"停用但存活"。这样无论从哪条路径触发,停用都不会刷屏 PL-94。
        print("WATCHDOG_DISABLED 本轮停用(只读:不贴台/不续派/不写收口证据,仅刷新心跳与状态板):%s" % dis_reason)
        a.route = False
        a.auto_rerun = False
        a.post = False
        WRITE_METADATA = False
        _DISABLED_STATE = [True, dis_reason]
    elif dis_reason:
        # 停用标志已到期 -> 本轮自动恢复,正常巡查/续派(不吞掉恢复条件)。
        print("WATCHDOG_DISABLE_EXPIRED 停用标志到期,本轮自动恢复:%s" % dis_reason)

    import time as _time
    cycle_t0 = _time.monotonic()
    deadline = O.CycleDeadline(cycle_t0, a.cycle_deadline_s)  # U44

    items = fetch_issues()
    scan_ok = not _ISSUE_FETCH_WARNING  # U56:本轮 issue 抓取成功才推进 last_successful_scan
    # U03:子任务引用的父 issue 若被分页/范围截断,二次补全,避免父子判定(U23/空转)漏判。
    if scan_ok and items:
        extra_parents = supplement_missing_parents(items)
        if extra_parents:
            items = items + extra_parents
            print("U03_PARENT_SUPPLEMENT 二次补全缺失父任务 %d 个" % len(extra_parents))
    st = load_state()

    # ===== U45/X62 分片续扫:按稳定排序的活跃 id + cursor 取本轮批次 =====
    active_ids_sorted = sorted(i.get("id") for i in active_issues(items) if i.get("id"))
    batch_ids, next_cursor, wrapped = O.slice_by_cursor(
        active_ids_sorted, st.get("scan_cursor", 0), a.max_active_per_cycle)
    subset = set(batch_ids) if a.max_active_per_cycle and a.max_active_per_cycle > 0 else None
    st["scan_cursor"] = next_cursor
    if subset is not None and not wrapped:
        print("CYCLE_PARTIAL 本轮处理活跃 %d/%d,下轮 cursor=%d 续扫" % (
            len(batch_ids), len(active_ids_sorted), next_cursor))

    # ===== X08:本轮各 issue assignee 快照(route 执行前回拉比对,防 TOCTOU 误派)=====
    global _SCAN_ASSIGNEE
    _SCAN_ASSIGNEE = {i.get("id"): i.get("assignee_id") for i in items if i.get("id")}
    # ===== U42/U43 并发预抓取本批活跃 issue 的 runs =====
    # X54:并发数同时受环境注入(WATCHDOG_RATE_LIMITED)与本轮 CLI 输出自动探测到的
    # rate-limit 信号约束 —— 真被限流时自动降并发,不再只靠人工注入。
    rate_limited = P.rate_limited_signal(os.environ, *_RATE_LIMIT_HINTS)
    if _RATE_LIMIT_HINTS:
        print("RATE_LIMIT_DETECTED 本轮探测到限流信号 %d 条,降并发抓取" % len(_RATE_LIMIT_HINTS))
    fw = O.adaptive_workers(a.fetch_workers, rate_limited)
    prefetch_runs(batch_ids if subset is not None else active_ids_sorted, fw)

    alerts = system_alerts(items) + clock_skew_alerts(items) + detect(
        items, a.zombie_min, a.stale_min, a.recent_min, active_subset_ids=subset,
        status_hist=st.get("status_hist"))  # X35:上一轮状态 → 判 backlog→todo 认领宽限
    # X35:记录本轮各 issue 状态,供下一轮判断 backlog→todo 提升(认领宽限起点)。
    st["status_hist"] = {i.get("id"): i.get("status") for i in items if i.get("id")}
    # X26:对比上一轮 leader 映射,leader 轮换/小队增删 → 写审计日志(可重放/可追责)。
    cur_leader_map = {sid: (brain[0] if brain else None)
                      for sid, brain in load_line_brains().items()}
    leader_changes = O.diff_leader_map(st.get("leader_map"), cur_leader_map)
    if leader_changes:
        n = O.append_leader_audit(leader_changes, LEADER_AUDIT_F, now=now_utc())
        print("LEADER_AUDIT 记录 leader 映射变更 %d 条 -> %s" % (n, LEADER_AUDIT_F))
    st["leader_map"] = cur_leader_map
    if a.probe:
        alerts += probe_alerts(E._load_env(), want_device=a.probe_device)
    if a.sys_health:
        # U15/U59/U60/U61/U68/U69/U70/U71:主机级系统健康探针(贴 PL-94,不路由小队)
        alerts += system_health_alerts(E._load_env(), items)
    # U22:把本批活跃 issue 的当前 role-stage 落进 state 字段(typed 优先,可追溯/可比对)
    prev_stage_state = dict(st.get("stage_state", {}))
    st["stage_state"] = compute_stage_states(items, subset_ids=subset)
    # X38:对比上一轮与本轮阶段,逐轮把发生的阶段转换(from/to/event/source)持久化成
    # JSONL,供审计重放"任务在哪一轮从哪个阶段走到哪个阶段、由什么事件驱动"。
    def _stage_view(ss):
        return {iid: {"stage": v.get("role") or v.get("event"), "ident": v.get("ident"),
                      "event": v.get("event"), "source": "watchdog"}
                for iid, v in (ss or {}).items()}
    try:
        transitions = P.diff_stage_state(_stage_view(prev_stage_state),
                                         _stage_view(st["stage_state"]), now=now_utc())
        if transitions:
            tp = P.append_jsonl(transitions, STAGE_TRANS_F)
            print("STAGE_TRANSITIONS 本轮阶段转换 %d 条 -> %s" % (len(transitions), tp or "落盘失败"))
    except Exception as e:
        print("STAGE_TRANSITIONS_SKIP %s" % str(e)[:120])
    # ===== X07/X10:扫描不完整时只告警、阻断全部 route/assign/rerun =====
    # X10 deadline 命中 + X07 issue 列表截断(覆盖未取全)都意味着本轮扫描是半截的,
    # 据此执行 route/assign/rerun 会基于不完整视图误派/误重启。命中任一即转 partial-scan:
    # 告警照常贴(让人看见漏洞),但本轮不做任何续派动作,状态板标 partial-scan。
    deadline_hit = bool(a.cycle_deadline_s and deadline.expired(_time.monotonic()))
    scan_truncated = bool(_ISSUE_TRUNCATED)
    if deadline_hit:
        print("CYCLE_DEADLINE_HIT 单轮超 %.0fs 预算,收口/续派可能未跑全" % a.cycle_deadline_s)
    scan_blocked = deadline_hit or scan_truncated
    scan_state = "partial-scan" if scan_blocked else "full"
    if scan_blocked:
        reasons = []
        if scan_truncated:
            reasons.append("coverage_truncated")
        if deadline_hit:
            reasons.append("cycle_deadline")
        print("PARTIAL_SCAN 本轮扫描不完整(%s):只告警,阻断全部 route/assign/rerun" % ",".join(reasons))

    closed_loop = list(st.get("closed_loop", []))  # U55:闭环链路历史

    # ===== 线空转/待推进检测(沿用原逻辑)=====
    idle_results = compute_idle(items)
    idle_state = st["idle_reminded"]
    to_remind = []
    for sid, info in idle_results.items():
        if info["idle"]:
            if idle_state.get(sid) == info["sig"]:
                print("IDLE_SKIP %s(持续空转,已提醒过)" % info["name"])
            else:
                to_remind.append((sid, info))
        elif sid in idle_state:
            del idle_state[sid]
            print("IDLE_RESET %s(已脱离空转/无待推进)" % info["name"])
    if to_remind:
        ilines = ["🔔 **看门狗·空转待推进提醒**:以下线已无活跃任务但仍有待推进的活,请对应线小队判定并推进下一项(只派小队,不@个人)"]
        for sid, info in to_remind:
            ilines.append(
                "- %s:本线已无活跃任务,但有 %d 个 backlog / %d 个待收尾父任务,"
                "请判定并推进下一项。禁止用个人 agent 链接点名派工。" % (
                    squad_label(sid), info["backlog"], info["parents"]))
        ibody = "\n".join(ilines)
        print(ibody)
        if a.post:
            ok, err = comment(HANDOFF, ibody)
            print("IDLE_POSTED" if ok else "IDLE_POST_FAIL: " + err)
            if not ok:
                fb = E.append_fallback(ibody, "PL-94 idle comment failed: %s" % err)
                print("HANDOFF_FALLBACK -> %s" % (fb or "兜底写入也失败"))
            if ok:
                for sid, info in to_remind:
                    idle_state[sid] = info["sig"]
    idle_summary = "空转待推进:" + (",".join(i["name"] for i in idle_results.values() if i["idle"]) or "无")

    if not alerts:
        print("WATCHDOG OK：无僵尸/失败/取消/无效完成/门禁误触发/阶段卡死")
        print(idle_summary)
        st["alert_sigs"] = {}  # 异常清零,允许下次新异常重新贴
        emit_observability(a, st, items, alerts, idle_results, closed_loop,
                           cycle_ms=int((_time.monotonic() - cycle_t0) * 1000),
                           scan_ok=scan_ok, scanned=len(items), scan_state=scan_state)
        save_state(st)
        return

    # ===== 去重(BOM-7):按 (ident|kind) 分别签名 =====
    sigs = st["alert_sigs"]
    changed = []  # 本轮新/变化的告警(才贴台/才派单)
    new_sigs = {}
    for al in alerts:
        key = alert_state_key(al)
        sig = "%s::%s" % (al["why"], al.get("streak", ""))  # why 含状态(连续取消次数),状态变化->sig 变->重新提醒
        new_sigs[key] = sig
        if sigs.get(key) != sig:
            changed.append(al)
    # 保留本轮仍在的签名,丢弃已消失的(消失->下次复发可重新提醒)
    st["alert_sigs"] = new_sigs

    # 全量告警贴台(显示当前全部异常),但仅当有 changed 才贴(防刷屏)
    lines = ["⚠️ **看门狗告警**:检测到异常任务(分类)"]
    by_kind = {}
    for al in alerts:
        by_kind.setdefault(al["kind"], []).append(al)
    for kind, group in by_kind.items():
        label = KIND_META.get(kind, (None, kind))[1]
        sla = O.sla_for(kind)  # U62:severity/owner/SLA
        for al in group:
            lines.append("- [%s] %s:%s(agent %s)⟨%s|owner:%s|SLA:%.0fm⟩" % (
                label, al["ident"], al["why"], al["agent"],
                sla["severity"], sla["owner"], sla["sla_min"]))
            # X07/X10:auto-rerun 与 route/assign 一样纳入 scan_blocked 统一门控。
            # partial-scan(覆盖截断 / deadline 命中)下不基于半截扫描盲目重启——
            # 既不执行 rerun,也不输出"已自动重启(rerun)"文案;完整扫描下仍照常重启。
            # DRY_RUN 与 route 一样只打印意图、不真正调用 multica issue rerun(避免误改平台)。
            if a.auto_rerun and not scan_blocked and kind in ("zombie", "failed"):
                if os.environ.get("WATCHDOG_DRY_RUN"):
                    lines.append("  → [DRY] would rerun(auto)")
                else:
                    sh("multica", "issue", "rerun", al["iid"])
                    lines.append("  → 已自动重启(rerun)")
    body = "\n".join(lines)
    print(body)
    if a.post:
        if not changed:
            print("POST_SKIP(无新增/变化告警)")
        else:
            # U48:超长告警按字符数分块,避免单条 comment 过长整条失败
            bodies = O.chunk_lines(lines[1:], a.alert_chunk_chars, header=lines[0])
            all_ok, last_err = True, ""
            for cb in bodies:
                ok, err = comment(HANDOFF, cb)
                all_ok = all_ok and ok
                if not ok:
                    last_err = err
            if all_ok:
                print("POSTED" + (" (%d 块)" % len(bodies) if len(bodies) > 1 else ""))
            else:
                print("POST_FAIL: " + last_err)
                # U37:PL-94 贴台失败 -> 落本地兜底通道,保证告警不丢
                fb = E.append_fallback(body, "PL-94 comment failed: %s" % last_err)
                print("HANDOFF_FALLBACK -> %s" % (fb or "兜底写入也失败"))
                for al in changed:
                    st["alert_sigs"].pop(alert_state_key(al), None)

    # ===== 分流(仅对 changed,去重防刷;多 issue 并发,单 issue 合并)=====
    # X07/X10:扫描不完整(覆盖截断 / deadline 命中)时,本轮禁止任何 route/assign/rerun,
    # 只保留上面的告警贴台。基于半截扫描续派会误派/误重启。
    route_results = None  # U51:本轮真实 route 结果,落 cycle ledger(routed_ok/fail)
    if a.route and scan_blocked:
        print("ROUTE_SKIP partial-scan 本轮不执行任何 route/assign/rerun(只告警)")
    # X53:route(改派/rerun)与 line_dispatch 续派(backlog→todo)共用同一协调锁,
    # 互斥防止两个进程同一时刻操作同一 issue(一个在打回、一个在续派)。
    # DRY_RUN 离线下用 devnull 退化(不触真锁文件,回归测试不受影响)。
    route_coord_fd = None
    if a.route and not scan_blocked:
        route_coord_fd = (open(os.devnull, "w") if os.environ.get("WATCHDOG_DRY_RUN")
                          else P.acquire_coord_lock(COORD_LOCK_F))
        if route_coord_fd is None:
            print("COORD_LOCK_BUSY 协调锁被 line_dispatch 占用,本轮跳过 route(只告警,下轮再派)")
    if a.route and not scan_blocked and route_coord_fd is not None:
        # X34:父任务假收口 → 自动打回 in_progress(动作 = 改状态,不走 assign/rerun)。
        # 在常规分流前单独执行,并把这些 alert 从分流候选里剔除(避免对父任务误 rerun)。
        reopen_alerts = [al for al in changed if al.get("kind") == "parent_reopen"]
        for al in reopen_alerts:
            ok, err = reopen_parent(al["iid"], al.get("reopen_to") or "in_progress",
                                    timeout=a.route_timeout)
            if ok:
                print("PARENT_REOPEN %s -> in_progress(子任务FAIL/未收口,父假收口已打回)" % al["ident"])
            else:
                print("PARENT_REOPEN_FAIL %s:%s" % (al["ident"], err))
        seen = st["routed_run_ids"]
        route_candidates = []
        canary_held = 0
        for al in changed:
            if al.get("kind") == "parent_reopen":
                continue  # X34:已通过 reopen_parent 处理,不再走 assign/rerun 分流
            route = alert_route(al)
            if route == "crisis":
                rid = al["run_id"]
                if rid and rid in seen:
                    continue
            # U72:canary 灰度 —— 放量 <100% 时,哈希未命中的 issue 本轮只告警不续派
            if route in ("crisis", "brain") and not O.canary_allows(al.get("iid"), a.canary_route_pct):
                canary_held += 1
                continue
            route_candidates.append(al)
        if canary_held:
            print("CANARY_HOLD 灰度放量 %.0f%%:本轮 %d 条告警只贴台不续派(等放量)" % (
                a.canary_route_pct, canary_held))
        jobs = build_route_jobs(route_candidates)
        results = run_route_jobs(jobs, a.route_workers, a.route_timeout)
        route_results = results  # U51:供 cycle ledger 记 routed_ok/routed_fail
        retry_keys = set()
        for res in results:
            job = res["job"]
            kinds = ",".join(sorted(set(al["kind"] for al in job["alerts"])))
            # U55/X43/X47:闭环链路 alert->route->rerun->verdict 落账(供追责/复盘)
            for al in job["alerts"]:
                closed_loop.append(O.closed_loop_entry(now_utc(), al, res))
            if res["ok"]:
                print("ROUTE_JOB %s -> %s alerts=%d kinds=%s" % (
                    job["ident"], squad_label(job["squad"]), len(job["alerts"]), kinds))
                # U36:续派后二次确认结果(仅在 WATCHDOG_CONFIRM_RERUN 开启时有值)
                if res.get("confirmed") is True:
                    print("ROUTE_CONFIRMED %s %s" % (job["ident"], res.get("confirm_info", "")))
                elif res.get("confirmed") is False:
                    print("ROUTE_UNCONFIRMED %s %s" % (job["ident"], res.get("confirm_info", "")))
                    # 未确认 -> 解除该 issue 去重,下一轮重新评估/重派
                    for al in job["alerts"]:
                        st["alert_sigs"].pop(alert_state_key(al), None)
                for al in job["alerts"]:
                    if alert_route(al) == "crisis" and al.get("run_id"):
                        seen.add(al["run_id"])
            else:
                print("ROUTE_JOB_FAIL %s -> %s alerts=%d kinds=%s comment=%s route=%s" % (
                    job["ident"], squad_label(job["squad"]), len(job["alerts"]), kinds, res["err"], res["route_err"]))
                for al in job["alerts"]:
                    retry_keys.add(alert_state_key(al))
        if retry_keys:
            route_retry = st.get("route_retry", {})
            for key in retry_keys:
                st["alert_sigs"].pop(key, None)
                # X56:重试带失败次数 + 下次重试时间(指数退避)
                meta = O.route_retry_meta(route_retry.get(key), now_utc())
                route_retry[key] = meta
                print("ROUTE_RETRY_ARMED %s failcount=%d next=%s" % (
                    key, meta["failcount"], meta["next_retry_ts"]))
            st["route_retry"] = route_retry
            print("ROUTE_RETRY_ARMED keys=%d" % len(retry_keys))
        else:
            # 成功路由的 key 清掉历史退避计数
            for res in results:
                if res["ok"]:
                    for al in res["job"]["alerts"]:
                        st.get("route_retry", {}).pop(alert_state_key(al), None)

    # X53:释放协调锁(None / devnull 均安全),让 line_dispatch 本轮之后可接力。
    P.release_coord_lock(route_coord_fd)
    print(idle_summary)
    emit_observability(a, st, items, alerts, idle_results, closed_loop,
                       cycle_ms=int((_time.monotonic() - cycle_t0) * 1000),
                       scan_ok=scan_ok, scanned=len(items), scan_state=scan_state,
                       route_results=route_results)
    save_state(st)

if __name__ == "__main__":
    main()
