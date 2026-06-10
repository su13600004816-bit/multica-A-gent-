#!/usr/bin/env python3
# DONE_REAL 收口门禁(BOM-6,纯脚本):在把 issue 置 done / 跑 line_reset 前强校验五个必要条件。
# 任一不满足 -> 退出码 2(BLOCK),禁止收口;全满足 -> 退出码 0(ALLOW)。
#
# DONE_REAL 五条(全 AND):
#   1. dev_delivered     开发交付完成(有 PR / push / build·typecheck 通过 的交付证据)
#   2. audit_pass        专属审计最新结论 = PASS(且其后无 FAIL)
#   3. gate_pass         真门禁 PASS —— 接受 line_bridge.py gate 的【门禁 Qwen】回写,
#                        或 T02 本地千问 API(http://127.0.0.1:18181,qwen-plus)回写;
#                        线主脑/开发自己口头"门禁已 PASS"不算;占位岗误触发判为门禁失败。
#   4. page_evidence     页面/视觉任务必须有 真实 URL + 截图(附件)且视觉 PASS;
#                        build/typecheck/代码核查只够"技术审计 PASS",不够 DONE_REAL。
#   5. no_user_counter   done 之后无用户/成员贴出反例截图(用户截图优先级最高,触发返工)。
#
# 用法:
#   line_done_gate.py <issue_id>                 # 真查 multica,打印判定,退出码 0/2
#   line_done_gate.py <issue_id> --json          # 机器可读
# 测试钩子:
#   DONE_GATE_ISSUE_FIXTURE=<json>      替代 issue get(含 title/description/status/metadata)
#   DONE_GATE_COMMENTS_FIXTURE=<json>   替代 comment list(扁平时间线,新->旧或旧->新均可,按 created_at 排序)
import os, sys, json, subprocess, argparse, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import line_evidence as _E   # X32:旧 PASS 新鲜度失效(evidence_is_stale)
except Exception:               # pragma: no cover - 离线兜底
    _E = None

# 真门禁/审计的 VERDICT 只认【结论位】:行首,或紧跟句末标点(。．.!?！?)之后 ——
# 即它是该评论自己的结论声明,而不是正文里复述/引用上一轮 VERDICT(PL-127 误 ALLOW 根因)。
# 反例:"上一轮门禁回写为 VERDICT: PASS,但截图缺失" 这种被字句包裹的引用不算结论,必须排除;
# 正例:"专属审计:…复核齐全。VERDICT: PASS" 与独占一行的 "VERDICT: PASS" 都算结论。
_VERDICT_RE = re.compile(r'(?:^|(?<=[。．.!?！？]))\s*`?\s*VERDICT:\s*(PASS|FAIL)\b', re.IGNORECASE)

def standalone_verdict(text):
    """返回评论里【结论位】最后一个 VERDICT(PASS/FAIL),无则 None。
    结论位 = 独占一行(行首)或紧跟句末标点之后;被其它字句包裹引用的 VERDICT 不算。行首可有反引号。"""
    v = None
    for line in (text or "").splitlines():
        for m in _VERDICT_RE.finditer(line.strip()):
            v = m.group(1).upper()
    return v

# 页面/视觉任务关键词:命中则要求 page_evidence(真实 URL + 截图)。
PAGE_KEYWORDS = ("页面", "画布", "canvas", "视觉", "截图", "UI", "前端", "入口", "按钮", "详情页", "面板")
# 真门禁回写:兼容旧 line_bridge.py gate header 与 T02 本地千问 API 回写。
REAL_GATE_HEADER = "【门禁 Qwen"
LOCAL_QWEN_ENDPOINT = "127.0.0.1:18181"
LOCAL_QWEN_MARKERS = ("qwen-plus", "千问本地 API", "本地千问 API", "千问门禁")
# 占位岗误触发标记 -> 门禁无效。
MISROUTE_MARKERS = ("不能冒充千问", "占位岗", "误触发", "不能直接做千问", "不能执行门禁")
# 开发交付证据标记。
DELIVER_MARKERS = ("/pull/", "PR #", "已合并", "merged", "已 push", "已push", "typecheck", "build 通过", "build 0", "apply --check", "numstat")

def jget(*a):
    try:
        r = subprocess.run(list(a), capture_output=True, text=True, timeout=90)
        return json.loads(r.stdout)
    except Exception:
        return None

def get_issue(iid):
    fx = os.environ.get("DONE_GATE_ISSUE_FIXTURE")
    if fx:
        try: return json.load(open(fx))
        except Exception: return {}
    return jget("multica", "issue", "get", iid, "--output", "json") or {}

def get_comments(iid):
    fx = os.environ.get("DONE_GATE_COMMENTS_FIXTURE")
    if fx:
        try: d = json.load(open(fx))
        except Exception: d = []
    else:
        d = jget("multica", "issue", "comment", "list", iid, "--output", "json")
    cs = d if isinstance(d, list) else (d.get("comments", d.get("items", [])) if d else [])
    # 扁平化 thread replies,统一按 created_at 升序
    flat = []
    for c in cs:
        flat.append(c)
        for rep in (c.get("replies") or []):
            flat.append(rep)
    flat.sort(key=lambda x: x.get("created_at") or "")
    return flat

