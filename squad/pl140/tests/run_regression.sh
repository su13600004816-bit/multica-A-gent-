#!/usr/bin/env bash
# PL-104 线机制回归:用 PL-89/PL-91 历史断点做样本,验证看门狗/门禁/收口的修复。
# 全程离线(fixtures + WATCHDOG_NOW),不触发任何 agent、不贴台、不调模型。
# 用法:bash tests/run_regression.sh   (在 /home/fleet/line-config 下)
set -u
cd "$(dirname "$0")/.." || exit 1
T=tests
REG_TMP=$(mktemp -d /tmp/line-regression.XXXXXX)
export REG_TMP
trap 'rm -rf "$REG_TMP"' EXIT
pass=0; fail=0
ok(){ echo "  ✅ $1"; pass=$((pass+1)); }
no(){ echo "  ❌ $1"; fail=$((fail+1)); }

export WATCHDOG_DRY_RUN=1 WATCHDOG_NOW=2026-06-09T04:28:00Z
export WATCHDOG_FIXTURE=$T/pl89_issues.json
export WATCHDOG_RUNS_FIXTURE=$T/pl89_runs.json
export WATCHDOG_COMMENTS_FIXTURE=$T/pl89_comments.json
export WATCHDOG_LEDGER_DIR=$REG_TMP/ledger  # U30:ledger 落盘到临时目录,不污染仓库
# PL-140:leader_audit 默认写 /home/fleet/line-config/leader_audit.jsonl,审计 checkout 下该路径只读,
# 会让 --route 用例误判失败。改写到临时目录,使回归在任意只读 checkout 下都可复现。
export WATCHDOG_LEADER_AUDIT_F=$REG_TMP/leader_audit.jsonl
# PL-137:离线回归默认指向不存在的停用标志,隔离生产 watchdog.disabled,
# 否则停用期跑回归会误把 --post 用例判失败(停用=真只读)。需要测停用的用例自行覆盖该变量。
export WATCHDOG_DISABLE_FLAG=$REG_TMP/no_such_disable_flag.disabled

echo "[1] BOM-2 看门狗必须告警 PL-89 cancelled run f70159f8(旧版漏报)"
out=$(WATCHDOG_STATE=$REG_TMP/rg1.json python3 line_watchdog.py 2>&1)
echo "$out" | grep -q "取消run(result为空)] PL-89" && ok "检出 PL-89 取消run" || no "未检出 PL-89 取消run"
echo "$out" | grep -q "WATCHDOG OK" && no "竟报 WATCHDOG OK(漏报)" || ok "未误报 OK"

echo "[2] BOM-2 旧版看门狗对 cancelled 无检测能力(结构证明)"
grep -Eq "cancelled" <(grep -v 'DONE_ST' line_watchdog.py.bak.pl104 | grep -i cancel) \
  && no "旧版有 cancelled 检测?" || ok "旧版无 cancelled 检测逻辑"

echo "[3] BOM-3 阶段卡死:in_progress(PL-89)/in_review(PL-91)均被检出"
echo "$out" | grep -q "阶段卡死.*PL-89" && ok "PL-89 in_progress 卡死" || no "PL-89 卡死漏检"
echo "$out" | grep -q "阶段卡死.*PL-91" && ok "PL-91 in_review 卡死" || no "PL-91 卡死漏检"

echo "[4] BOM-5 门禁误触发:占位岗回复被判 gate_misrouted"
echo "$out" | grep -q "门禁误触发.*PL-89" && ok "检出占位岗误触发" || no "占位岗误触发漏检"

echo "[5] BOM-7 去重:同状态二次扫描不重复贴台"
rm -f "$REG_TMP/rg5.json"
r1=$(WATCHDOG_STATE=$REG_TMP/rg5.json python3 line_watchdog.py --post 2>&1 | grep -c POSTED)
r2=$(WATCHDOG_STATE=$REG_TMP/rg5.json python3 line_watchdog.py --post 2>&1 | grep -c "POST_SKIP")
[ "$r1" = 1 ] && [ "$r2" = 1 ] && ok "首轮 POSTED、次轮 POST_SKIP" || no "去重异常(r1=$r1 r2=$r2)"

echo "[6] BOM-7 连续取消必须重新提醒并升级危机"
python3 - <<'PY'
import json
import os
d=json.load(open("tests/pl89_runs.json"))
d["8bcfaeb5-8b2a-45e1-983a-2e79d319f6b5"].insert(0,{"id":"second-cancel","status":"cancelled","result":None,"error":None,"agent_id":"be3d60e9","created_at":"2026-06-09T04:25:00Z"})
json.dump(d,open(os.path.join(os.environ["REG_TMP"], "rg_streak2.json"),"w"))
PY
rm -f "$REG_TMP/rg6.json"
WATCHDOG_STATE=$REG_TMP/rg6.json python3 line_watchdog.py --post --route >/dev/null 2>&1
o6=$(WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_streak2.json WATCHDOG_STATE=$REG_TMP/rg6.json python3 line_watchdog.py --post --route 2>&1)
echo "$o6" | grep -q "连续取消2次" && ok "第二次取消重新提醒(连续取消2次)" || no "未重新提醒"
echo "$o6" | grep -q "ROUTE_JOB PL-89 -> 线小队-T01" && ok "连续取消已分流给 T01 小队" || no "未分流给 T01 小队"

echo "[7] BOM-6 DONE_REAL 门禁:PL-89 早收口场景必须 BLOCK"
export DONE_GATE_ISSUE_FIXTURE=$T/pl89_done_issue.json
DONE_GATE_COMMENTS_FIXTURE=$T/pl89_done_block_comments.json python3 line_done_gate.py PL-89 >/dev/null 2>&1
[ $? -eq 2 ] && ok "早收口 BLOCK(exit 2)" || no "竟未 BLOCK"

echo "[8] BOM-6 DONE_REAL 门禁:真门禁PASS+真站证据 必须 ALLOW"
DONE_GATE_COMMENTS_FIXTURE=$T/pl89_done_pass_comments.json python3 line_done_gate.py PL-89 >/dev/null 2>&1
[ $? -eq 0 ] && ok "真闭环 ALLOW(exit 0)" || no "真闭环竟被 BLOCK"

echo "[9] BOM-6 line_reset 证据不全必须中止"
DONE_GATE_COMMENTS_FIXTURE=$T/pl89_done_block_comments.json python3 line_reset.py PL-89 --no-archive >/dev/null 2>&1
[ $? -eq 2 ] && ok "line_reset ABORTED(exit 2)" || no "line_reset 未中止"

echo "[10] BOM-4 门禁无截图/URL 证据直接 FAIL(离线确定性路径,不调模型)"
# 空证据 → gate() 在调任何模型前直接 FAIL(line_bridge.py:107-109),离线可复现。
python3 line_bridge.py gate iid --goal g --evidence "" 2>&1 | grep -q "VERDICT: FAIL" && ok "空证据判 FAIL" || no "空证据未判 FAIL"
# 页面任务无 URL/截图 → 同样在调模型前直接 FAIL(line_bridge.py:110-112)。
python3 line_bridge.py gate iid --goal "画布页面入口" --evidence "已完成开发" 2>&1 | grep -q "VERDICT: FAIL" && ok "页面任务无URL/截图判 FAIL" || no "页面无URL/截图未判 FAIL"

echo "[11] BOM-7 line_dispatch 告警台不再指向失效旧台号"
grep -Eq '^HANDOFF *= *"199691f5' line_dispatch.py && no "HANDOFF 仍指向失效 199691f5" || ok "HANDOFF 已改为 PL-94 bc056ade"
grep -Eq '^HANDOFF *= *"bc056ade' line_dispatch.py && ok "确认指向 PL-94" || no "未指向 PL-94"

echo "[12] BOM-3 deadline:正常阶段完成后下一环节超时也必须告警(状态机驱动)"
d12=$(WATCHDOG_STATE=$REG_TMP/rg12.json \
  WATCHDOG_FIXTURE=$T/stage_progress_issues.json \
  WATCHDOG_RUNS_FIXTURE=$T/stage_progress_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$T/stage_progress_comments.json \
  python3 line_watchdog.py --stale-min 5 2>&1)
