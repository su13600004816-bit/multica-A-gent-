#!/usr/bin/env python3
"""PL1 线主管 (Line Foreman) periodic driver — the smart+periodic middle layer.

This script is intentionally DUMB and THIN. It does NOT make management
decisions. It only:
  1. coarsely detects whether any LIVE line issue looks stalled
     (updated_at older than STALE_MIN), and
  2. if so, and the foreman agent is not already busy, wakes the foreman
     LLM agent by posting a briefing comment to its control board issue.

All actual management judgment (what is stuck, why, what to do, when to
escalate) lives in the foreman agent's instructions + the LLM — not here.

Driven by cron on a slower cadence than the watchdog (e.g. every 10 min).
Respects the shared watchdog.disabled kill switch and its own foreman.disabled.
--dry-run prints the briefing and the decision but posts nothing.

DETECTION (foreman-fix 2026-06-11): enumerate stale live issues straight from
the DB via `sudo docker exec psql`. The previous `multica issue list
--assignee-id <squad>` path was DOUBLE-broken: (a) the CLI list is scoped and
returned only terminal issues, and (b) it only checked squad-id-assigned issues
while real live line work is usually assigned to the line's leader/worker AGENTS
— so it reported "0 stale" for 7h while the board piled up. cron runs as fleet
which holds NOPASSWD sudo, so the DB read is reliable and scope-free.
"""
import os
import re
import sys
import json
import subprocess
import datetime

AGENT_ID = os.environ.get("FOREMAN_AGENT_ID", "25347412-6839-49c2-9b67-b17e34ccde49")
ISSUE_ID = os.environ.get("FOREMAN_ISSUE_ID", "f0b81a27-2ad7-41fd-97c9-9a74bbe4f047")
STALE_MIN = int(os.environ.get("FOREMAN_STALE_MIN", "25"))
LINE_RE = re.compile(os.environ.get("FOREMAN_LINE_RE", r"^(线小队|审计小队)-"))
# 一个 issue 算"线相关"的判据:它的归属标签(squad 名 或 指派 agent 名)带线标记。
# 这样既盖 squad 指派、也盖 线主脑/队员 agent 指派,排掉未指派/非线杂项。
LINE_LABEL_RE = re.compile(r"线小队|审计小队|主脑|主线|C\d|T\d")
LOG = os.environ.get("FOREMAN_LOG", "/home/fleet/line-config/foreman.log")
DISABLE_FLAGS = [
    "/home/fleet/line-config/foreman.disabled",
    "/home/fleet/line-config/watchdog.disabled",
]
TERMINAL = {"done", "cancelled"}
DRY = "--dry-run" in sys.argv
SEP = "\x1f"  # unit separator: 安全分隔(标题里不会有它)
PSQL = ["sudo", "-n", "docker", "exec", "-i", "multica-postgres-1",
        "psql", "-U", "multica", "-d", "multica", "-t", "-A", "-F", SEP]


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(*args, timeout=30):
    try:
        return subprocess.run(["multica", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def jsh(*args, timeout=30):
    p = sh(*args, timeout=timeout)
    if p is None or p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def as_list(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def db_stale_line_issues():
    """可靠枚举所有非终态、停滞 >= STALE_MIN 的"线相关"issue(直连 DB,绕开 CLI 作用域)。
    返回 list[dict] 或 None(DB 读失败)。排除 foreman 自己的控制台 issue。"""
    sql = (
        "SELECT i.number, i.status, coalesce(i.title,''), "
        "round(extract(epoch from (now()-i.updated_at))/60)::int, "
        "coalesce(sq.name, ag.name, '(unassigned)') "
        "FROM issue i "
        "LEFT JOIN squad sq ON i.assignee_type='squad' AND sq.id=i.assignee_id "
        "LEFT JOIN agent  ag ON i.assignee_type='agent' AND ag.id=i.assignee_id "
        "WHERE i.status NOT IN ('done','cancelled') "
        "AND i.updated_at < now() - (%d * interval '1 minute') "
        "AND i.id::text <> '%s' "
        "ORDER BY i.updated_at;" % (STALE_MIN, ISSUE_ID)
    )
    try:
        p = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=40)
    except Exception as e:
        log(f"db enumerate failed (exc): {str(e)[:160]}")
        return None
    if p.returncode != 0:
        log(f"db enumerate rc={p.returncode}: {(p.stderr or p.stdout or '')[:160]}")
        return None
    out = []
    for ln in p.stdout.splitlines():
        if not ln.strip():
            continue
        parts = ln.split(SEP)
        if len(parts) < 5:
            continue
        num, st, title, agemin, label = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not LINE_LABEL_RE.search(label):  # 只盯线相关(squad/线 agent),排未指派/非线
            continue
        try:
            age = int(agemin)
        except Exception:
            age = 0
        out.append({"line": label[:18], "num": num, "status": st, "age_min": age, "title": title[:38]})
    return out


def foreman_busy():
    """True if the foreman already has an in-flight task (avoid pile-up)."""
    data = jsh("agent", "tasks", AGENT_ID, "--output", "json")
    if data is None:
        return False  # can't tell — server-side on-comment dedup still protects us
    for t in as_list(data, "tasks", "items"):
        if isinstance(t, dict) and t.get("status") in ("queued", "dispatched", "running"):
            return True
    return False


def main():
    for flag in DISABLE_FLAGS:
        if os.path.exists(flag):
            log(f"DISABLED via {flag}; read-only exit")
            return 0

    stale = db_stale_line_issues()
    if stale is None:
        log("db enumerate unavailable (sudo/psql?); foreman skipped this tick (no false healthy)")
        return 0

    if not stale:
        log(f"healthy: 0 stale live line issues (>= {STALE_MIN}m); foreman not woken")
        return 0

    stale.sort(key=lambda x: -x["age_min"])
    lines = sorted({x["line"] for x in stale})
    rows = "\n".join(
        f"- [{x['line']}] PL-{x['num']} `{x['status']}` 滞留 {x['age_min']}m — {x['title']}"
        for x in stale
    )
    briefing = (
        "🧑‍✈️ **巡线时间到 (Foreman tick)**\n"
        f"检测到 {len(stale)} 个活线任务滞留 ≥ {STALE_MIN}m,横跨 {len(lines)} 条线。"
        "请按你的线主管职责巡线:逐个判断卡点→主动推进/解阻/重排,自己解不了的升级给人类总管,"
        "健康的线 no_action。**终态 issue 不碰。** 巡完在本台贴一条简报。\n\n"
        "本轮粗筛到的滞留项(你需自行复核真实卡点):\n" + rows
    )

    if DRY:
        log(f"DRY-RUN: would wake foreman — {len(stale)} stale across {len(lines)} lines")
        print("\n----- BRIEFING -----\n" + briefing + "\n--------------------")
        return 0

    if foreman_busy():
        log(f"foreman busy (in-flight task); {len(stale)} stale noted, skip this tick")
        return 0

    p = subprocess.run(
        ["multica", "issue", "comment", "add", ISSUE_ID, "--content-stdin"],
        input=briefing, text=True, capture_output=True, timeout=30,
    )
    if p.returncode == 0:
        log(f"woke foreman: {len(stale)} stale across {len(lines)} lines -> {sorted(lines)}")
    else:
        log(f"FAILED to wake foreman: {(p.stderr or p.stdout)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
