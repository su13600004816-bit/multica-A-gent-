#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""U65:Agent-AB ReleaseGate —— 常驻进入 cron/runtime 的释放门。

旧态:释放门只在一次性深挖里手动跑过,部署/收口/cron 都不再校验,后续变更可绕过门直接上线。
本脚本把校验做成可常驻调用的 `ab_gate.py validate`:每次 cron 启动看门狗前先跑一遍,
校验运行时不变量仍成立(核心模块可编译、关键 U-修复函数在位、只派小队不派个人、契约策略未被篡改)。
门 FAIL 时由 run_watchdog.sh 降级为只读巡查(不 route/assign/rerun),避免带病续派。

用法:
  python3 ab_gate.py validate            # 退出码 0=PASS,非0=FAIL;打印 VERDICT
  python3 ab_gate.py validate --json     # 机器可读
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE_MODULES = ["line_watchdog.py", "line_states.py", "line_evidence.py", "line_observe.py"]
CONTRACT = os.path.join(ROOT, "watchdog_deepdig_ab_contract.json")

# 关键 U-修复必须在运行时在位(被删/被回退即视为释放门破)
REQUIRED_FUNCS = {
    "line_watchdog": [
        "audit_issue_evidence",      # U30/U31/U32 审计热路径
        "supplement_missing_parents",  # U03
        "clock_skew_alerts",         # U08
        "system_health_alerts",      # U15/U59/U60/U61/U68/U69/U70/U71
        "compute_stage_states",      # U22
        "detect", "main",
    ],
    "line_states": [
        "permission_drift_alert", "resource_pressure_alert",
        "memory_cleanup_due", "cli_version_drift_alert",
        "proxy_health_alert", "image_oversize_alert",
    ],
    "line_evidence": ["build_ledger", "build_cycle_ledger", "write_cycle_ledger"],
    "line_observe": ["watchdog_disabled", "canary_allows"],
}


def _py_compile():
    violations = []
    for m in CORE_MODULES:
        p = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(ROOT, m)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            violations.append("py_compile 失败 %s:%s" % (m, (p.stderr or "")[:160]))
    return violations


def _funcs_present():
    violations = []
    sys.path.insert(0, ROOT)
    for mod_name, funcs in REQUIRED_FUNCS.items():
        try:
            mod = __import__(mod_name)
        except Exception as e:
            violations.append("模块导入失败 %s:%s" % (mod_name, str(e)[:160]))
            continue
        for fn in funcs:
            if not hasattr(mod, fn):
                violations.append("关键函数缺失 %s.%s(U-修复被回退?)" % (mod_name, fn))
    return violations


def _policy_ok():
    """契约策略不变量:只派小队不派个人、读取失败不输出健康态、require_policy_ok。"""
    violations = []
    try:
        with open(CONTRACT, encoding="utf-8") as f:
            c = json.load(f)
    except Exception as e:
        return ["释放门契约不可读 %s:%s" % (CONTRACT, str(e)[:120])]
    if not c.get("action_space", {}).get("require_policy_ok"):
        violations.append("契约 require_policy_ok 非真(策略门被关闭)")
    blocked = set(c.get("action_space", {}).get("blocked_tools", []))
    for must in ("personal_agent_dispatch", "self_certify_done"):
        if must not in blocked:
            violations.append("契约未阻止 %s(个人派工/自证完成应被禁止)" % must)
    # 运行时校验:KIND_META 里没有任何 kind 路由到 'personal'
    try:
        sys.path.insert(0, ROOT)
        import line_watchdog as W
        if any(meta[0] == "personal" for meta in W.KIND_META.values()):
            violations.append("KIND_META 出现 personal 路由(违反只派小队规则)")
    except Exception as e:
        violations.append("无法核验 KIND_META 路由:%s" % str(e)[:120])
    return violations


def validate(as_json=False):
    violations = []
    violations += _py_compile()
    violations += _funcs_present()
    violations += _policy_ok()
    passed = not violations
    if as_json:
        print(json.dumps({"verdict": "PASS" if passed else "FAIL",
                          "violations": violations}, ensure_ascii=False))
    else:
        if passed:
            print("AB_GATE VERDICT: PASS —— 释放门通过(核心可编译/关键修复在位/策略未篡改)")
        else:
            print("AB_GATE VERDICT: FAIL —— 释放门未通过,共 %d 项:" % len(violations))
            for v in violations:
                print("  - %s" % v)
    return 0 if passed else 1


def main(argv):
    if len(argv) >= 2 and argv[1] == "validate":
        return validate(as_json=("--json" in argv[2:]))
    print("用法: ab_gate.py validate [--json]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
