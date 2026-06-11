# 看门狗停用 / 恢复口径(PL-137)

线看门狗 `run_watchdog.sh`(cron 每 2 分钟)与 `line_watchdog.py` 共用同一套停用判定
`line_observe.watchdog_disabled`。停用是**临时熔断**,不是永久关闭;恢复条件不被吞掉。

## 1. 停用的三种写法(都等价于"只读巡查")

停用态 = **只读**:不贴台 PL-94、不 route/assign/rerun、不写收口证据 metadata;
**仍刷新** `watchdog_heartbeat.json` / `watchdog_status.json`(打 `disabled:true` 签名),
所以监控看得见"停用但存活"。

1. 环境变量:`WATCHDOG_DISABLED=1`(取消该变量即恢复)。
2. 标志文件 `watchdog.disabled`,**手动停用**(无到期):持续停用到删除该文件。
3. 标志文件 `watchdog.disabled`,**临时停用**(自动恢复):文件里写一行
   - `until: 2026-06-10T18:00:00Z`(ISO8601 或 epoch 秒),到点本轮自动恢复;或
   - `ttl_min: 120`(相对文件 mtime 的分钟数),到点自动恢复。
   可选 `reason: <原因>` 行进状态板/日志便于审计。`#` 开头为注释。

到期后 cron 与 python 都会判 `disabled=False`、自动恢复正常巡查/续派;过期的标志文件
留在盘上不影响运行,运维择机删除即可。

## 2. 删除 `watchdog.disabled`(手动恢复)前必须满足的条件

1. 去重/路由/状态协议修复已在当前 `line_watchdog.py` 生效(本任务 PL-137 改动已上线)。
2. 连续两次 `--post` 干跑(fixture + `WATCHDOG_DRY_RUN=1`)去重通过:首轮 `POSTED`、次轮 `POST_SKIP`。
   ```bash
   cd /home/fleet/line-config
   ST=$(mktemp)
   WATCHDOG_DRY_RUN=1 WATCHDOG_NOW=2026-06-09T04:28:00Z \
     WATCHDOG_FIXTURE=tests/pl89_issues.json WATCHDOG_RUNS_FIXTURE=tests/pl89_runs.json \
     WATCHDOG_COMMENTS_FIXTURE=tests/pl89_comments.json WATCHDOG_STATE=$ST \
     python3 line_watchdog.py --post 2>&1 | grep -E 'POSTED|POST_SKIP'   # 期望 POSTED
   WATCHDOG_DRY_RUN=1 WATCHDOG_NOW=2026-06-09T04:28:00Z \
     WATCHDOG_FIXTURE=tests/pl89_issues.json WATCHDOG_RUNS_FIXTURE=tests/pl89_runs.json \
     WATCHDOG_COMMENTS_FIXTURE=tests/pl89_comments.json WATCHDOG_STATE=$ST \
     python3 line_watchdog.py --post 2>&1 | grep -E 'POSTED|POST_SKIP'   # 期望 POST_SKIP
   ```
3. `bash tests/run_regression.sh` 全绿。

## 3. 删除后如何验证不刷屏

```bash
rm /home/fleet/line-config/watchdog.disabled          # 恢复
# 观察连续两个 cron 周期(约 4-5 分钟)的日志:
tail -n 50 /home/fleet/line-config/watchdog-cron.log
```
预期:**首个**恢复周期对当前真实异常贴台一次(这是正常的当前态告警,不是刷屏);
其后每周期靠 `alert_sigs` 去重 —— 同一异常不再重复贴台,日志出现 `POST_SKIP(无新增/变化告警)`。
若同一签名连续多周期重复贴台,即为刷屏回归,重新置 `watchdog.disabled` 并排查 `alert_sigs` 逻辑。

恢复后 `watchdog_status.json` 的 `disabled` 字段消失、`overall` 回到 green/yellow/red;
停用期间该字段为 `true`、`overall=disabled`。

## 4. 协议红线(看门狗续派必须遵守,见 KIND_META)

- `failed` / `zombie` -> 危机处理(crisis);`cancelled`(result 空)/ `evidence_missing` /
  `blocked_stale` / 空转 `idle` -> 线主脑(brain)。
- `done` / `cancelled` 已收口任务**不得**被重开:done_real 抑制 + terminal_conflict 只告警不重派。
- `blocked` 是真实状态:超时只提醒线主脑解阻;阻塞解除回 `in_progress`,**不得**用 `cancelled` 当返工/中转。
- 扫描不完整(partial-scan:覆盖截断 / 单轮 deadline 命中)本轮只告警、阻断全部 route/assign/rerun。