echo "$d12" | grep -q "PL-91:.*未推进至『审计』" && ok "dev_done→in_review 无审计 run 告警" || no "dev_done→审计 漏检"
echo "$d12" | grep -q "PL-91G:.*未推进至『门禁』" && ok "审计 PASS 后无 gate 告警" || no "audit_pass→门禁 漏检"
echo "$d12" | grep -q "PL-89D:.*未推进至『收口』" && ok "门禁 PASS 后无 done/reset 告警" || no "gate_pass→收口 漏检"
echo "$d12" | grep -q "PL-89R:.*未推进至『返工』" && ok "门禁/视觉 FAIL 后无返工 run 告警" || no "gate_fail→返工 漏检"
echo "$d12" | grep -q "PL-89H" && no "对照组(推进正常)被误报" || ok "对照组无误报(下一环节已启动)"

echo "[13] BOM-9 分流:同时 3 个 issue 异常必须拆成 3 个小队 ROUTE_JOB,不能串死"
cat >"$REG_TMP/rg_multi_issues.json" <<'JSON'
[
  {"id":"multi-a","identifier":"PL-A","title":"T01 cancelled","status":"in_progress","assignee_id":"7dafa944-07b8-4fba-ab3d-1b7ae0ceda96","assignee_type":"squad","number":1},
  {"id":"multi-b","identifier":"PL-B","title":"T02 evidence missing","status":"in_progress","assignee_id":"9db04481-be45-48a1-8114-5f2b85506f78","assignee_type":"squad","number":2},
  {"id":"multi-c","identifier":"PL-C","title":"T03 zombie","status":"in_progress","assignee_id":"0f0013ea-a65d-4ab8-b1b4-17e14da8ab52","assignee_type":"squad","number":3}
]
JSON
cat >"$REG_TMP/rg_multi_runs.json" <<'JSON'
{
  "multi-a":[{"id":"run-a","status":"cancelled","result":null,"error":null,"agent_id":"agent-a","created_at":"2026-06-09T04:10:00Z"}],
  "multi-b":[{"id":"run-b","status":"completed","result":{"output":"干完了","pr_url":""},"error":null,"agent_id":"agent-b","created_at":"2026-06-09T04:20:00Z"}],
  "multi-c":[{"id":"run-c","status":"running","result":null,"error":null,"agent_id":"agent-c","created_at":"2026-06-09T03:20:00Z"}]
}
JSON
cat >"$REG_TMP/rg_multi_comments.json" <<'JSON'
{"multi-a":"2026-06-09T04:10:00Z","multi-b":"2026-06-09T04:20:00Z","multi-c":"2026-06-09T03:20:00Z"}
JSON
rm -f "$REG_TMP/rg13.json"
o13=$(WATCHDOG_STATE=$REG_TMP/rg13.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_multi_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_multi_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_multi_comments.json \
  python3 line_watchdog.py --post --route --route-workers 3 --zombie-min 40 --stale-min 5 2>&1)
jobs=$(echo "$o13" | grep -c "^ROUTE_JOB ")
[ "$jobs" = 3 ] && ok "三异常拆成 3 个 ROUTE_JOB" || no "ROUTE_JOB 数量异常($jobs)"
echo "$o13" | grep -q "PL-A -> 线小队-T01" && ok "PL-A 分流 T01" || no "PL-A 未分流 T01"
echo "$o13" | grep -q "PL-B -> 线小队-T02" && ok "PL-B 分流 T02" || no "PL-B 未分流 T02"
echo "$o13" | grep -q "PL-C -> 线小队-T03" && ok "PL-C 分流 T03" || no "PL-C 未分流 T03"
bad_token="mention://""agent"
echo "$o13" | grep -q "$bad_token" && no "分流输出仍含个人 mention" || ok "分流输出无个人 mention"

echo "[14] BOM-10 扩容:8 个线小队、24 个异常必须全部扫描并分流"
python3 - <<'PY'
import json
import os
tmp=os.environ["REG_TMP"]
squads=[]; issues=[]; runs={}; comments={}
base_ids=[
 "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96",
 "9db04481-be45-48a1-8114-5f2b85506f78",
 "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52",
]
for n in range(1,9):
    sid=base_ids[n-1] if n<=3 else f"line-squad-t{n:02d}"
    squads.append({"name":f"线小队-T{n:02d}","id":sid,"leader_id":f"leader-t{n:02d}","archived_at":None})
    for k in range(1,4):
        iid=f"scale-{n:02d}-{k}"
        ident=f"PL-S{n:02d}-{k}"
        issues.append({"id":iid,"identifier":ident,"title":f"T{n:02d} scale {k}","status":"in_progress","assignee_id":sid,"assignee_type":"squad","number":n*100+k})
        if k==1:
            runs[iid]=[{"id":f"run-{iid}","status":"cancelled","result":None,"error":None,"agent_id":f"agent-{n}-{k}","created_at":"2026-06-09T04:10:00Z"}]
        elif k==2:
            runs[iid]=[{"id":f"run-{iid}","status":"completed","result":{"output":"干完了","pr_url":""},"error":None,"agent_id":f"agent-{n}-{k}","created_at":"2026-06-09T04:20:00Z"}]
        else:
            runs[iid]=[{"id":f"run-{iid}","status":"running","result":None,"error":None,"agent_id":f"agent-{n}-{k}","created_at":"2026-06-09T03:20:00Z"}]
        comments[iid]="2026-06-09T04:10:00Z"
json.dump({"squads":squads}, open(os.path.join(tmp, "rg_scale_squads.json"),"w"))
json.dump(issues, open(os.path.join(tmp, "rg_scale_issues.json"),"w"))
json.dump(runs, open(os.path.join(tmp, "rg_scale_runs.json"),"w"))
json.dump(comments, open(os.path.join(tmp, "rg_scale_comments.json"),"w"))
PY
rm -f "$REG_TMP/rg14.json"
o14=$(WATCHDOG_STATE=$REG_TMP/rg14.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_scale_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_scale_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_scale_comments.json \
  python3 line_watchdog.py --post --route --route-workers 0 --zombie-min 40 --stale-min 5 2>&1)
jobs14=$(echo "$o14" | grep -c "^ROUTE_JOB ")
[ "$jobs14" = 24 ] && ok "24 个异常拆成 24 个 ROUTE_JOB" || no "ROUTE_JOB 数量异常($jobs14)"
echo "$o14" | grep -q "PL-S08-3 -> 线小队-T08" && ok "T08 被动态发现并分流" || no "T08 未被扫描/分流"
echo "$o14" | grep -q "$bad_token" && no "8队分流输出仍含个人 mention" || ok "8队分流输出无个人 mention"

echo "[15] BOM-11 续派扩容:8 个空闲线小队 backlog 必须全部自动接上"
python3 - <<'PY'
import json
import os
tmp=os.environ["REG_TMP"]
base_ids=[
 "7dafa944-07b8-4fba-ab3d-1b7ae0ceda96",
 "9db04481-be45-48a1-8114-5f2b85506f78",
 "0f0013ea-a65d-4ab8-b1b4-17e14da8ab52",
]
items=[]
for n in range(1,9):
    sid=base_ids[n-1] if n<=3 else f"line-squad-t{n:02d}"
    items.append({"id":f"dispatch-{n:02d}","identifier":f"PL-D{n:02d}","title":f"T{n:02d} backlog","status":"backlog","assignee_id":sid,"assignee_type":"squad","number":n})
json.dump(items, open(os.path.join(tmp, "rg_dispatch_issues.json"),"w"))
PY
o15=$(LINE_DISPATCH_DRY_RUN=1 \
  LINE_DISPATCH_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  LINE_DISPATCH_ISSUES_FIXTURE=$REG_TMP/rg_dispatch_issues.json \
  python3 line_dispatch.py --post 2>&1)
dispatches=$(echo "$o15" | grep -c "would status dispatch-")
[ "$dispatches" = 8 ] && ok "8 个空闲小队全部续派" || no "续派数量异常($dispatches)"
echo "$o15" | grep -q "线小队-T08 ← PL-D08" && ok "T08 backlog 被续派" || no "T08 backlog 未续派"

echo "[16] BOM-12 组合失败:分流失败不得被去重吞掉,下一轮必须重试"
rm -f "$REG_TMP/rg16.json"
o16a=$(WATCHDOG_STATE=$REG_TMP/rg16.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_multi_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_multi_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_multi_comments.json \
  WATCHDOG_ROUTE_FAIL_IDS=multi-a \
  python3 line_watchdog.py --post --route --route-workers 3 --zombie-min 40 --stale-min 5 2>&1)
