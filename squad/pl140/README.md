# PL-140 落地物(可审计)

看门狗(line-watchdog)PARTIAL 16 项复核补齐的真实运行代码 + 单测 + 探测证据。
逐项结论见 [`PL140_MATRIX.md`](./PL140_MATRIX.md)。

## 文件
- `line_watchdog.py` / `line_dispatch.py` —— **现役热路径脚本**。本轮返工把下列 10 项从
  `line_partial.py` 旁路库**接入这两个文件的真实调用链**(call site 见下表)。
- `line_observe.py` / `line_states.py` —— 热路径 import 依赖(随同入库,使单测在本目录可跑)。
- `line_partial.py` —— PARTIAL 项的纯函数库(被上面热路径 import 消费,不再是旁路)。
- `line_done_gate.py` —— X32:收口门禁第 6 条 `evidence_fresh`(旧 PASS 新鲜度失效)。
- `line_evidence.py` —— X32/X51 依赖(`evidence_is_stale` / `verify_github_refs` 等)。
- `line_bridge.py` —— X20/BOM-4 门禁桥:`gate()` 在调任何模型前对空证据 / 页面任务无 URL+截图
  **直接判 FAIL**(`line_bridge.py:107-112`,离线确定性,回归 `[10] BOM-4` 依赖此文件);
  无专属 API URL 即拒非专属路由(`line_bridge.py:122-124,154-156`,X20 脚本策略层证据)。
- `tests/test_pl140_partial.py`(33 条)+ `tests/test_line_mechanism.py`(143 条,含
  emit_observability 现役路径,跑出 `CROSS_VALIDATE_OK` / `MEMBER_STATS` / `STATUS_SIGNED`)。
- `systemd/line-watchdog-heartbeat.{service,timer}` + `install.sh` —— X63 进程外心跳 watcher(待 ops 安装)。
- `BLOCKED_OWNER_ACCEPTANCE.md` + `probes/blocked_evidence.txt` —— X20/X51/X63/X72 平台/凭据/外部缺口与验收口径。

## 现役热路径接入 call site(本轮返工核心)

| 项 | 现役 call site(本目录文件) | 接入点 |
|---|---|---|
| X08 route 前重读 assignee | `line_watchdog.py` `P.route_precheck(...)` @ `execute_route_job` | route 执行前回拉比对 assignee(防 TOCTOU) |
| X11 schema drift 落证据文件 | `line_watchdog.py` `P.write_schema_drift_evidence(...)` @ `system_alerts` | 漂移检出即落 `schema_drift_<ts>.json` |
| X14 覆盖截断量化 | `line_watchdog.py` `P.coverage_estimate(...)` @ `system_alerts` | coverage_limit 告警显示预计漏扫数 |
| X24/X27 成员维度统计 | `line_watchdog.py` `P.member_headcount(...)` @ `emit_observability` | 状态板 `member_stats` |
| X38 阶段转换持久化 | `line_watchdog.py` `P.diff_stage_state/append_jsonl` @ `main` | 逐轮写 `stage_transitions.jsonl` |
| X53 跨脚本统一锁 | `line_watchdog.py` + `line_dispatch.py` `P.acquire_coord_lock(COORD_LOCK_F)` | route 与续派互斥同一锁文件 |
| X54 rate-limit 自动探测 | `line_watchdog.py` `P.rate_limited_signal(...)` @ `main`(+ `sh` 采样) | 限流信号自动降并发 |
| X67 三文件互校 | `line_watchdog.py` `P.cross_validate(st,status,hb)` @ `emit_observability` | 自报状态自相矛盾即告警留痕 |
| X71 状态板防篡改签名 | `line_watchdog.py` `P.sign_status(status,key)` @ `emit_observability` | 写盘前盖 HMAC sig |

## 可复现命令

