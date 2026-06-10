# BLOCKED 项 · 平台/总管决策清单与验收口径(PL-136)

本文件把"看门狗未部署剩余项"里真正卡在**平台能力 / 总管决策**的 BLOCKED 项,逐条写到
**具体缺的平台子命令或规则 + 可验收口径**。脚本侧能做的检测已落地(给出 file:line 证据),
但"硬阻断 / 权威判定"需要平台或总管补能力。每条给:现役事实 → 缺的能力(owner)→ 验收口径
(交付后怎么判 PASS)。

> 现役 `squad-ops v2` 子命令(`sudo /usr/local/sbin/squad-ops`)只有:
> deploy-canvas / deploy-multica / restart / status / fetch / logs / db-ro / test-backend /
> screenshot / open-pr / repo-info。**无**下列治理类子命令。

---

## U40 ≡ X21 ≡ X59 — 平台硬禁"个人派单"

- **现役事实**:看门狗只做**事后检测**。`line_watchdog.py:1041` 对 `assignee_type=="agent"
  且非线主脑` 的 issue emit `personal_assignment` 告警(`line_watchdog.py:769`),由续派侧归队。
  平台层**不阻止**创建/改派到个人:`multica issue create --assignee-id <任意 agent>` 当前可成功。
- **缺的能力(owner=平台 / 总管)**:服务端校验规则——`issue create/update` 当 `--assignee-id`
  指向一个 agent 且该 agent 不是任一线小队的 leader(线主脑)时**直接拒绝**(返回非 0 + 明确错误);
  或一个可由管理员开启的 `squad-ops enforce-squad-assignment <on|off>` 开关把该规则推到平台。
- **验收口径(PASS 判据)**:
  1. `multica issue create --title t --assignee-id <非主脑 agent-id>` → **退出码非 0**,stderr 含
     "个人派单被拒/只能派给线小队"类信息。
  2. 派给线小队 id 或线主脑 agent 仍正常成功。
  3. 回归侧:保留 `personal_assignment` 检测作为**第二道**(平台规则 + 看门狗双保险),
     现有用例 `[个人派单归队]` 继续 PASS。

## X22 — 一个 issue 多 squad / 多 owner(跨队共担)

- **现役事实**:平台 issue 模型是**单 `assignee_id`**。跨队协作只能由看门狗
  `cross_squad_collab` 告警建模(`line_watchdog.py:1518` 一带),交线主脑口头协调,平台无共担归属。
- **缺的能力(owner=平台)**:issue 支持**多 assignee / co-owner**——
  `multica issue assignee add|remove <id> --assignee-id <...>`,且 `issue get --output json`
  返回 `assignees: [...]` 数组(而非单字段);看门狗据此按多归属判活/归队。
- **验收口径**:
  1. `multica issue assignee add <id> --assignee-id <squadB>` 后 `issue get` 的 `assignees`
     含 squadA、squadB 两条。
  2. 任一 owner 队推进都计入该 issue 进度,不再被单队视图漏判。
  3. 在此之前维持现状:`cross_squad_collab` 告警 = 唯一可用的跨队建模,不得当已闭环。

## X29 — 凭据主体必须匹配执行身份(跨 GitHub/Multica/GDrive)

- **现役事实**:`open-pr` / `deploy-*` 用的是**集团统一**部署密钥 / GitHub App,**不校验**
  "发起这次 PR/部署的 agent 执行身份"是否等于 commit author / 凭据主体。无身份-凭据绑定表。
- **缺的能力(owner=总管,身份治理决策)**:
  1. 总管先**决策**身份治理口径:每个 agent 执行身份 ↔ 允许使用的 GitHub/Multica/GDrive 主体的绑定表
     (放 `~/.config/squad/identity.map` 或平台侧)。
  2. 平台/squad-ops 在 `open-pr` / `deploy-*` 时按该表**校验** commit author / 操作主体与执行
     agent 绑定一致,不一致则拒绝(`squad-ops verify-identity <agent>` 子命令或内建校验)。
- **验收口径**:用 agentA 的执行身份发起一个 commit author=agentB 的 PR → **被拒**;一致则放行。
  绑定表缺失时,`squad-ops` 应报"身份绑定未配置"而非静默放行。

## X41 — ReleaseGate 读"typed 状态机结果"判定

- **现役事实**:发布门 `ab_gate.py validate`(`ab_gate.py:93`)现在校验 py_compile + 关键函数
  存在(`ab_gate.py:38`)+ policy(`ab_gate.py:68`),**不读** issue 的结构化阶段/状态结果。
  阶段判定靠 `line_states` 正则 + 评论回退(typed 字段缺失时降级)。