o16b=$(WATCHDOG_STATE=$REG_TMP/rg16.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_multi_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_multi_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_multi_comments.json \
  WATCHDOG_ROUTE_FAIL_IDS=multi-a \
  python3 line_watchdog.py --post --route --route-workers 3 --zombie-min 40 --stale-min 5 2>&1)
fails16=$(printf "%s\n%s\n" "$o16a" "$o16b" | grep -c "ROUTE_JOB_FAIL PL-A")
[ "$fails16" = 2 ] && ok "失败分流连续两轮都重试" || no "失败分流被吞掉(fails=$fails16)"
echo "$o16a" | grep -q "ROUTE_RETRY_ARMED" && ok "失败后写入重试保护" || no "失败后未解除去重状态"

echo "[17] BOM-13 续派占线:in_review/线主脑自持任务必须阻止误续派"
python3 - <<'PY'
import json
import os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"active-leader-t04","identifier":"PL-L04","title":"T04 audit pending","status":"in_review","assignee_id":"leader-t04","assignee_type":"agent","number":401},
 {"id":"dispatch-04b","identifier":"PL-D04B","title":"T04 backlog should wait","status":"backlog","assignee_id":"line-squad-t04","assignee_type":"squad","number":402},
 {"id":"dispatch-05b","identifier":"PL-D05B","title":"T05 backlog can start","status":"backlog","assignee_id":"line-squad-t05","assignee_type":"squad","number":502}
]
json.dump(items, open(os.path.join(tmp, "rg_dispatch_leader_busy.json"),"w"))
PY
o17=$(LINE_DISPATCH_DRY_RUN=1 \
  LINE_DISPATCH_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  LINE_DISPATCH_ISSUES_FIXTURE=$REG_TMP/rg_dispatch_leader_busy.json \
  python3 line_dispatch.py --post 2>&1)
echo "$o17" | grep -q "would status dispatch-04b" && no "T04 in_review 仍被误续派" || ok "T04 leader/in_review 占线未误派"
echo "$o17" | grep -q "would status dispatch-05b" && ok "T05 空闲仍可续派" || no "T05 空闲未续派"

echo "[18] BOM-14 归档冲突:archived 线小队不能被扫描续派"
python3 - <<'PY'
import json
import os
tmp=os.environ["REG_TMP"]
squads=[
 {"name":"线小队-T09","id":"line-squad-t09","leader_id":"leader-t09","archived_at":"2026-06-09T00:00:00Z"},
 {"name":"线小队-T10","id":"line-squad-t10","leader_id":"leader-t10","archived_at":None}
]
items=[
 {"id":"dispatch-09","identifier":"PL-D09","title":"archived backlog","status":"backlog","assignee_id":"line-squad-t09","assignee_type":"squad","number":9},
 {"id":"dispatch-10","identifier":"PL-D10","title":"active backlog","status":"backlog","assignee_id":"line-squad-t10","assignee_type":"squad","number":10}
]
json.dump({"squads":squads}, open(os.path.join(tmp, "rg_archived_squads.json"),"w"))
json.dump(items, open(os.path.join(tmp, "rg_archived_issues.json"),"w"))
PY
o18=$(LINE_DISPATCH_DRY_RUN=1 \
  LINE_DISPATCH_SQUADS_FIXTURE=$REG_TMP/rg_archived_squads.json \
  LINE_DISPATCH_ISSUES_FIXTURE=$REG_TMP/rg_archived_issues.json \
  python3 line_dispatch.py --post 2>&1)
echo "$o18" | grep -q "would status dispatch-09" && no "归档 T09 被误续派" || ok "归档 T09 未续派"
echo "$o18" | grep -q "would status dispatch-10" && ok "现役 T10 被续派" || no "现役 T10 未续派"

echo "[19] AB-1 没触发:active issue 无任何 run 超时必须告警并分流"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"norun-t04","identifier":"PL-NR04","title":"T04 no worker","status":"in_progress","assignee_id":"line-squad-t04","assignee_type":"squad","number":1901,"updated_at":"2026-06-09T03:00:00Z"}
]
json.dump(items, open(os.path.join(tmp, "rg_norun_issues.json"),"w"))
json.dump({}, open(os.path.join(tmp, "rg_norun_runs.json"),"w"))
json.dump({"norun-t04":"2026-06-09T03:00:00Z"}, open(os.path.join(tmp, "rg_norun_comments.json"),"w"))
PY
o19=$(WATCHDOG_STATE=$REG_TMP/rg19.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_norun_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_norun_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_norun_comments.json \
  python3 line_watchdog.py --post --route --route-workers 4 --stale-min 5 2>&1)
echo "$o19" | grep -qE "(无人接单|在办无run|中途断工|疑似断工).*PL-NR04" && ok "无 run 卡死被检出(U20: in_progress 归为在办无run)" || no "无 run 卡死漏检"
echo "$o19" | grep -q "PL-NR04 -> 线小队-T04" && ok "无 run 卡死分流 T04" || no "无 run 未分流 T04"

echo "[20] AB-2 组合冲突:线主脑自持 active 时,该线不得被空转误报"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"leader-busy-t04","identifier":"PL-LB04","title":"T04 leader busy","status":"in_progress","assignee_id":"leader-t04","assignee_type":"agent","number":2001,"updated_at":"2026-06-09T04:25:00Z"},
 {"id":"backlog-t04","identifier":"PL-BL04","title":"T04 backlog","status":"backlog","assignee_id":"line-squad-t04","assignee_type":"squad","number":2002}
]
runs={"leader-busy-t04":[{"id":"run-leader-busy","status":"running","result":None,"error":None,"agent_id":"leader-t04","created_at":"2026-06-09T04:25:00Z"}]}
json.dump(items, open(os.path.join(tmp, "rg_idle_leader_issues.json"),"w"))
json.dump(runs, open(os.path.join(tmp, "rg_idle_leader_runs.json"),"w"))
json.dump({"leader-busy-t04":"2026-06-09T04:25:00Z"}, open(os.path.join(tmp, "rg_idle_leader_comments.json"),"w"))
PY
o20=$(WATCHDOG_STATE=$REG_TMP/rg20.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_idle_leader_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_idle_leader_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_idle_leader_comments.json \
  python3 line_watchdog.py --post --route --route-workers 4 --stale-min 5 2>&1)
echo "$o20" | grep -q "空转待推进:.*线小队-T04" && no "T04 leader忙仍被误报空转" || ok "T04 leader忙未被误报空转"

echo "[21] AB-3 去重漏洞:空转 backlog/父任务数量变化必须重新提醒"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
one=[{"id":"idle-one","identifier":"PL-I1","title":"T04 one","status":"backlog","assignee_id":"line-squad-t04","assignee_type":"squad","number":2101}]
two=one+[{"id":"idle-two","identifier":"PL-I2","title":"T04 two","status":"backlog","assignee_id":"line-squad-t04","assignee_type":"squad","number":2102}]
json.dump(one, open(os.path.join(tmp, "rg_idle_one.json"),"w"))
json.dump(two, open(os.path.join(tmp, "rg_idle_two.json"),"w"))
json.dump({}, open(os.path.join(tmp, "rg_idle_runs.json"),"w"))
json.dump({}, open(os.path.join(tmp, "rg_idle_comments.json"),"w"))
PY
o21a=$(WATCHDOG_STATE=$REG_TMP/rg21.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_idle_one.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_idle_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_idle_comments.json \
  python3 line_watchdog.py --post 2>&1)
o21b=$(WATCHDOG_STATE=$REG_TMP/rg21.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_idle_two.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_idle_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_idle_comments.json \
  python3 line_watchdog.py --post 2>&1)
idle_posts=$(printf "%s\n%s\n" "$o21a" "$o21b" | grep -c "IDLE_POSTED")
[ "$idle_posts" = 2 ] && ok "空转签名变化后二次提醒" || no "空转签名变化未提醒(posts=$idle_posts)"