```bash
cd squad/pl140

# 0) 现役热路径接入证据:E2E 离线跑看门狗,看新分支真触发(schema 证据文件/覆盖量化/签名/互校)
D=$(mktemp -d)
printf '%s' '[{"id":"11111111-1111-1111-1111-111111111111","identifier":"PL-T1","status":"in_progress","assignee_id":"0f0013ea-a65d-4ab8-b1b4-17e14da8ab52","assignee_type":"squad","updated_at":"2026-06-10T00:00:00Z","created_at":"2026-06-09T00:00:00Z"},{"id":"22222222-2222-2222-2222-222222222222","identifier":"PL-T2","status":"frozen_custom","assignee_id":"0f0013ea-a65d-4ab8-b1b4-17e14da8ab52","assignee_type":"squad","updated_at":"2026-06-10T00:00:00Z","created_at":"2026-06-09T00:00:00Z"}]' > "$D/issues.json"
printf '%s' '[{"id":"0f0013ea-a65d-4ab8-b1b4-17e14da8ab52","name":"线小队-T03","leader_id":"b70d9b47-4243-4c91-9393-8034fe413ede"}]' > "$D/squads.json"
WATCHDOG_DRY_RUN=1 WATCHDOG_NOW="2026-06-10T01:00:00Z" WATCHDOG_FIXTURE="$D/issues.json" \
  WATCHDOG_SQUADS_FIXTURE="$D/squads.json" WATCHDOG_ISSUE_LIMIT=2 WATCHDOG_SIGN_KEY="k" \
  WATCHDOG_STATE="$D/state.json" WATCHDOG_STATUS_F="$D/status.json" WATCHDOG_HEARTBEAT_F="$D/hb.json" \
  WATCHDOG_SCHEMA_DRIFT_DIR="$D/drift" WATCHDOG_STAGE_TRANS_F="$D/trans.jsonl" WATCHDOG_FALLBACK_F="$D/fb.log" \
  python3 line_watchdog.py --route --emit-observability 2>&1 | \
  grep -E "SCHEMA_DRIFT_EVIDENCE|预计漏扫|STATUS_SIGNED|CROSS_VALIDATE_OK|MEMBER_STATS"
# → schema_drift_<ts>.json 落盘;coverage 量化"预计漏扫 ≥1";status.json 带 sig;三文件互校 OK

# 1) 单测(33 条,连跑 3 次全绿)
python3 -m unittest tests.test_pl140_partial

# 1b) 全量回归(自包含:LEADER_AUDIT/ledger 均落临时目录,任意只读 checkout 下离线可复现)
bash tests/run_regression.sh   # → ==== 回归汇总:PASS=92 FAIL=0 ====  ALL GREEN

# 2) X32 端到端:PASS 之后又出现 failed run → 旧 PASS 失效 → BLOCK(exit 2)
D=$(mktemp -d)
printf '%s' '{"identifier":"PL-X32","status":"in_review","description":"x","metadata":{}}' > "$D/i.json"
printf '%s' '[{"author_type":"agent","created_at":"2026-06-10T00:00:00Z","content":"开发交付:已 push,typecheck 通过。"},{"author_type":"agent","created_at":"2026-06-10T00:05:00Z","content":"专属审计 VERDICT: PASS"},{"author_type":"agent","created_at":"2026-06-10T00:06:00Z","content":"【门禁 Qwen】VERDICT: PASS"}]' > "$D/c.json"
echo '[{"id":"r2","status":"failed","created_at":"2026-06-10T00:10:00Z"}]' > "$D/r.json"
DONE_GATE_ISSUE_FIXTURE="$D/i.json" DONE_GATE_COMMENTS_FIXTURE="$D/c.json" DONE_GATE_RUNS_FIXTURE="$D/r.json" \
  python3 line_done_gate.py PL-X32; echo "exit=$?"   # → ⛔ BLOCK / exit=2
# 把 r.json 换成 completed@00:04 → ✅ ALLOW / exit=0

# 3) X63 心跳 CLI:停摆 → exit 2 / 新鲜 → exit 0
python3 line_partial.py heartbeat-check <heartbeat.json> 10

# 4) X20/X51/X72 缺凭据探测(输出即证据)
bash probes/probe_blocked_credentials.sh
```
