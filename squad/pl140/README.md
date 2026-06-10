# PL-140 落地物(可审计)

看门狗(line-watchdog)PARTIAL 16 项复核补齐的真实运行代码 + 单测 + 探测证据。
逐项结论见 [`PL140_MATRIX.md`](./PL140_MATRIX.md)。

## 文件
- `line_partial.py` —— 13 项 PARTIAL 的「脚本可独立闭环」纯函数库。
- `line_done_gate.py` —— X32:收口门禁第 6 条 `evidence_fresh`(旧 PASS 新鲜度失效)。
- `line_evidence.py` —— X32 依赖(`evidence_is_stale` 等),随门禁一并入库以便复现。
- `tests/test_pl140_partial.py` —— 33 条单测。
- `systemd/line-watchdog-heartbeat.{service,timer}` + `install.sh` —— X63 进程外心跳 watcher。
- `probes/probe_blocked_credentials.sh` + `blocked_evidence.txt` —— X20/X51/X72 缺凭据证据。

## 可复现命令

```bash
cd squad/pl140

# 1) 单测(33 条,连跑 3 次全绿)
python3 -m unittest tests.test_pl140_partial

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