echo "[22] AB-4 读取失败:issue list 失败时禁止报 WATCHDOG OK"
o22=$(WATCHDOG_STATE=$REG_TMP/rg22.json WATCHDOG_ISSUE_LIST_FAIL=1 python3 line_watchdog.py --post 2>&1)
echo "$o22" | grep -q "issue列表读取失败" && ok "issue list 失败被告警" || no "issue list 失败未告警"
echo "$o22" | grep -q "WATCHDOG OK" && no "issue list 失败仍报 OK" || ok "issue list 失败未报 OK"

echo "[23] AB-5 覆盖漏洞:issue list 达到 limit 必须提示分页截断风险"
o23=$(WATCHDOG_STATE=$REG_TMP/rg23.json \
  WATCHDOG_ISSUE_LIMIT=2 \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_dispatch_issues.json \
  python3 line_watchdog.py --post 2>&1)
echo "$o23" | grep -q "扫描覆盖到达上限" && ok "limit 截断风险被告警" || no "limit 截断风险漏报"

echo "[24] AB-6 派单约束:任务派给个人 agent 必须告警并按 T 编号归队"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[{"id":"personal-t04","identifier":"PL-PERS","title":"T04 personal wrong assignee","status":"todo","assignee_id":"agent-bad-t04","assignee_type":"agent","number":2401,"updated_at":"2026-06-09T03:00:00Z"}]
json.dump(items, open(os.path.join(tmp, "rg_personal_issues.json"),"w"))
json.dump({}, open(os.path.join(tmp, "rg_personal_runs.json"),"w"))
json.dump({"personal-t04":"2026-06-09T03:00:00Z"}, open(os.path.join(tmp, "rg_personal_comments.json"),"w"))
PY
o24=$(WATCHDOG_STATE=$REG_TMP/rg24.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_personal_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_personal_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_personal_comments.json \
  python3 line_watchdog.py --post --route --route-workers 4 --stale-min 5 2>&1)
echo "$o24" | grep -q "个人派单违规.*PL-PERS" && ok "个人派单违规被检出" || no "个人派单违规漏检"
echo "$o24" | grep -q "PL-PERS -> 线小队-T04" && ok "个人派单按 T04 归队" || no "个人派单未归队 T04"

echo "[25] AB-7 续派读取失败:line_dispatch 不得输出队列为空"
o25=$(LINE_DISPATCH_DRY_RUN=1 LINE_DISPATCH_ISSUE_LIST_FAIL=1 python3 line_dispatch.py --post 2>&1)
echo "$o25" | grep -q "DISPATCH_FETCH_FAIL" && ok "续派 issue list 失败被告警" || no "续派 issue list 失败漏报"
echo "$o25" | grep -q "未续派;存在读取/覆盖告警" && ok "续派失败未伪装为空队列" || no "续派失败仍伪装为空队列"

echo "[26] AB-8 续派覆盖:line_dispatch 达到 limit 必须提示截断"
o26=$(LINE_DISPATCH_DRY_RUN=1 \
  LINE_DISPATCH_ISSUE_LIMIT=1 \
  LINE_DISPATCH_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  LINE_DISPATCH_ISSUES_FIXTURE=$REG_TMP/rg_dispatch_issues.json \
  python3 line_dispatch.py --post 2>&1)
echo "$o26" | grep -q "DISPATCH_COVERAGE_WARN" && ok "续派 limit 截断风险被告警" || no "续派 limit 截断风险漏报"

echo "[27] AB-9 小队发现失败:动态 squad list 失败必须告警,不能悄悄退回三队"
o27=$(WATCHDOG_STATE=$REG_TMP/rg27.json WATCHDOG_SQUAD_LIST_FAIL=1 python3 line_watchdog.py --post 2>&1)
echo "$o27" | grep -q "线小队发现失败" && ok "小队发现失败被告警" || no "小队发现失败未告警"

echo "[28] A段 U02/U07/U17 schema 漂移:出现未登记状态必须告警,不静默漏扫"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"drift-1","identifier":"PL-DRIFT","title":"new status","status":"reopened","assignee_id":"line-squad-t04","assignee_type":"squad","number":2801,"updated_at":"2026-06-09T03:00:00Z"},
 {"id":"blk-1","identifier":"PL-BLK","title":"blocked stuck","status":"blocked","assignee_id":"line-squad-t05","assignee_type":"squad","number":2802,"updated_at":"2026-06-09T03:00:00Z"},
]
json.dump(items, open(os.path.join(tmp,"rg_a_issues.json"),"w"))
json.dump({}, open(os.path.join(tmp,"rg_a_runs.json"),"w"))
json.dump({}, open(os.path.join(tmp,"rg_a_comments.json"),"w"))
PY
o28=$(WATCHDOG_STATE=$REG_TMP/rg28.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_a_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_a_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_a_comments.json \
  python3 line_watchdog.py --stale-min 5 2>&1)
echo "$o28" | grep -q "未登记的 issue 状态 'reopened'" && ok "U02/U07/U17 schema 漂移 reopened 被告警" || no "schema 漂移漏报"

echo "[29] A段 U02 blocked 滞留:blocked 超时未解阻必须告警(旧逻辑只看 ACTIVE_ST 会静默)"
echo "$o28" | grep -q "blocked 滞留.*PL-BLK" && ok "blocked 滞留被检出" || no "blocked 滞留漏检"

echo "[30] A段 U19/X04 收口矛盾:done/cancelled 仍有 running run 必须告警"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"term-1","identifier":"PL-TERM","title":"done but running","status":"done","assignee_id":"line-squad-t04","assignee_type":"squad","number":3001,"updated_at":"2026-06-09T04:00:00Z"},
]
json.dump(items, open(os.path.join(tmp,"rg_term_issues.json"),"w"))
json.dump({"term-1":[{"id":"run-term-1","status":"running","created_at":"2026-06-09T04:05:00Z"}]}, open(os.path.join(tmp,"rg_term_runs.json"),"w"))
json.dump({}, open(os.path.join(tmp,"rg_term_comments.json"),"w"))
PY
o30=$(WATCHDOG_STATE=$REG_TMP/rg30.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_term_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_term_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_term_comments.json \
  python3 line_watchdog.py --stale-min 5 2>&1)
echo "$o30" | grep -q "收口矛盾.*PL-TERM" && ok "U19 收口矛盾被检出" || no "收口矛盾漏检"

echo "[31] A段 U20 分级:todo 无人认领 vs in_progress 无 run 必须分两类"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"todo-1","identifier":"PL-TODO","title":"unclaimed","status":"todo","assignee_id":"line-squad-t04","assignee_type":"squad","number":3101,"updated_at":"2026-06-09T03:00:00Z"},
 {"id":"prog-1","identifier":"PL-PROG","title":"claimed but idle","status":"in_progress","assignee_id":"line-squad-t05","assignee_type":"squad","number":3102,"updated_at":"2026-06-09T03:00:00Z"},
]
json.dump(items, open(os.path.join(tmp,"rg_grade_issues.json"),"w"))
json.dump({}, open(os.path.join(tmp,"rg_grade_runs.json"),"w"))
json.dump({"todo-1":"2026-06-09T03:00:00Z","prog-1":"2026-06-09T03:00:00Z"}, open(os.path.join(tmp,"rg_grade_comments.json"),"w"))
PY
o31=$(WATCHDOG_STATE=$REG_TMP/rg31.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_grade_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_grade_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_grade_comments.json \
  python3 line_watchdog.py --stale-min 5 2>&1)
echo "$o31" | grep -q "待接单.*PL-TODO" && ok "U20 todo_no_claim 分级" || no "todo_no_claim 漏检"
echo "$o31" | grep -q "在办无run.*PL-PROG" && ok "U20 in_progress_no_run 分级" || no "in_progress_no_run 漏检"

echo "[32] B段 U28/U29 证据无法核验:completed 声称截图/裸PR号但无真实产物必须告警"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
items=[
 {"id":"unv-1","identifier":"PL-UNV","title":"fake screenshot","status":"in_progress","assignee_id":"line-squad-t04","assignee_type":"squad","number":3201,"updated_at":"2026-06-09T04:10:00Z"},
]
# 04:10 完成,距 WATCHDOG_NOW(04:28)18 分钟,超出 X46 截图上传宽限(默认10分),应正常判 evidence_unverified。
runs={"unv-1":[{"id":"run-unv","status":"completed","result":{"output":"已完成,截图见上 PASS","pr_url":""},"error":None,"agent_id":"a","created_at":"2026-06-09T04:10:00Z"}]}
json.dump(items, open(os.path.join(tmp,"rg_unv_issues.json"),"w"))
json.dump(runs, open(os.path.join(tmp,"rg_unv_runs.json"),"w"))
json.dump({"unv-1":"2026-06-09T04:10:00Z"}, open(os.path.join(tmp,"rg_unv_comments.json"),"w"))
PY
o32=$(WATCHDOG_STATE=$REG_TMP/rg32.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/rg_scale_squads.json \
  WATCHDOG_FIXTURE=$REG_TMP/rg_unv_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_unv_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_unv_comments.json \
  python3 line_watchdog.py --stale-min 5 2>&1)