def get_runs(iid):
    """X32:拉 run 列表(带新鲜度判定用)。离线/无数据返回 []，行为退化为原五条门禁。"""
    fx = os.environ.get("DONE_GATE_RUNS_FIXTURE")
    if fx:
        try: d = json.load(open(fx))
        except Exception: d = []
    else:
        d = jget("multica", "issue", "runs", iid, "--output", "json")
    return d if isinstance(d, list) else (d.get("runs", d.get("items", [])) if d else [])

def ctext(c):
    return c.get("content") or c.get("body") or ""

def has_image(c):
    for at in (c.get("attachments") or []):
        if str(at.get("content_type") or "").startswith("image/"):
            return True
    return False

def is_page_task(issue):
    md = issue.get("metadata") or {}
    if str(md.get("page_task")).lower() in ("true", "1", "yes"):
        return True
    blob = (issue.get("title") or "") + " " + (issue.get("description") or "")
    return any(k in blob for k in PAGE_KEYWORDS)

def is_real_gate_comment(text):
    t = text or ""
    if REAL_GATE_HEADER in t:
        return True
    if LOCAL_QWEN_ENDPOINT in t and any(m in t for m in LOCAL_QWEN_MARKERS):
        return True
    return False

def evaluate(issue, comments, runs=None):
    reasons = {}
    page = is_page_task(issue)

    # 1. dev_delivered:有交付证据(PR/push/build/apply-check)或任一【独占一行】审计/门禁 VERDICT。
    dev = any(any(m in ctext(c) for m in DELIVER_MARKERS) or standalone_verdict(ctext(c)) for c in comments)
    reasons["dev_delivered"] = (dev, "有开发交付证据(PR/push/build)" if dev else "缺开发交付证据(PR/push/build/typecheck)")

    # 2. audit_pass:最后一个出现的审计 VERDICT(排除门禁回写),且不是 FAIL。只认独占一行的 VERDICT。
    audit_last = None
    for c in comments:
        t = ctext(c)
        if is_real_gate_comment(t):
            continue  # 门禁的 verdict 单独算
        v = standalone_verdict(t)
        if v:
            audit_last = v
    ap = audit_last == "PASS"
    reasons["audit_pass"] = (ap, "专属审计最新 VERDICT: PASS" if ap else "专属审计未 PASS(最新=%s)" % (audit_last or "无"))

    # 3. gate_pass:必须来自真门禁回写头;占位岗误触发 -> 失败
    gate_last = None
    misrouted = False
    for c in comments:
        t = ctext(c)
        if any(m in t for m in MISROUTE_MARKERS):
            misrouted = True
        if is_real_gate_comment(t):
            v = standalone_verdict(t)  # 只认独占一行的 VERDICT,正文引用不算
            if v:
                gate_last = v
    gp = (gate_last == "PASS")
    if gate_last is None:
        gmsg = "无真门禁回写(line_bridge.py gate 的【门禁 Qwen】或本地千问API 127.0.0.1:18181);口头/自证'门禁已 PASS'不算"
    elif misrouted and gate_last != "PASS":
        gmsg = "门禁被占位岗误触发,判为门禁失败,请走真实千问门禁入口"
    else:
        gmsg = "真门禁 VERDICT: PASS" if gp else "真门禁最新 = %s" % gate_last
    reasons["gate_pass"] = (gp, gmsg)

    # 4. page_evidence(仅页面任务):真实 URL + 截图附件
    if page:
        has_url = any(("http://" in ctext(c)) or ("https://" in ctext(c)) for c in comments)
        has_shot = any(has_image(c) for c in comments)
        pe = has_url and has_shot
        reasons["page_evidence"] = (pe, "页面任务:有真实 URL + 截图" if pe else
                                    "页面任务缺视觉证据(URL=%s,截图=%s);build/代码核查不够 DONE_REAL" % (has_url, has_shot))
    else:
        reasons["page_evidence"] = (True, "非页面任务,跳过视觉证据")

    # 5. no_user_counter:成员(人)在最后一条门禁/审计 PASS 之后贴出的反例(图或明确反对)
    #    取最后一次 PASS 信号的时间点,其后若有 member 贴图/写"没看到/缺/反例/不对",判为有用户反例。
    last_pass_ts = ""
    for c in comments:
        if standalone_verdict(ctext(c)) == "PASS":
            last_pass_ts = max(last_pass_ts, c.get("created_at") or "")
    counter = None
    for c in comments:
        if c.get("author_type") == "member" and (c.get("created_at") or "") >= last_pass_ts and last_pass_ts:
            t = ctext(c)
            if has_image(c) or any(k in t for k in ("没看到", "看不到", "缺", "反例", "不对", "没有", "FAIL", "失败")):
                counter = c.get("created_at")
                break
    nc = counter is None
    reasons["no_user_counter"] = (nc, "无用户反例截图" if nc else "用户在 PASS 后贴出反例(%s),必须撤回 done 并返工" % counter)

    # 6. evidence_fresh(X32):最后一次 PASS 之后若又出现更新的 failed/cancelled run,
    #    旧 PASS 失效 —— gate FAIL / 用户反例后不得再凭旧 PASS 收口。仅在有 run 数据时强制,
    #    无 run 数据(离线/拉取失败)退化为原五条门禁,不误拦。
    if runs and _E is not None and last_pass_ts:
        stale, why = _E.evidence_is_stale(None, last_pass_ts, runs)
        reasons["evidence_fresh"] = (not stale,
                                     "无更新 run 覆盖,PASS 仍代表当前代码" if not stale else why)
    else:
        reasons["evidence_fresh"] = (True, "无 run 数据,跳过新鲜度校验(原五条门禁)")

    # 7. no_terminal_conflict(X42):发布门必须阻断 terminal-conflict 状态 —— 收口前若该 issue 仍有
    #    running run,置 done 即制造 done×running 矛盾态(看门狗 terminal_conflict 危机告警)。复用与
    #    line_watchdog 同一信号(running run),把检测前移到收口门,从源头不让矛盾态进 done。
    #    仅在有 run 数据时强制;离线/无 run 退化为放行,不误拦。
    running = [r for r in (runs or []) if str(r.get("status")) == "running"]
    if runs:
        nt = not running
        reasons["no_terminal_conflict"] = (
            nt, "无 running run,收口不产生 terminal-conflict" if nt else
            "仍有 %d 个 running run(%s),置 done 将制造 terminal-conflict,先停/等 run 收尾再收口" % (
                len(running), ",".join((r.get("id") or "")[:8] for r in running[:3])))
    else:
        reasons["no_terminal_conflict"] = (True, "无 run 数据,跳过 terminal-conflict 校验")

    # 8. evidence_ledger(X69):evidence ledger 必须进入发布门验证 —— 收口必须能就本 issue 的 run 证据
    #    生成结构化 ledger 且每条证据带可重算指纹(X48 evidence_sha256),否则证据链不可追溯,不予收口。
    #    复用 line_evidence.build_ledger;离线/无 run 退化放行。
    if runs and _E is not None:
        try:
            led = _E.build_ledger(issue.get("id") or "", issue.get("identifier") or "", runs)
            entries = led.get("entries") or []
            intact = (led.get("entry_count") == len(runs)) and bool(entries) \
                and all(e.get("evidence_sha256") for e in entries)
            reasons["evidence_ledger"] = (
                intact,
                "evidence ledger 已生成并校验(%d 条,指纹齐全)" % led.get("entry_count", 0) if intact else
                "evidence ledger 不完整(条目=%s/run=%d 或缺可重算指纹),证据链不可追溯,不予收口" % (
                    led.get("entry_count"), len(runs)))
        except Exception as e:
            reasons["evidence_ledger"] = (False, "evidence ledger 生成失败,无法进入发布门:%s" % str(e)[:120])
    else:
        reasons["evidence_ledger"] = (True, "无 run 数据/离线,跳过 ledger 校验")

    required = ["dev_delivered", "audit_pass", "gate_pass", "page_evidence", "no_user_counter"]
    if runs and last_pass_ts:
        required.append("evidence_fresh")  # X32:有 run 数据才把新鲜度纳入 AND 门禁
    if runs:
        required += ["no_terminal_conflict", "evidence_ledger"]  # X42/X69:有 run 数据才纳入 AND 门禁
    allow = all(reasons[k][0] for k in required)
    return allow, page, reasons

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    issue = get_issue(a.issue_id)
    comments = get_comments(a.issue_id)
    runs = get_runs(a.issue_id)
    allow, page, reasons = evaluate(issue, comments, runs)
    if a.json:
        print(json.dumps({"allow": allow, "page_task": page,
                          "checks": {k: {"pass": v[0], "why": v[1]} for k, v in reasons.items()}},
                         ensure_ascii=False, indent=2))
    else:
        ident = issue.get("identifier") or a.issue_id
        print("DONE_REAL 门禁 %s:%s  (页面任务=%s)" % (ident, "✅ ALLOW" if allow else "⛔ BLOCK", page))
        order = ["dev_delivered", "audit_pass", "gate_pass", "page_evidence", "no_user_counter"]
        if "evidence_fresh" in reasons:
            order.append("evidence_fresh")
        for extra in ("no_terminal_conflict", "evidence_ledger"):
            if extra in reasons:
                order.append(extra)
        for k in order:
            ok, why = reasons[k]
            print("  [%s] %-16s %s" % ("PASS" if ok else "FAIL", k, why))
    sys.exit(0 if allow else 2)

if __name__ == "__main__":
    main()