- **缺的能力(owner=平台)**:`multica issue get --output json` 返回**typed**阶段/裁决字段
  (如 `stage_state`、`gate_verdict`、`phase`),而非让脚本正则解析评论文本。
- **验收口径**:`issue get` 含 `stage_state`(枚举值)与 `gate_verdict`(pass/fail/none);
  `ab_gate.py validate` 能直接读该字段判门禁,无需评论正则。交付前:维持正则 + 评论回退(降级只读),
  不得把"靠评论解析"当权威收口。

## X42 — 发布门阻断 terminal-conflict(可降级本地接,权威判定依赖 X41)

- **现役事实**:`terminal_conflict`(done/cancelled 仍有 running run)检测在看门狗
  detect(`line_watchdog.py:1537`),但发布门 `ab_gate.validate` **不读**该结果阻断发布。
- **缺的能力 / 路径**:
  - **可本地降级接**(不依赖平台):让 `ab_gate.validate` 读 `watchdog_status.json` 的
    `red_lights`,含 `terminal_conflict` 即门禁 FAIL。此条**不必 BLOCKED**,可排进本地实现。
  - **权威判定**仍 BLOCKED on X41:终态-运行冲突的"真状态"需 typed 状态机字段才稳。
- **验收口径**:`watchdog_status.json.red_lights` 含 `terminal_conflict` 时
  `ab_gate.py validate` 返回非 0 且 VERDICT 注明被该红灯阻断;无该红灯不误阻。

## X69 — evidence ledger 进发布门(依赖 X47 每轮生产落账)

- **现役事实**:evidence ledger 代码已在(`line_evidence.py build_ledger/evidence_gate_decision`,
  看门狗 detect 落 `LEDGER_DIR/<ident>.json`),但 `ab_gate.validate` **不读** ledger 的
  `gate_decision` 做发布门校验。
- **缺的能力 / 路径**:
  - 依赖 **X47**:ledger 需每轮生产稳定生成(看门狗恢复后产出)才有可读数据。
  - 之后**可本地接**:`ab_gate.validate` 读目标 issue 的 ledger,`gate_decision.status ∈
    {fail, evidence_missing}` 即门禁 FAIL。owner 非平台,但前置 X47 落地 + 看门狗恢复采证。
- **验收口径**:给一个 `gate_decision.status=fail` 的 ledger,`ab_gate.py validate <ident>`
  返回非 0;`status=pass` 放行;ledger 缺失报"无证据账本"而非默认放行。

---

## X20 — 平台硬禁"普通 agent 冒充专属(Qwen/DeepSeek)身份"

- **现役事实**:脚本侧策略层已落地——`line_bridge.py:122-124,154-156` 无专属 API URL 即拒非专属
  路由;`line_evidence.py:348 evidence_trust` / `361 passes_trust_policy` trust policy 现役在
  `line_watchdog.py` 采信。但这只是**脚本自律**:它不能阻止一个普通 agent 在平台侧自称专属身份。
- **缺的能力(owner=平台)**:服务端身份校验——当某 run/agent 自报为 Qwen/DeepSeek 专属岗,
  平台核对其真实执行身份,不匹配即**硬拒**(返回非 0 + 明确错误),而非靠脚本事后信任策略。
- **验收口径(PASS 判据)**:用普通 agent 执行身份发起一次"专属门禁/专属路由"动作 → 平台直接拒绝,
  留下可取证的拒绝记录(`squad-ops fetch`/`db-ro` 可查);脚本 trust policy 作为第二道保留。
- **本轮取证**:仅能证明脚本策略层成立;"平台硬拒"无现役/平台证据 → 该子项保持 BLOCKED(平台)。

## X51 — 现役证据链接入"真 GitHub API 校验"

- **现役事实**:`line_evidence.py:227 verify_github_refs(verify_network=True)` 能力已具备,但现役
  `audit_completed_evidence()` 只做**格式自检**,从不传 `verify_network=True`——因为无可用 PAT。
- **本轮真实探测(credential gap 实证)**:
  - `curl -s -o /dev/null -w '%{http_code}' https://api.github.com/user` → **HTTP 401**(无凭据)。
  - `/etc/squad/github_pat` → **Permission denied**(root-only,fleet 取不到);`~/.config/squad` 无 token 文件。
  - `repo.env` 自注:`# PR 需要 gh PAT(待配)`。