echo "$o32" | grep -q "证据无法核验.*PL-UNV" && ok "U28/U29 evidence_unverified 被检出" || no "evidence_unverified 漏检"

echo "[33] B段 U25/U26 Qwen/DeepSeek endpoint 探针故障必须告警(注入 fixture,全离线)"
o33=$(WATCHDOG_STATE=$REG_TMP/rg33.json WATCHDOG_ISSUE_LIST_FAIL=1 \
  QWEN_API_URL=http://q DEEPSEEK_API_URL=http://d \
  WATCHDOG_PROBE_FIXTURE='{"qwen":false,"deepseek":true}' \
  python3 line_watchdog.py --probe 2>&1)
echo "$o33" | grep -q "Qwen门禁 endpoint 不可用" && ok "Qwen endpoint 故障被探出" || no "Qwen 探针漏报"
echo "$o33" | grep -q "DeepSeek深挖 endpoint 不可用" && no "DeepSeek 正常却误报" || ok "DeepSeek 正常未误报"

echo "[34] B段 U27 真机探针:adb 无设备在线必须告警"
o34=$(WATCHDOG_STATE=$REG_TMP/rg34.json WATCHDOG_ISSUE_LIST_FAIL=1 \
  QWEN_API_URL=http://q DEEPSEEK_API_URL=http://d \
  WATCHDOG_PROBE_FIXTURE='{"qwen":true,"deepseek":true,"device_online":0}' \
  python3 line_watchdog.py --probe --probe-device 2>&1)
echo "$o34" | grep -q "真机探针失败" && ok "U27 真机离线被探出" || no "真机探针漏报"

echo "[35] B段 U36 续派后二次确认:有新run=CONFIRMED,无新run=UNCONFIRMED"
cat >"$REG_TMP/rg_conf_issues.json" <<'JSON'
[{"id":"conf-a","identifier":"PL-CONF","title":"T01 cancelled","status":"in_progress","assignee_id":"7dafa944-07b8-4fba-ab3d-1b7ae0ceda96","assignee_type":"squad","number":3501}]
JSON
cat >"$REG_TMP/rg_conf_runs.json" <<'JSON'
{"conf-a":[{"id":"run-conf","status":"cancelled","result":null,"error":null,"agent_id":"a","created_at":"2026-06-09T04:10:00Z"}]}
JSON
cat >"$REG_TMP/rg_conf_comments.json" <<'JSON'
{"conf-a":"2026-06-09T04:10:00Z"}
JSON
# 全离线:DRY_RUN 模拟路由成功,confirm_rerun 用 fixture runs。
# WATCHDOG_NOW=路由时刻;fixture 04:10 的 run 晚于 04:08 -> CONFIRMED;晚于 04:20 则无新run -> UNCONFIRMED。
rm -f "$REG_TMP/rg35.json"
o35c=$(WATCHDOG_DRY_RUN=1 WATCHDOG_STATE=$REG_TMP/rg35.json \
  WATCHDOG_NOW=2026-06-09T04:08:00Z WATCHDOG_CONFIRM_RERUN=1 \
  WATCHDOG_FIXTURE=$REG_TMP/rg_conf_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_conf_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_conf_comments.json \
  python3 line_watchdog.py --post --route --route-workers 1 --zombie-min 40 --stale-min 5 2>&1)
echo "$o35c" | grep -q "ROUTE_CONFIRMED PL-CONF" && ok "U36 有新run -> ROUTE_CONFIRMED" || no "U36 确认成功漏报"
rm -f "$REG_TMP/rg35b.json"
o35u=$(WATCHDOG_DRY_RUN=1 WATCHDOG_STATE=$REG_TMP/rg35b.json \
  WATCHDOG_NOW=2026-06-09T04:20:00Z WATCHDOG_CONFIRM_RERUN=1 \
  WATCHDOG_FIXTURE=$REG_TMP/rg_conf_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_conf_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_conf_comments.json \
  python3 line_watchdog.py --post --route --route-workers 1 --zombie-min 40 --stale-min 5 2>&1)
echo "$o35u" | grep -q "ROUTE_UNCONFIRMED PL-CONF" && ok "U36 无新run -> ROUTE_UNCONFIRMED" || no "U36 未确认漏报"

echo "[36] B段 U37 PL-94 兜底:贴台失败必须落本地 fallback 文件,告警不丢(WATCHDOG_COMMENT_FAIL 钩子,不触真台)"
rm -f "$REG_TMP/fallback.jsonl"
o36=$(WATCHDOG_DRY_RUN=1 WATCHDOG_COMMENT_FAIL=1 \
  WATCHDOG_STATE=$REG_TMP/rg36.json \
  WATCHDOG_FALLBACK_F=$REG_TMP/fallback.jsonl \
  WATCHDOG_FIXTURE=$REG_TMP/rg_multi_issues.json \
  WATCHDOG_RUNS_FIXTURE=$REG_TMP/rg_multi_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/rg_multi_comments.json \
  python3 line_watchdog.py --post 2>&1)
echo "$o36" | grep -q "HANDOFF_FALLBACK" && [ -s "$REG_TMP/fallback.jsonl" ] && ok "U37 贴台失败已落本地兜底文件" || no "U37 兜底未触发/文件为空"

echo "[37] B段 U30 evidence ledger:门禁 PASS+真PR 的 run 必须归档来源/verdict/trust"
o37=$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PWD","."))
import line_evidence as E
runs=[{"id":"r1","status":"completed","created_at":"2026-06-09T04:00:00Z","result":"【门禁 Qwen】真机可见 VERDICT: PASS https://github.com/o/r/pull/3"}]
led=E.build_ledger("iid","PL-LEDGER",runs)
e=led["entries"][0]
import json
print("LEDGER", e["source"], e["verdict"], e["trust"], len(e["pr_urls"]))
PY
)
echo "$o37" | grep -q "LEDGER qwen_gate PASS trusted 1" && ok "U30 ledger 记录 source/verdict/trust/pr" || no "U30 ledger 字段异常($o37)"

echo "[38] B段 U31 信任策略:门禁阶段 self-report PASS 不可信,trusted 来源才放行"
o38=$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PWD","."))
import line_evidence as E
g_self=E.passes_trust_policy("gate", E.classify_source("我自测门禁通过 PASS"))
g_qwen=E.passes_trust_policy("gate", E.classify_source("【门禁 Qwen】VERDICT: PASS"))
print("TRUST", g_self, g_qwen)
PY
)
echo "$o38" | grep -q "TRUST False True" && ok "U31 门禁 self-report 拒绝 / trusted 放行" || no "U31 信任策略异常($o38)"

