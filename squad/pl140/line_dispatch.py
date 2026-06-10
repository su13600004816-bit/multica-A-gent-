#!/usr/bin/env python3
# 自动续派:小队空闲(无活跃任务)+ 该队有 backlog 排队任务 → 自动把最早的 backlog 提升为 todo(触发小队干下一个)。
# 让生产线"自流动":做完一个,自动接下一个,不用 cc 手动盯着派。
# 用法:line_dispatch.py [--post]
import os
import re
import subprocess, json, sys, argparse
import fcntl
import line_partial as P  # X53:与 line_watchdog 共用的跨脚本协调锁(acquire/release_coord_lock)

# PL-94 看门狗/调度告警台。旧值 199691f5(已失效,issue get 返回空)是 BOM-7 修复点。
HANDOFF = "bc056ade-f639-41af-b5df-9c7fb6a27628"
DEFAULT_SQUADS = {
    "线小队-T01": "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96",
    "线小队-T02": "9db04481-be45-48a1-8114-5f2b85506f78",
    "线小队-T03": "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52",
}
DEFAULT_LINE_BRAIN = {
    "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96": ("514f9573-4cf9-4b99-b13f-238967893d63", "线主脑-T01"),
    "9db04481-be45-48a1-8114-5f2b85506f78": ("d1713390-6dd4-4caf-ad6a-7a55a1bfaaa9", "线主脑-T02"),
    "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52": ("b70d9b47-4243-4c91-9393-8034fe413ede", "线主脑-T03"),
}
ACTIVE = ("todo", "in_progress", "in_review")  # 占着小队、不能再派
# U38:backlog 续派排序优先级。先按优先级,再按 number,保证高优先级任务先被接上,不再纯 FIFO。
PRIORITY_RANK = {"urgent": 0, "critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3, "none": 4, "": 4}
LINE_SQUAD_RE = re.compile(r"^线小队-T\d+$")
ISSUE_LIMIT = int(os.environ.get("LINE_DISPATCH_ISSUE_LIMIT", "5000"))
SCRIPT_LOCK_F = os.environ.get("LINE_DISPATCH_SCRIPT_LOCK", "/home/fleet/line-config/line-dispatch.lock")
# X53:与 line_watchdog route 共用的跨脚本协调锁(同一文件)。续派(backlog→todo)与
# watchdog 改派/rerun 互斥,防止两进程同一时刻操作同一 issue。
COORD_LOCK_F = os.environ.get("WATCHDOG_COORD_LOCK", P.COORD_LOCK_DEFAULT)
_SQUAD_CACHE = None
_BRAIN_CACHE = None
_SQUAD_DISCOVERY_WARNING = None
_ISSUE_FETCH_WARNING = None

def squad_sort_key(item):
    name = item[0] if isinstance(item, tuple) else str(item)
    m = re.search(r"T(\d+)$", name)
    return (0, int(m.group(1))) if m else (1, name)

def acquire_script_lock():
    if os.environ.get("LINE_DISPATCH_DRY_RUN") or os.environ.get("LINE_DISPATCH_DISABLE_LOCK"):
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
        print("DISPATCH_SKIP: previous run still active")
        return False
    except Exception as e:
        print("DISPATCH_LOCK_WARN: %s" % str(e)[:160])
        return None

def sh(*a, timeout=60):
    if os.environ.get("LINE_DISPATCH_DRY_RUN") and len(a) >= 4 and a[:3] == ("multica", "issue", "status"):
        class R:
            stdout = "[DRY] would status %s %s" % (a[3], a[4] if len(a) > 4 else "")
            stderr = ""
            returncode = 0
        print(R.stdout)
        return R()
    try: return subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        class R: stdout=""; stderr=str(e); returncode=1
        return R()

def jget(*a):
    r = sh(*a)
    try: return json.loads(r.stdout)
    except Exception: return None

def _items_from_obj(d, key):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get(key, d.get("items", []))
    return []

def fetch_squad_items():
    global _SQUAD_DISCOVERY_WARNING
    _SQUAD_DISCOVERY_WARNING = None
    if os.environ.get("LINE_DISPATCH_SQUAD_LIST_FAIL"):
        _SQUAD_DISCOVERY_WARNING = "forced squad list failure"
        return []
    fx = os.environ.get("LINE_DISPATCH_SQUADS_FIXTURE")
    if fx:
        try:
            return _items_from_obj(json.load(open(fx)), "squads")
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
    return _items_from_obj(d, "squads")

def load_line_squads():
    global _SQUAD_CACHE, _BRAIN_CACHE
    if _SQUAD_CACHE is not None:
        return _SQUAD_CACHE
    squads = {}
    brains = {}
    for s in fetch_squad_items():
        name = s.get("name") or ""
        sid = s.get("id") or ""
        if sid and LINE_SQUAD_RE.match(name) and not s.get("archived_at"):
            squads[name] = sid
            leader = s.get("leader_id")
            if leader:
                brains[sid] = (leader, name.replace("线小队", "线主脑"))
    if not squads:
        squads = dict(DEFAULT_SQUADS)
        brains = dict(DEFAULT_LINE_BRAIN)
    _SQUAD_CACHE = dict(sorted(squads.items(), key=squad_sort_key))
    merged = dict(DEFAULT_LINE_BRAIN)
    merged.update(brains)
    _BRAIN_CACHE = merged
    return _SQUAD_CACHE

def load_line_brains():
    if _BRAIN_CACHE is None:
        load_line_squads()
    return _BRAIN_CACHE or {}

def squad_assignee_ids(sid):
    ids = {sid}
    brain = load_line_brains().get(sid)
    if brain and brain[0]:
        ids.add(brain[0])
    return ids

def squad_discovery_warning():
    load_line_squads()
    return _SQUAD_DISCOVERY_WARNING

def fetch_issues():
    global _ISSUE_FETCH_WARNING
    _ISSUE_FETCH_WARNING = None
    if os.environ.get("LINE_DISPATCH_ISSUE_LIST_FAIL"):
        _ISSUE_FETCH_WARNING = "forced issue list failure"
        return []
    fx = os.environ.get("LINE_DISPATCH_ISSUES_FIXTURE")
    if fx:
        try:
            d = json.load(open(fx))
        except Exception as e:
            _ISSUE_FETCH_WARNING = "issue fixture read failed:%s" % str(e)[:120]
            d = []
    else:
        r = sh("multica", "issue", "list", "--limit", str(ISSUE_LIMIT), "--output", "json")
        if r.returncode != 0:
            _ISSUE_FETCH_WARNING = (r.stderr or r.stdout or "issue list failed")[:200]
            return []
        try:
            d = json.loads(r.stdout)
        except Exception as e:
            _ISSUE_FETCH_WARNING = "issue list json parse failed:%s" % str(e)[:120]
            return []
    return d if isinstance(d, list) else (d.get("issues", d.get("items", [])) if d else [])

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--post", action="store_true")
    a = ap.parse_args()
    lock_handle = acquire_script_lock()
    if lock_handle is False:
        return

    items = fetch_issues()
    warnings = []
    if _ISSUE_FETCH_WARNING:
        warnings.append("DISPATCH_FETCH_FAIL issue list:%s" % _ISSUE_FETCH_WARNING)
    sw = squad_discovery_warning()
    if sw:
        warnings.append("DISPATCH_SQUAD_DISCOVERY_FAIL:%s" % sw)
    if len(items) >= ISSUE_LIMIT:
        warnings.append("DISPATCH_COVERAGE_WARN issue list reached LINE_DISPATCH_ISSUE_LIMIT=%d" % ISSUE_LIMIT)
    for w in warnings:
        print(w)
    dispatched = []
    # X53:进入实际续派(改 issue 状态)前获取与 watchdog 共用的协调锁;被 watchdog route
    # 占用则本轮不续派(只读,下轮再接力),避免和 watchdog 同时改同一 issue。
    # DRY_RUN 离线下退化为 devnull(不触真锁文件,回归测试不受影响)。锁仅护续派写循环。
    coord_fd = (open(os.devnull, "w")
                if os.environ.get("LINE_DISPATCH_DRY_RUN") or os.environ.get("LINE_DISPATCH_DISABLE_LOCK")
                else P.acquire_coord_lock(COORD_LOCK_F))
    if coord_fd is None:
        print("DISPATCH_SKIP: 协调锁被 watchdog route 占用,本轮不续派(下轮再接力)")
        return
    for name, sid in load_line_squads().items():
        owners = squad_assignee_ids(sid)
        mine = [i for i in items if i.get("assignee_id") in owners]
        # 该队已有活跃任务 → 不派(在干活)
        if any(i.get("status") in ACTIVE for i in mine):
            continue
        # 找该队优先级最高、其次最早的 backlog(排队任务)(U38:优先级/SLA 排序,不再纯 number FIFO)
        backlog = sorted(
            [i for i in mine if i.get("status") == "backlog"],
            key=lambda x: (PRIORITY_RANK.get((x.get("priority") or "").lower(), 4), x.get("number", 0)))
        if not backlog:
            continue
        nxt = backlog[0]
        r = sh("multica", "issue", "status", nxt["id"], "todo")   # 提升触发
        # U18:检查 returncode,status 失败时不计入已续派,并把失败作为告警上报,避免假装"已接上"。
        if getattr(r, "returncode", 1) != 0:
            warnings.append("DISPATCH_STATUS_FAIL %s %s:%s" % (
                name, nxt.get("identifier"),
                (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "status failed")[:160]))
            continue
        dispatched.append((name, nxt.get("identifier"), nxt.get("title", "")[:40]))

    # X53:续派写循环结束,释放协调锁(后续只贴 PL-94 汇总,不再改 issue 状态)。
    P.release_coord_lock(coord_fd)

    if not dispatched:
        if warnings:
            print("DISPATCH：未续派;存在读取/覆盖告警,不能证明队列为空")
        else:
            print("DISPATCH：无空闲小队需续派(或队列空)")
        if a.post and warnings and not os.environ.get("LINE_DISPATCH_DRY_RUN"):
            body = "⚠️ **自动续派覆盖告警**\n" + "\n".join("- " + w for w in warnings)
            p = subprocess.run(["multica", "issue", "comment", "add", HANDOFF, "--content-stdin"],
                               input=body, text=True, capture_output=True, timeout=30)
            print("POSTED" if p.returncode == 0 else "POST_FAIL")
        return
    lines = ["🔁 **自动续派**：空闲小队接上了排队任务"]
    for name, ident, title in dispatched:
        lines.append("- %s ← %s %s" % (name, ident, title))
    # U34:续派成功时也把覆盖/读取/status 失败告警一并带进 PL-94 汇总,不让告警只留在日志里。
    if warnings:
        lines.append("")
        lines.append("⚠️ 覆盖/读取告警(随本轮续派一并上报):")
        lines.extend("- " + w for w in warnings)
    body = "\n".join(lines)
    print(body)
    if a.post:
        if os.environ.get("LINE_DISPATCH_DRY_RUN"):
            print("[DRY] would post dispatch summary")
            print("POSTED")
        else:
            p = subprocess.run(["multica", "issue", "comment", "add", HANDOFF, "--content-stdin"],
                               input=body, text=True, capture_output=True, timeout=30)
            print("POSTED" if p.returncode == 0 else "POST_FAIL")

if __name__ == "__main__":
    main()
