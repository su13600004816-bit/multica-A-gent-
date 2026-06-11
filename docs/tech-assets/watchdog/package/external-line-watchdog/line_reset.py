#!/usr/bin/env python3
# 显式记忆清除(主动 reset):写/审 每次任务跑完调用。
# 顺序:① 先归档保住知识(分梯度记忆,记忆总管) ② 再清工作记忆/工作区残留 ③ 记 reset 日志。
# 用法:
#   line_reset.py <task_id> [--role 写|审] [--worktree <git工作区>] [--no-archive]
import os, sys, argparse, subprocess, time, json

LOG = "/home/fleet/line-config/reset.log"
BRIDGE = "/home/fleet/line-config/line_bridge.py"
DONE_GATE = "/home/fleet/line-config/line_done_gate.py"

def log(m):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), m)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)

def reclaim_worktree(worktree):
    """DONE 后回收本 worktree 的磁盘:① 停掉 cwd 在该 worktree 内的服务器/构建进程,
    ② 删除 node_modules/.next/.turbo(gitignore 的产物,git clean -fd 清不掉)。
    只作用于这一个 worktree,不碰永久预览站(canvas-preview 等不在 worktree 下)。"""
    wt = os.path.realpath(worktree)
    # ① 停掉工作目录落在本 worktree 内的进程(残留 next start / esbuild 等)
    killed = []
    proc = "/proc"
    if os.path.isdir(proc):
        for pid in os.listdir(proc):
            if not pid.isdigit():
                continue
            try:
                cwd = os.path.realpath(os.path.join(proc, pid, "cwd"))
            except OSError:
                continue
            if cwd == wt or cwd.startswith(wt + os.sep):
                try:
                    os.kill(int(pid), 15)
                    killed.append(pid)
                except OSError:
                    pass
    if killed:
        time.sleep(2)
        for pid in killed:
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass
        log("  停掉本 worktree 残留进程(QA服务器/构建): pid=%s" % ",".join(killed))
    # ② 回收构建产物
    freed = 0
    for root, dirs, _files in os.walk(wt):
        for d in list(dirs):
            if d in ("node_modules", ".next", ".turbo"):
                p = os.path.join(root, d)
                try:
                    freed += _dir_size(p)
                except OSError:
                    pass
                subprocess.run(["rm", "-rf", p])
                dirs.remove(d)  # 不再递归进已删目录
    log("  构建产物已回收(node_modules/.next/.turbo): 约 %.1f GiB" % (freed / 1024**3))

def _dir_size(p):
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--role", default="?")
    ap.add_argument("--worktree", default=None)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="跳过 DONE_REAL 门禁(仅危机/人工处置用,会记日志)")
    ap.add_argument("--skip-done-gate", action="store_true", help="同 --force 别名")
    a = ap.parse_args()

    log("RESET start task=%s role=%s" % (a.task_id, a.role))

    # done_real 守卫(总管 2026-06-10):若该 issue 已被总管据真实生产证据收口
    # (metadata.pipeline_status=done_real),则不跑门禁/不 reset/不改状态,直接 no_action 退出。
    # 杜绝"门禁(Qwen)判 gate_pass=FAIL -> 线主脑把已收口任务从 done 拉回 blocked"的回滚自燃。
    if not (a.force or a.skip_done_gate):
        try:
            _r = subprocess.run(["multica", "issue", "get", a.task_id, "--output", "json"],
                                capture_output=True, text=True, timeout=60)
            if _r.returncode == 0:
                _md = (json.loads(_r.stdout or "{}") or {}).get("metadata") or {}
                if isinstance(_md, str):
                    _md = json.loads(_md or "{}")
                if str((_md or {}).get("pipeline_status", "")).lower() == "done_real":
                    log("RESET no_action task=%s 已 done_real(总管据真实证据收口);跳过门禁/reset,不改状态" % a.task_id)
                    sys.exit(0)
        except Exception as _e:
            log("  done_real 预检失败(忽略,继续正常流程): %s" % _e)

    # ⛔ BOM-6 收口铁律:证据不全(DONE_REAL 门禁 BLOCK)时禁止 reset,防止 PL-89 式早收口。
    if not (a.force or a.skip_done_gate):
        g = subprocess.run(["python3", DONE_GATE, a.task_id], capture_output=True, text=True, timeout=120)
        sys.stdout.write(g.stdout)
        if g.returncode == 2:
            log("RESET ABORTED task=%s DONE_REAL 门禁 BLOCK,证据不全,拒绝收口(需补齐或 --force)" % a.task_id)
            sys.exit(2)
        elif g.returncode != 0:
            log("  DONE_REAL 门禁无法判定(rc=%s),按谨慎放行但记录;建议人工核 evidence" % g.returncode)
        else:
            log("  DONE_REAL 门禁 ALLOW,证据齐全,继续收口")

    # ① 先归档(保住 durable 知识,清之前先存)
    if not a.no_archive:
        r = subprocess.run(["python3", BRIDGE, "archive", a.task_id], capture_output=True, text=True, timeout=300)
        ok = "ARCHIVED" in (r.stdout or "")
        log("  归档分梯度记忆: %s" % ("OK" if ok else "FAIL/" + (r.stdout or r.stderr)[:120]))

    # ② 清工作区残留(只在已提交/归档后):git reset --hard + clean,回到干净基线
    if a.worktree and os.path.isdir(a.worktree):
        subprocess.run(["git", "-C", a.worktree, "reset", "--hard"], capture_output=True, text=True)
        subprocess.run(["git", "-C", a.worktree, "clean", "-fd"], capture_output=True, text=True)
        log("  工作区已清空回干净基线: %s" % a.worktree)
        # ②b 磁盘铁律:DONE 后必须回收构建产物 + 停掉本线 QA 服务器。
        #     注意 `git clean -fd` 不带 -x,不会清 gitignore 的 node_modules/.next,
        #     必须显式回收,否则每条线的产物只增不减、最终撑爆磁盘(2026-06-08 事故根因)。
        reclaim_worktree(a.worktree)
    else:
        log("  (未指定/无 worktree,跳过工作区清理)")

    # ③ 清深挖累积记忆(深挖人#1..#10 递增下探的累积,只在任务 DONE 后清,不跨轮清)
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(a.task_id))
    dig = "/home/fleet/line-config/memory/.digs/%s.md" % safe
    if os.path.exists(dig):
        os.remove(dig)
        log("  深挖累积记忆已清(任务完成才清,递增下探到此结束): %s" % dig)
    else:
        log("  (本任务无深挖累积记忆,跳过)")

    # ④ 工作记忆标记:Multica 每任务本就是新会话(上下文不跨任务带);此处显式标记下一任务从干净上下文开始
    log("RESET done task=%s 工作记忆+深挖累积记忆已清除,下个任务从干净上下文+按需读分梯度记忆开始" % a.task_id)

if __name__ == "__main__":
    main()