echo "[39] PL-124 P0:U30/U31/U32/U23 接进看门狗 detect 审计路径(cron 热路径),U03 二次补全父任务"
o39=$(WATCHDOG_ISSUE_GET_FIXTURE=$REG_TMP/rg_getpar.json python3 - <<'PY'
import os, sys, json
sys.path.insert(0, os.environ.get("PWD","."))
os.environ["WATCHDOG_NOW"] = "2026-06-09T04:28:00Z"
import line_watchdog as W
# 全离线:固定小队映射,屏蔽真实 CLI
W.load_line_squads = lambda: W.DEFAULT_SQUADS
W.brain_to_squad = lambda: {}
sq = list(W.DEFAULT_SQUADS.values())[0]
W.last_comment_age = lambda iid: 1.0
W._RUN_FETCH_WARNINGS.clear()

# U32:done 收口阶段,旧 PASS 被更新的门禁 FAIL 证据覆盖 -> 必须返工(PL-132)。
# PL-132 起 stale_evidence 已弃用:裸 failed run(无 VERDICT,崩溃/中断)不再使旧 PASS 失效
# (根治 PL-128 自燃死循环);只有"最新有效门禁证据=FAIL"才覆盖旧 PASS,统一 emit gate_fail_rework
# (即"旧 PASS 不得收口、走返工点")。故新覆盖 run 须带真实 VERDICT: FAIL。
i32 = {"id": "i32", "identifier": "PL-32", "status": "done", "assignee_id": sq,
       "created_at": "2026-06-09T04:20:00Z", "updated_at": "2026-06-09T04:20:00Z"}
runs32 = [{"id": "rf", "status": "completed", "created_at": "2026-06-09T04:10:00Z",
           "result": "VERDICT: FAIL 门禁复核不过,旧 PASS 失效需返工"},
          {"id": "rp", "status": "completed", "created_at": "2026-06-09T04:00:00Z",
           "result": "VERDICT: PASS https://github.com/o/r/pull/1"}]
W.fetch_runs = lambda iid: runs32
W.comment_has_trusted_pass = lambda iid, n=5: False
k32 = {a["kind"] for a in W.detect([i32], 40.0, 5.0)}
print("U32", "gate_fail_rework" in k32)
print("U30", os.path.exists(os.path.join(os.environ["WATCHDOG_LEDGER_DIR"], "PL-32.json")))

# U31:in_review 门禁阶段 self-report PASS + 评论无可信背书 -> evidence_untrusted;有背书则压制
i31 = {"id": "i31", "identifier": "PL-31", "status": "in_review", "assignee_id": sq,
       "created_at": "2026-06-09T04:25:00Z", "updated_at": "2026-06-09T04:25:00Z"}
runs31 = [{"id": "rp2", "status": "completed", "created_at": "2026-06-09T04:25:00Z",
           "result": "我自测通过 VERDICT: PASS"}]
W.fetch_runs = lambda iid: runs31
W.comment_has_trusted_pass = lambda iid, n=5: False
k31a = {a["kind"] for a in W.detect([i31], 40.0, 5.0)}
W.comment_has_trusted_pass = lambda iid, n=5: True
k31b = {a["kind"] for a in W.detect([i31], 40.0, 5.0)}
print("U31", ("evidence_untrusted" in k31a) and ("evidence_untrusted" not in k31b))

# U23:父任务已 done 但子任务仍 in_progress -> parent_done_child_open
parent = {"id": "p1", "identifier": "PL-P", "status": "done", "assignee_id": sq,
          "created_at": "2026-06-09T04:25:00Z", "updated_at": "2026-06-09T04:25:00Z"}
child = {"id": "c1", "identifier": "PL-C", "status": "in_progress", "assignee_id": sq,
         "parent_issue_id": "p1", "created_at": "2026-06-09T04:25:00Z"}
W.fetch_runs = lambda iid: []
k23 = {a["kind"] for a in W.detect([parent, child], 40.0, 5.0)}
print("U23", "parent_done_child_open" in k23)

# U03:子任务引用的父 issue 不在本轮 items,从 get-fixture 二次补全
json.dump({"pmissing": {"id": "pmissing", "identifier": "PL-PAR", "status": "in_progress"}},
          open(os.environ["WATCHDOG_ISSUE_GET_FIXTURE"], "w"))
items = [{"id": "cc", "identifier": "PL-CC", "assignee_id": sq, "parent_issue_id": "pmissing"}]
sup = W.supplement_missing_parents(items)
print("U03", len(sup) == 1 and sup[0]["id"] == "pmissing")

# U06:最近 N 条评论里有可信门禁 PASS 时,comment_has_trusted_pass=True(自报则 False)
json.dump({"i06": [
    {"created_at": "2026-06-09T04:20:00Z", "content": "我自测 PASS", "author_name": "Claude开发"},
    {"created_at": "2026-06-09T04:21:00Z", "content": "【门禁 Qwen】VERDICT: PASS", "author_name": "门禁 Qwen"},
]}, open(os.environ["REG_TMP"] + "/rg_comm06.json", "w"))
os.environ["WATCHDOG_COMMENT_LIST_FIXTURE"] = os.environ["REG_TMP"] + "/rg_comm06.json"
W._COMM_LIST_FX = None
trusted = W.comment_has_trusted_pass("i06")
del os.environ["WATCHDOG_COMMENT_LIST_FIXTURE"]; W._COMM_LIST_FX = None
print("U06", trusted is True)
PY
)
echo "$o39" | grep -q "U32 True"  && ok "U32 旧PASS被新门禁FAIL覆盖->gate_fail_rework返工(审计路径,PL-132)" || no "U32 未在 detect 触发($o39)"
echo "$o39" | grep -q "U30 True"  && ok "U30 ledger 在 detect 审计路径落盘"                || no "U30 ledger 未落盘($o39)"
echo "$o39" | grep -q "U31 True"  && ok "U31 self-report PASS 告警 / 可信背书压制"          || no "U31 信任策略未接入 detect($o39)"
echo "$o39" | grep -q "U23 True"  && ok "U23 父done×子未收口->parent_done_child_open"        || no "U23 未独立检测($o39)"
echo "$o39" | grep -q "U03 True"  && ok "U03 缺失父任务二次补全(issue get)"                 || no "U03 补全失败($o39)"
echo "$o39" | grep -q "U06 True"  && ok "U06 最近N条评论证据归类(可信门禁PASS识别)"        || no "U06 评论归类失败($o39)"

echo "[40] PL-124 P1:U08 时钟偏移 / U13 跨小队协作 / U14 重建代际去重 / U21 typed阶段 / U22 role-stage状态字段"
o40=$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PWD","."))
os.environ["WATCHDOG_NOW"] = "2026-06-09T04:28:00Z"
import line_states as S, line_watchdog as W
W.load_line_squads = lambda: W.DEFAULT_SQUADS
W.brain_to_squad = lambda: {}
W._RUN_FETCH_WARNINGS.clear()
sqs = list(W.DEFAULT_SQUADS.values())
sq = sqs[0]

# U08:出现未来时间戳(04:31 > now 04:28,超 2min 容差)-> clock_skew;容差内不报
sk, _, _ = S.clock_skew_alert(W.now_utc(), ["2026-06-09T04:31:00Z", "2026-06-09T04:00:00Z"], 2.0)
sk_ok, _, _ = S.clock_skew_alert(W.now_utc(), ["2026-06-09T04:29:00Z"], 2.0)
print("U08", sk is True and sk_ok is False)

# U14:同 ident+kind、不同 iid(归档重建拿新 UUID)-> 去重键必须不同,不被旧签名吞掉
a_old = {"ident": "PL-9", "squad": "sq", "iid": "old-uuid", "kind": "zombie"}
a_new = {"ident": "PL-9", "squad": "sq", "iid": "new-uuid", "kind": "zombie"}
print("U14", W.alert_state_key(a_old) != W.alert_state_key(a_new))

# U21:结构化字段优先(纯无关文本也能判),回退正则
e1, t1 = S.event_from_run({"event": "gate_pass"}, "完全无关的文本")
e2, t2 = S.event_from_run({"stage": "audit", "verdict": "FAIL"}, "")
e3, t3 = S.event_from_run({}, "专属审计 VERDICT: PASS")
print("U21", e1 == "gate_pass" and t1 and e2 == "audit_fail" and t2 and e3 == "audit_pass" and not t3)

# U22:compute_stage_states 把当前 role-stage 落成状态字段(typed 优先)
W.resolve_squad = lambda aid: aid if aid in sqs else None
it = [{"id": "i1", "identifier": "PL-A", "status": "in_review", "assignee_id": sq}]
W.fetch_runs = lambda iid: [{"id": "r1", "status": "completed",
    "created_at": "2026-06-09T04:20:00Z", "stage": "gate", "verdict": "PASS"}]
ss = W.compute_stage_states(it)
print("U22", ss.get("i1", {}).get("event") == "gate_pass"
      and ss["i1"]["role"] == "qwen_gate" and ss["i1"]["typed"] is True)

# U13:父任务的未收口子任务跨 2 个线小队 -> cross_squad_collab
W.last_comment_age = lambda iid: 1.0
W.fetch_runs = lambda iid: []
parent = {"id": "p1", "identifier": "PL-P", "status": "in_progress", "assignee_id": sqs[0]}
c1 = {"id": "c1", "identifier": "PL-C1", "status": "in_progress",
      "assignee_id": sqs[0], "parent_issue_id": "p1"}
