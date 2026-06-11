# C01 生产线自检
C01 全链路真测通过 — 日期 2026-06-10

## PL-145 看门狗路由「C系异常→回本队」自检 — 2026-06-11 (C02-W)
- 实现：`issueOwningSquadID` 作为看门狗重路由目标的唯一真源，所有 squad-leader 唤醒入口（comment/assign/enqueue）统一经它解析，目标恒为 issue 自身归属 squad，结构上无法落到 T01/T02/T03。
- 回归：`go test ./internal/handler/ -run 'TestIssueOwningSquadID_RoutesBackToOwningSquadNeverCrossTeam|TestWatchdogReroute_CSeriesAnomalyReturnsToOwningSquad'` → PASS。
- 全包：`go test ./internal/handler/` → ok。
