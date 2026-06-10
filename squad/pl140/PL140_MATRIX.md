# PL-140 — X PARTIAL 16 项复核补齐 · 逐项结论矩阵

父任务 PL-136。范围:X01–X72 中仍 PARTIAL 的 16 个叶子 ID。
本文件把每一项落到**可审计仓库内的代码路径 / 函数 / 测试名**,并诚实区分三档状态:

- ✅ **端到端补齐**:已 wiring + 有测试/真跑证据,可复现。
- 🟡 **库+单测就绪,主循环 wiring 未完成**:纯函数与单测在仓库内可跑,但接入活体
  `line_watchdog.py` 主循环的 1~2 行 wiring **未完成**——按审计要求,这类**不算"补齐"**,
  明确标为「未 wiring,依赖 PL-139 收口后接入」。`line_watchdog.py` 复核期间正被 PL-139
  并发改写,本轮不在该文件落 wiring 以免互相覆盖。
- ⛔ **BLOCKED**:缺平台凭据/能力,脚本不可独力闭环,附真实探测证据
  (`probes/blocked_evidence.txt`,可由 `probes/probe_blocked_credentials.sh` 复现)。

所有代码路径均相对本目录 `squad/pl140/`。测试运行:
`python3 -m unittest tests.test_pl140_partial`(33 条,连跑 3 次全绿)。

| 编号 | 状态 | 代码路径 · 函数 | 测试 / 证据 | 已覆盖 / 缺口 |
|---|---|---|---|---|
| X08 | 🟡 未 wiring | `line_partial.py` · `route_precheck` / `assignee_unchanged` | `tests/test_pl140_partial.py::X08AssigneeToctou`(4 例) | route 前重读 assignee,被改→跳过防误派(TOCTOU)。缺:`line_watchdog.execute_route_job` 1 行接入 |
| X11 | 🟡 未 wiring | `line_partial.py` · `schema_drift_evidence` / `write_schema_drift_evidence` | `::X11SchemaDrift`(3 例) | 漂移落独立 `schema_drift_<ts>.json` 永久留底。缺:`detect()` 段接入 |
| X14 | 🟡 未 wiring | `line_partial.py` · `coverage_estimate` | `::X14Coverage`(3 例) | 截断时量化预计漏扫(有 total_hint→精确,无→保守下界≥1)。缺:接 count 二次探测 + observe 接入 |
| X20 | ⛔ BLOCKED | `line_partial.py` · `classify_source`(身份分级,已有) | `probes/blocked_evidence.txt`(X20 段) | 证据绑「专属 API 凭据主体」需平台身份系统;环境无 per-agent 凭据/mTLS,multica 未暴露 identity 子命令 |
| X24 | 🟡 未 wiring | `line_partial.py` · `member_headcount` | `::X2427MemberHeadcount` | 按成员维度统计每线人数/工作/闲置/卡死。缺:watchdog status 接入 |
| X27 | 🟡 未 wiring | `line_partial.py` · `member_headcount`(同 X24) | `::X2427MemberHeadcount` | 同 X24,产出每线岗位人数。缺:status 接入 |
| X32 | ✅ 端到端补齐 | `line_done_gate.py`(`evidence_fresh` 第 6 条门禁)→ `line_evidence.py::evidence_is_stale` | `::X32DoneGateFreshness`(2 例)+ CLI 子进程双向复现(见 README) | 最后一次 PASS 之后又出现 failed/cancelled run → 旧 PASS 失效 → 收口 BLOCK(exit 2);无更新→ALLOW(exit 0);无 run 数据退化为原五条门禁 |
| X38 | 🟡 未 wiring | `line_partial.py` · `diff_stage_state` / `stage_transition_entry` / `append_jsonl` | `::X38StageTransition`(3 例) | 阶段转换带 from/to/event/source 逐轮持久化(滚动保留)。缺:watchdog emit 接入 |
| X47 | ✅ 端到端补齐 | `line_watchdog.py::emit_observability`(`--emit-observability`)→ `line_evidence.py::build_cycle_ledger` / `write_cycle_ledger` → `cycles.jsonl` | 仓库内可复现:README「3) X47 端到端」一条命令离线跑现役 `line_watchdog.py --emit-observability`,在 `LINE_EVIDENCE_LEDGER_DIR` 指定目录落出 `cycles.jsonl`(每轮一行) | wiring 在现役热路径脚本 `line_watchdog.py`(本目录)内、非旁路;落盘目录由 `LINE_EVIDENCE_LEDGER_DIR` 决定,离线指向临时目录不污染生产。PL-126「未接 cron」判定已过时——周期账本逐轮落盘,判定纠偏为 PASS |
| X51 | ⛔ BLOCKED | `line_partial.py` · `parse_github_refs` / `verify_github_refs(verify_network=True)` | `probes/blocked_evidence.txt`(X51 段) | gh v2.93 就绪但未登录任何 host,无 PAT(`repo.env`:待配)。配上即可真网络校验 |
| X53 | 🟡 未 wiring | `line_partial.py` · `acquire_coord_lock` / `release_coord_lock` | `::X53CoordLock`(真子进程互斥) | 跨脚本统一事务锁(flock)。缺:watchdog **与** line_dispatch **同时**接同一锁才生效,只接一方无意义 |
| X54 | 🟡 未 wiring | `line_partial.py` · `detect_rate_limit` / `rate_limited_signal` | `::X54RateLimit`(2 例) | 从 CLI 输出自动探测 429/限流,喂 `adaptive_workers`。缺:watchdog `fw=` 1 行接入 |
| X63 | ✅ 端到端补齐 | `line_partial.py heartbeat-check` CLI + `systemd/line-watchdog-heartbeat.{service,timer}`,已纳入 `systemd/install.sh` 复制+`enable --now` 路径 | `::X63HeartbeatAlert`(3 例)+ CLI 真跑 stale→exit 2 / fresh→exit 0(见 README) | 进程外 watcher:看门狗自身死掉它仍跑,停摆→落 fallback 告警 + exit 2。`install.sh` 现同时 `cp` 两单元并 `systemctl enable --now line-watchdog-heartbeat.timer`,运维一条命令即装载 |
| X67 | 🟡 未 wiring | `line_partial.py` · `cross_validate` | `::X67CrossValidate`(4 例) | watchdog_state / status / heartbeat 三源互校验(时间差/full-vs-ok 矛盾/缺失)。缺:watchdog 接入并把不一致点红 |
| X71 | 🟡 未 wiring | `line_partial.py` · `sign_status` / `verify_payload_signature` / `load_sign_key` | `::X71TamperProof`(4 例) | 状态来源 HMAC 防篡改(改红→绿/换 key 均被拒)。缺:配 `WATCHDOG_SIGN_KEY` + emit 签名 1 行接入 |
| X72 | ⛔ 外发 BLOCKED(脚本侧已补) | `line_partial.py` · `p0_ack_entry` / `pending_p0_acks` | `::X72P0Ack`(3 例);外发证据 `probes/blocked_evidence.txt`(X72 段) | P0 人工 ack 闭环账本(脚本侧)已补齐 + 单测。外部通知(Slack/SMS)需平台 webhook/凭据,环境无 → 外发 BLOCKED |