c2 = {"id": "c2", "identifier": "PL-C2", "status": "in_progress",
      "assignee_id": sqs[1], "parent_issue_id": "p1"}
kinds = {a["kind"] for a in W.detect([parent, c1, c2], 40.0, 5.0)}
print("U13", "cross_squad_collab" in kinds)
PY
)
echo "$o40" | grep -q "U08 True" && ok "U08 时钟偏移探针:未来时间戳告警/容差内不误报" || no "U08 时钟偏移异常($o40)"
echo "$o40" | grep -q "U13 True" && ok "U13 跨小队协作建模:父任务子任务跨队->cross_squad_collab" || no "U13 跨队建模异常($o40)"
echo "$o40" | grep -q "U14 True" && ok "U14 去重键带 squad+代际:重建同T编号不被旧签名吞" || no "U14 去重键异常($o40)"
echo "$o40" | grep -q "U21 True" && ok "U21 typed 阶段事件优先(结构化字段),回退正则" || no "U21 typed 阶段异常($o40)"
echo "$o40" | grep -q "U22 True" && ok "U22 role-stage 落进 stage_state 状态字段(typed优先)" || no "U22 stage_state 异常($o40)"

echo "[41] PL-124 P2:U15/U70权限漂移 U59/U60资源 U61记忆清理 U68版本pin U69代理 U71图片 U72总开关/canary U51轮ledger U65释放门"
o41=$(python3 - <<'PY'
import os, sys, json
sys.path.insert(0, os.environ.get("PWD","."))
os.environ["WATCHDOG_NOW"] = "2026-06-09T04:28:00Z"
import line_states as S, line_observe as O, line_evidence as E, line_watchdog as W

# U15+U70:CLI 不可用 + 文件缺失/属主漂移/不可写 -> cli_permission_drift + permission_drift
p = S.permission_drift_alert(False, [
    {"path": "/a", "exists": False},
    {"path": "/b", "exists": True, "owner": "stranger", "writable": True},
    {"path": "/c", "exists": True, "owner": "fleet", "writable": False}])
pk = {k for k, _, _ in p}
print("U15U70", "cli_permission_drift" in pk and "permission_drift" in pk and len(p) == 4)

# U59+U60:内存低告警 / 磁盘满告警;阈值内不误报
r_hit = {k for k, _, _ in S.resource_pressure_alert(3.0, 95.0)}
r_ok = S.resource_pressure_alert(70.0, 80.0)
print("U59U60", r_hit == {"memory_pressure", "disk_pressure"} and r_ok == [])

# U60:白名单清理候选只挑 .bak/.log.N/.tmp,绝不碰源码
cand = {e["name"] for e in S.cleanup_candidates(
    [{"name": "x.bak.pl1"}, {"name": "line_watchdog.py"}, {"name": "a.log.3"}])}
print("U60", cand == {"x.bak.pl1", "a.log.3"})

# U61:无 marker/超 2h 到期,2h 内不到期
d1 = S.memory_cleanup_due(None, W.now_utc())[0]
d2 = S.memory_cleanup_due("2026-06-09T01:00:00Z", W.now_utc(), 2.0)[0]
d3 = S.memory_cleanup_due("2026-06-09T03:30:00Z", W.now_utc(), 2.0)[0]
print("U61", d1 is True and d2 is True and d3 is False)

# U68:未 pin 告警 / 漂移告警 / 一致放行
print("U68", [k for k, _, _ in S.cli_version_drift_alert("multica dev", "")] == ["cli_version_unpinned"]
      and [k for k, _, _ in S.cli_version_drift_alert("multica 2", "multica dev")] == ["cli_version_drift"]
      and S.cli_version_drift_alert("multica dev", "multica dev") == [])

# U69:出口不通告警,通则静默
pr_down = lambda u: (False, "down")
pr_up = lambda u: (True, "ok")
print("U69", [k for k, _, _ in S.proxy_health_alert([{"name": "P", "url": "http://x:1"}], pr_down)] == ["proxy_down"]
      and S.proxy_health_alert([{"name": "P", "url": "http://x:1"}], pr_up) == [])

# U71:超阈值图片告警,小图/非图静默
big = S.image_oversize_alert([{"filename": "a.png", "size_bytes": 5 * 1024 * 1024}])
small = S.image_oversize_alert([{"filename": "a.png", "size_bytes": 100}, {"filename": "b.txt", "size_bytes": 9 * 1024 * 1024}])
print("U71", [k for k, _, _ in big] == ["image_oversize"] and small == [])

# U72:总开关(env/文件) + canary 灰度(稳定哈希、边界、近似比例)
dis_env = O.watchdog_disabled({"WATCHDOG_DISABLED": "1"})[0]
dis_off = O.watchdog_disabled({})[0]
c100 = O.canary_allows("abc", 100); c0 = O.canary_allows("abc", 0)
stable = O.canary_allows("id-5", 30) == O.canary_allows("id-5", 30)
ratio = sum(O.canary_allows("id-%d" % i, 30) for i in range(200))
print("U72", dis_env is True and dis_off is False and c100 is True and c0 is False and stable and 30 <= ratio <= 90)

# U51:每轮 ledger 统计 kind 计数 / 路由成败 / 落盘 jsonl
cyc = E.build_cycle_ledger(W.now_utc(), 12,
    [{"kind": "zombie"}, {"kind": "zombie"}, {"kind": "disk_pressure"}],
    "full", [{"ok": True}, {"ok": False}], "green", 30)
d = os.path.join(os.environ["REG_TMP"], "cyc")
path = E.write_cycle_ledger(cyc, ledger_dir=d)
ok_write = bool(path) and os.path.exists(path)
print("U51", cyc["alert_kinds"] == {"zombie": 2, "disk_pressure": 1}
      and cyc["routed_ok"] == 1 and cyc["routed_fail"] == 1 and ok_write)

# U15-U71:system_health_alerts 离线 fixture 端到端聚合(不触真 proc/df/网络)
fx = {"cli_ok": False, "mem_avail_pct": 2.0, "disk_used_pct": 99.0, "mem_cleanup_last": None,
      "cli_version": "multica dev", "files": [{"path": "/k", "exists": False}],
      "proxies": [{"name": "P", "url": "http://p:1"}], "proxy_down_urls": ["http://p:1"],
      "attachments": [{"filename": "big.png", "size_bytes": 9 * 1024 * 1024}]}
os.environ["WATCHDOG_SYSHEALTH_FIXTURE"] = json.dumps(fx)
W.load_line_squads = lambda: W.DEFAULT_SQUADS
sk = {al["kind"] for al in W.system_health_alerts({}, None)}
want = {"cli_permission_drift", "permission_drift", "memory_pressure", "disk_pressure",
        "memory_cleanup_due", "cli_version_unpinned", "proxy_down", "image_oversize"}
print("SYS", want <= sk)
PY
)
echo "$o41" | grep -q "U15U70 True" && ok "U15/U70 权限漂移:CLI失效+文件缺失/属主/不可写" || no "U15/U70 异常($o41)"
echo "$o41" | grep -q "U59U60 True" && ok "U59/U60 内存/磁盘压力探针(阈值内不误报)" || no "U59/U60 异常($o41)"
echo "$o41" | grep -q "U60 True"    && ok "U60 白名单清理候选只挑 .bak/.log.N/.tmp" || no "U60 清理白名单异常($o41)"
echo "$o41" | grep -q "U61 True"    && ok "U61 记忆清理定时(无marker/超2h到期,2h内不到期)" || no "U61 异常($o41)"
echo "$o41" | grep -q "U68 True"    && ok "U68 CLI版本 pin 漂移探针(未pin/漂移/一致)" || no "U68 异常($o41)"
echo "$o41" | grep -q "U69 True"    && ok "U69 网络出口/代理健康探针" || no "U69 异常($o41)"
echo "$o41" | grep -q "U71 True"    && ok "U71 超阈值图片token保护(小图/非图静默)" || no "U71 异常($o41)"
echo "$o41" | grep -q "U72 True"    && ok "U72 总开关(env/文件)+canary稳定哈希灰度" || no "U72 异常($o41)"
echo "$o41" | grep -q "U51 True"    && ok "U51 每轮 ledger:kind计数/路由成败/落盘 jsonl" || no "U51 异常($o41)"
echo "$o41" | grep -q "SYS True"    && ok "U15-U71 system_health_alerts 离线聚合端到端" || no "system_health 聚合异常($o41)"