- **缺的能力(owner=总管/平台)**:给现役证据链一个可用的 GitHub 只读 token(或 `squad-ops verify-github-ref
  <owner/repo> <ref>` 子命令走 GitHub App),使 `verify_network=True` 能在 hot path 真打 GitHub API。
- **验收口径**:配置后,对一个真实存在/不存在的 commit/PR ref,现役采证分别返回校验通过/失败,
  且失败时给出 GitHub API 的 HTTP 状态;凭据缺失时报"GitHub 凭据未配置"而非静默按格式通过。

## X63 — 心跳外部 watcher 的"已安装并启用"证据

- **现役事实**:外部 watcher 逻辑 + systemd unit 文件已就绪——`line_partial.py:389 heartbeat_alert`
  + `line_partial.py:431 _heartbeat_check_main`(CLI `line_partial.py heartbeat-check`,退出码 2 可被
  timer/告警链感知);unit 文件 `systemd/line-watchdog-heartbeat.{service,timer}` 在仓库内。
- **本轮真实探测(install gap 实证)**:
  - `systemctl is-enabled line-watchdog-heartbeat.timer` → **No such file or directory**(未安装)。
  - `ls /etc/systemd/system/line-watchdog-heartbeat*` → **不存在**。
  - `squad-ops v2` 子命令集**无** systemd 安装/enable 能力(只有 deploy/restart/status/...)。
- **缺的能力(owner=总管/ops)**:把 `systemd/line-watchdog-heartbeat.*` `cp` 到
  `/etc/systemd/system/` 并 `systemctl daemon-reload && enable --now`(需 root),或加
  `squad-ops install-timer <unit>` 子命令。
- **验收口径**:`systemctl is-active line-watchdog-heartbeat.timer` → `active`;人为停看门狗主进程,
  心跳超阈值后该 timer 跑出 `WATCHDOG_SILENT` 并落 fallback,证明"看门狗死了也有进程外告警"。

## X72 — P0 外部通知 + 人工 ACK 闭环

- **现役事实**:ACK 账本纯函数已就绪——`line_partial.py:406 p0_ack_entry` / `416 pending_p0_acks`
  (P0 critical 未 ack 即"未闭环 P0")。但**外部通知通道**(把 P0 推到人能收到的地方)无现役接入。
- **本轮真实探测(channel gap 实证)**:环境内无任何 `slack/webhook/smtp/notify` 通道配置。
- **缺的能力(owner=平台/总管)**:一个外部通知出口(webhook/邮件/IM)+ 人工 ACK 回写入口
  (如 `squad-ops notify-p0 <body>` 与 ACK 落账),使 P0 能送达人并回收确认。
- **验收口径**:制造一条 P0 critical 告警 → 外部通道收到通知;人工 ACK 后 `pending_p0_acks`
  不再含该条;未 ACK 的 P0 在状态板/账本持续标"未闭环"。

---

## PL-140 X 接入小结(本轮返工)

X08/X11/X14/X24/X27/X38/X53/X54/X67/X71 已从 `line_partial.py` 旁路库**接入现役热路径**
(`line_watchdog.py` / `line_dispatch.py`,call site 见结果评论);X20/X51/X63/X72 经上述真实探测
确认是平台/凭据/外部通道/ops 安装缺口,非脚本可闭环,保持 BLOCKED 并由本清单 + 承接子单承接,
PL-140 在其闭环前不放行。

---

## 小结(派工建议)

| 项 | owner | 缺的具体能力 | 是否纯平台阻塞 |
|---|---|---|---|
| U40/X21/X59 | 平台/总管 | 服务端拒个人派单规则 / `enforce-squad-assignment` 开关 | 是(检测已双保险) |
| X22 | 平台 | issue 多 assignee 模型 + `assignee add/remove` | 是 |
| X29 | 总管 | 身份-凭据绑定表(决策)+ `verify-identity` 校验 | 是(先决策后落地) |
| X41 | 平台 | issue typed 阶段/裁决字段 | 是 |
| X42 | 平台(权威)/本地(降级) | ab_gate 读 status red_lights → 可先本地降级接 | **否,可本地降级** |
| X69 | 本地(前置 X47) | ab_gate 读 ledger gate_decision | **否,前置 X47 + 看门狗采证** |

**结论**:U40/X21/X59、X22、X29、X41 是真平台/总管阻塞,需总管补 `squad-ops` 子命令或平台规则;
X42、X69 不是纯平台阻塞,可在本地按上述验收口径接(X42 读 status red_lights、X69 读 ledger),
前置是 X47 每轮生产落账 + 撤 `watchdog.disabled` 后采证。