## 状态小结

- ✅ 端到端补齐(3 项):**X32 / X47 / X63**。
- 🟡 库+单测就绪、主循环 wiring 未完成(10 项):X08 / X11 / X14 / X24 / X27 / X38 / X53 / X54 / X67 / X71
  —— 依赖 PL-139 收口 `line_watchdog.py` 后由收口角色接入(每个函数已注明接入点)。**不算"补齐"**。
- ⛔ BLOCKED(3 项):X20 / X51 / X72(X72 脚本侧账本已补,仅外发通道 BLOCKED)—— 均附真实探测证据。

## 诚实说明

1. 本批代码原先只落在运维目录 `/home/fleet/line-config/`(非 git),导致审计 checkout
   绑定仓库后查无落地物。本轮已把真实运行的 `line_partial.py` / `line_done_gate.py` /
   `line_evidence.py`(X32 依赖)/ `tests/test_pl140_partial.py` / systemd 单元搬入本目录,
   审计可在仓库内直接复现。
2. 33 条单测在**仓库内**连跑 3 次全绿(非运维目录)。X32 端到端、heartbeat CLI 均给可复现命令(见 README)。
3. 全程未动生产 `watchdog.disabled` 熔断标志;X32 复现用 fixture 注入(`DONE_GATE_*_FIXTURE`),不触真网络/不改生产状态。