echo "[PL-137] 临时停用 / TTL 到期自动恢复 / 停用态状态签名"
o137=$(python3 - <<'PY'
import os, sys, tempfile
sys.path.insert(0, ".")
import line_observe as O
os.environ["WATCHDOG_NOW"] = "2026-06-10T12:00:00Z"
def flag(text, mtime=None):
    fd, p = tempfile.mkstemp(dir=os.environ["REG_TMP"], suffix=".disabled")
    os.write(fd, text.encode()); os.close(fd)
    if mtime: os.utime(p, (mtime, mtime))
    return p
env_on  = O.watchdog_disabled({"WATCHDOG_DISABLED": "1"})[0]      # 环境变量停用
legacy  = O.watchdog_disabled({}, flag("disabled_by_cx"))[0]       # 旧式自由文本=手动停用
fut     = O.watchdog_disabled({}, flag("until: 2026-06-10T18:00:00Z"))[0]  # 未到期=停用
past_d, past_r = O.watchdog_disabled({}, flag("until: 2026-06-10T06:00:00Z"))  # 已到期=自动恢复
ttl_d   = O.watchdog_disabled({}, flag("ttl_min: 5", mtime=1749000000))[0]    # mtime 很久前=到期恢复
print("DIS", env_on is True and legacy is True and fut is True
      and past_d is False and "到期" in past_r and ttl_d is False)
# 停用态状态签名落盘
hbp = os.path.join(os.environ["REG_TMP"], "dis_hb.json")
stp = os.path.join(os.environ["REG_TMP"], "dis_st.json")
hb, st = O.write_disabled_state("stop_spam", hbp, stp)
import json
st2 = json.load(open(stp)); hb2 = json.load(open(hbp))
print("SIG", st2.get("overall") == "disabled" and st2.get("disabled") is True
      and st2.get("disabled_reason") == "stop_spam" and hb2.get("disabled") is True)
PY
)
echo "$o137" | grep -q "DIS True" && ok "PL-137 停用判定:env/旧式/未到期=停用,until/ttl 到期=自动恢复" || no "PL-137 停用判定异常($o137)"
echo "$o137" | grep -q "SIG True" && ok "PL-137 停用态状态签名:overall=disabled + disabled 心跳" || no "PL-137 状态签名异常($o137)"

# PL-137 端到端:停用标志在场=只读(无 POSTED,状态板 disabled);过期标志=自动恢复(POSTED)
FBASE="WATCHDOG_DRY_RUN=1 WATCHDOG_NOW=2026-06-09T04:28:00Z WATCHDOG_FIXTURE=$T/pl89_issues.json WATCHDOG_RUNS_FIXTURE=$T/pl89_runs.json WATCHDOG_COMMENTS_FIXTURE=$T/pl89_comments.json WATCHDOG_LEDGER_DIR=$REG_TMP/l137"
printf 'reason: stop_spam\n' > "$REG_TMP/flag_on.disabled"
d137on=$(eval $FBASE WATCHDOG_DISABLE_FLAG=$REG_TMP/flag_on.disabled WATCHDOG_STATUS_F=$REG_TMP/s137.json WATCHDOG_HEARTBEAT_F=$REG_TMP/h137.json WATCHDOG_STATE=$REG_TMP/st137on.json python3 line_watchdog.py --post --route --emit-observability 2>&1)
echo "$d137on" | grep -q "WATCHDOG_DISABLED 本轮停用" && ! echo "$d137on" | grep -q "POSTED" \
  && ok "PL-137 端到端:停用标志在场=只读(无 POSTED/ROUTE)" || no "PL-137 停用未生效(仍 POST/ROUTE)"
python3 -c "import json;d=json.load(open('$REG_TMP/s137.json'));exit(0 if d.get('overall')=='disabled' and d.get('disabled') is True else 1)" \
  && ok "PL-137 端到端:状态板 overall=disabled 签名落盘" || no "PL-137 状态板未打 disabled 签名"
printf 'until: 2026-06-08T00:00:00Z\n' > "$REG_TMP/flag_exp.disabled"
d137ex=$(eval $FBASE WATCHDOG_DISABLE_FLAG=$REG_TMP/flag_exp.disabled WATCHDOG_STATE=$REG_TMP/st137ex.json python3 line_watchdog.py --post 2>&1)
echo "$d137ex" | grep -q "WATCHDOG_DISABLE_EXPIRED" && echo "$d137ex" | grep -q "POSTED" \
  && ok "PL-137 端到端:过期标志=自动恢复并正常 POST(不吞恢复条件)" || no "PL-137 过期标志未自动恢复"

echo "[X24] 成员维度统计接入看门狗主路径:--emit-observability 状态板落 member_stats(每线人数/工作/闲置/卡死)"
python3 - <<'PY'
import json, os
tmp=os.environ["REG_TMP"]
squads=[{"name":"线小队-T01","id":"7dafa944-07b8-4fba-ab3d-1b7ae0ceda96","leader_id":"L1","archived_at":None},
        {"name":"线小队-T02","id":"9db04481-be45-48a1-8114-5f2b85506f78","leader_id":"L2","archived_at":None}]
json.dump({"squads":squads}, open(tmp+"/x24_sq.json","w"))
# T01:3 人(含开发岗 id=队id,工作中);T02:2 人(全闲置)。member_id 归一成 id。
members={"线小队-T01":[{"member_id":"m1","role":"leader"},
                      {"member_id":"7dafa944-07b8-4fba-ab3d-1b7ae0ceda96","role":"写/开发"},
                      {"member_id":"m3","role":"审计"}],
         "线小队-T02":[{"member_id":"m4","role":"leader"},{"member_id":"m5","role":"写/开发"}]}
json.dump(members, open(tmp+"/x24_mem.json","w"))
items=[{"id":"i1","identifier":"PL-X24","status":"in_progress","assignee_id":"7dafa944-07b8-4fba-ab3d-1b7ae0ceda96","assignee_type":"squad","number":1,"updated_at":"2026-06-09T04:25:00Z"}]
json.dump(items, open(tmp+"/x24_items.json","w"))
json.dump({"i1":[]}, open(tmp+"/x24_runs.json","w"))
json.dump({"i1":"2026-06-09T04:25:00Z"}, open(tmp+"/x24_comm.json","w"))
PY
ox24=$(WATCHDOG_DISABLE_FLAG=/nonexistent/wd.disabled \
  WATCHDOG_STATE=$REG_TMP/x24_st.json WATCHDOG_STATUS_F=$REG_TMP/x24_status.json WATCHDOG_HEARTBEAT_F=$REG_TMP/x24_hb.json \
  WATCHDOG_SQUADS_FIXTURE=$REG_TMP/x24_sq.json WATCHDOG_MEMBERS_FIXTURE=$REG_TMP/x24_mem.json \
  WATCHDOG_FIXTURE=$REG_TMP/x24_items.json WATCHDOG_RUNS_FIXTURE=$REG_TMP/x24_runs.json \
  WATCHDOG_COMMENTS_FIXTURE=$REG_TMP/x24_comm.json WATCHDOG_LEDGER_DIR=$REG_TMP/x24_led \
  python3 line_watchdog.py --stale-min 5 --emit-observability 2>&1)
echo "$ox24" | grep -q "MEMBER_STATS lines=2 total_headcount=5" && ok "X24 成员维度统计在主路径产出(打印 MEMBER_STATS)" || no "X24 主路径未产出成员统计($ox24)"
python3 -c "
import json
d=json.load(open('$REG_TMP/x24_status.json')).get('member_stats',{})
t1=d.get('线小队-T01',{}); t2=d.get('线小队-T02',{})
exit(0 if t1.get('headcount')==3 and t1.get('working')==1 and t1.get('idle')==2 and t2.get('headcount')==2 and t2.get('idle')==2 else 1)
" && ok "X24 状态板 member_stats 落盘(T01:3人/1工作/2闲置;T02:2人/2闲置)" || no "X24 status.json member_stats 不符"

echo
echo "==== 回归汇总:PASS=$pass FAIL=$fail ===="
[ "$fail" = 0 ] && echo "ALL GREEN" || echo "HAS FAILURES"
exit $fail
