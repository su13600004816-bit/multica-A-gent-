#!/usr/bin/env python3
# 线机制混合桥(方案2):深挖=DeepSeek,门禁=Qwen,结果回写 Multica issue。
# 写/审在 Multica 原生(Claude/codex线主脑);本桥补 DeepSeek/Qwen 两环(它们无 Multica runtime)。
# 用法:
#   line_bridge.py deepdig <issue_id> --goal "<目标>" --problems "<审计FAIL问题>" [--parent <comment_id>] [--post]
#   line_bridge.py gate    <issue_id> --goal "<目标>" --evidence "<真机/检验证据>" [--parent <comment_id>] [--post]
# 不带 --post 只打印结果(供线主脑读取);带 --post 直接回写成 Multica 评论。
import os, sys, json, argparse, subprocess, urllib.request

ENVF = "/home/fleet/agent-control-plane/.control/.env"

def load_env():
    e = dict(os.environ)
    try:
        for line in open(ENVF, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1); v = v.strip().strip('"').strip("'")
            e.setdefault(k, v)
    except Exception: pass
    return e

def call_openai_compat(base, key, model, system, user, max_tokens=1200, timeout=90):
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user}], "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"].strip()

def call_local_model_api(url, system, user, max_tokens=1200, timeout=90):
    body = json.dumps({
        "system": system,
        "user": user,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }).encode()
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/chat"):
        endpoint += "/chat"
    req = urllib.request.Request(endpoint, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 10) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise RuntimeError("local model api failed: %s" % json.dumps(d, ensure_ascii=False)[:500])
    return str(d.get("content") or "").strip()

def post_comment(issue_id, content, parent=None):
    args = ["multica", "issue", "comment", "add", issue_id, "--content-stdin"]
    if parent: args += ["--parent", parent]
    p = subprocess.run(args, input=content, text=True, capture_output=True)
    return p.returncode == 0, (p.stderr or p.stdout)[:300]

DIGS_DIR = "/home/fleet/line-config/memory/.digs"

def _dig_path(key):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(key))
    return "%s/%s.md" % (DIGS_DIR, safe)

def deepdig(env, issue_id, goal, problems):
    # 递增式深挖(BOM 拆解法):深挖人#1..#10 跨返工轮【累积记忆】,每次在前序基础上更深一层。
    # 记忆只在任务 DONE 后由 line_reset 清除(见 line_reset.py),不跨轮清。
    os.makedirs(DIGS_DIR, exist_ok=True)
    dp = _dig_path(issue_id)
    prior = ""
    n = 1
    if os.path.exists(dp):
        prior = open(dp, encoding="utf-8").read()
        n = prior.count("<!--DEEPDIG_ROUND-->") + 1  # 用唯一哨兵计数,避免被 DeepSeek 回显的 header 污染
    # 加载 DP 的 SOP(skill 风格:版本化、单一来源,改 SOP 不动代码)
    sop_path = "/home/fleet/line-config/skills/deepdig-sop.md"
    sop = open(sop_path, encoding="utf-8").read() if os.path.exists(sop_path) else ""
    if n == 1:
        mode = "【本轮=第1轮·广度扫描BFS】一次列出最可能的 Top 3~5 根因,每条覆盖三镜头之一+可执行修复+自检。压成高信号提要(≤12行),让 Claude 一轮多点开花。"
    else:
        mode = "【本轮=第%d轮·递增单点深挖BOM】在前序全部结论之上,聚焦上轮没解决的点更深一层,不重复已说的,三镜头都过一遍。压成高信号提要。" % n
    sys_p = "%s\n\n%s" % (sop, mode)
    user = "本线目标:%s\n\n本轮审计FAIL问题:%s\n\n【前序深挖结论(第1~%d次)】:\n%s" % (
        goal, problems, n - 1, prior[-4000:] if prior else "(无,这是第1次深挖)")
    if not env.get("DEEPSEEK_API_URL"):
        raise RuntimeError("DEEPSEEK_API_URL missing; refusing non-dedicated API route")
    out = call_local_model_api(env["DEEPSEEK_API_URL"], sys_p, user)
    with open(dp, "a", encoding="utf-8") as f:
        f.write("\n<!--DEEPDIG_ROUND-->\n## 第%d次深挖(递增下探)\n问题:%s\n\n%s\n" % (n, problems[:200], out))
    return "(第%d次递增深挖 / 共10次上限)\n\n%s" % (n, out)

# 页面/视觉目标关键词:命中则按页面任务,强制要求真实 URL + 截图。
GATE_PAGE_KEYWORDS = ("页面", "画布", "canvas", "视觉", "截图", "UI", "前端", "入口", "按钮", "详情页", "面板", "弹窗", "样式")
# 代码/补丁/构建/核查类证据标记:非页面任务用这些证据判定,不强制截图。
GATE_CODE_MARKERS = ("apply", "diff", "patch", "numstat", "PR ", "/pull/", "push", "build", "tsc",
                     "typecheck", "commit", "sha", "git ", "exit 0", "基线", "编译", "用例", "测试", "回归", "checkout")

def gate(env, goal, evidence):
    # BOM-4:无证据直接 FAIL,不让模型替空证据背书。
    # 页面/视觉任务:必须有真实 URL 或截图(否则无法读图判定)。
    # 代码/补丁/构建类(非页面)任务:接受补丁可应用性、构建/类型检查、测试、文件清单等代码证据,
    #   不再因缺少页面截图而误判 —— 这是 PL-127 类纯代码核查任务无法走 gate 的根因(catch-22)。
    ev = (evidence or "").strip()
    goal_s = goal or ""
    is_page = any(k in goal_s for k in GATE_PAGE_KEYWORDS)
    has_url = ("http://" in ev) or ("https://" in ev)
    has_shot = any(m in ev for m in ("截图", "screenshot", ".png", ".jpg", ".jpeg", "图:"))
    has_code = any(m in ev for m in GATE_CODE_MARKERS)
    if not ev:
        return ("证据缺失:未提供任何真机/检验证据。\n"
                "VERDICT: FAIL 无证据,按门禁铁律直接判失败。")
    if is_page and not (has_url or has_shot):
        return ("证据缺失:页面/视觉任务必须给真实 URL + 截图,当前无法读图判定。\n"
                "VERDICT: FAIL 页面任务无 URL/截图,按门禁铁律直接判失败,请补真站 URL + 截图后再走 gate。")
    if not is_page and not (has_url or has_shot or has_code):
        return ("证据缺失:非页面任务也未给出补丁/构建/测试/核查等任何代码证据。\n"
                "VERDICT: FAIL 无具体证据,按门禁铁律直接判失败。")
    sys_p = ("你是生产线门禁(Qwen),负责真机/检验放行判定。依据证据判断是否真达成目标(不是假过)。"
             "若目标是页面/视觉类,证据无真实 URL 或截图一律判 FAIL;"
             "若目标是代码/补丁/构建/核查类,按补丁可应用性、构建/类型检查、测试、文件清单等代码证据严格判定,"
             "不要因缺少页面截图而误判,也不要替薄弱证据背书。"
             "最后单独一行输出 VERDICT: PASS 或 VERDICT: FAIL,然后一句话说原因。中文。")
    user = "本线目标:%s\n\n真机/检验证据:%s" % (goal, evidence)
    if not env.get("QWEN_API_URL"):
        raise RuntimeError("QWEN_API_URL missing; refusing non-dedicated API route")
    return call_local_model_api(env["QWEN_API_URL"], sys_p, user)

MEM_ROOT = "/home/fleet/line-config/memory"
TIERS = [("T1-简要说明书", "T1 简要说明书:一段话讲清这个任务是什么、目标、最终结论。最多4行。"),
         ("T2-段落简要记忆", "T2 段落简要记忆:关键决策、踩的坑、结果,分几段。"),
         ("T3-完全短记忆", "T3 完全短记忆:完整但精炼的过程记录(做了什么→怎么做→验证→结论)。"),
         ("T4-完全长记忆", "T4 完全长记忆:全量细节、关键命令/日志/证据、完整溯源。")]

def archive_task(env, issue_id):
    # 记忆总管(Qwen):把一个任务按编号生成 T1-T4 四梯度记忆写盘。
    g = subprocess.run(["multica", "issue", "get", issue_id, "--output", "json"], capture_output=True, text=True)
    ident = issue_id
    src = ""
    try:
        d = json.loads(g.stdout)
        ident = d.get("identifier") or issue_id
        src = "标题:%s\n状态:%s\n描述:\n%s" % (d.get("title", ""), d.get("status", ""), (d.get("description") or "")[:4000])
    except Exception:
        src = g.stdout[:3000]
    c = subprocess.run(["multica", "issue", "comment", "list", issue_id, "--recent", "8", "--output", "json"], capture_output=True, text=True)
    try:
        cs = json.loads(c.stdout); cs = cs if isinstance(cs, list) else cs.get("comments", cs.get("items", []))
        src += "\n\n评论摘录:\n" + "\n".join((("[%s] " % x.get("author_type", "")) + (x.get("content", "") or "")[:500]) for x in cs[-8:])
    except Exception:
        pass
    written = []
    for d, instr in TIERS:
        prompt = ("你是记忆总管。基于以下任务记录,只产出【%s】这一梯度的内容,不要别的梯度、不要解释。\n要求:%s\n\n任务记录:\n%s"
                  % (d, instr, src[:6000]))
        try:
            if not env.get("QWEN_API_URL"):
                raise RuntimeError("QWEN_API_URL missing; refusing non-dedicated API route")
            out = call_local_model_api(env["QWEN_API_URL"],
                "你是记忆总管,产出精炼准确的分梯度记忆。中文。", prompt, max_tokens=1500)
        except Exception as e:
            out = "(生成失败:%s)" % str(e)[:120]
        path = "%s/%s/%s.md" % (MEM_ROOT, d, ident)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# [%s] %s\n\n> 任务编号:%s ｜ 记忆总管(Qwen)归档\n\n%s\n" % (ident, d, ident, out))
        written.append(path)
    return ident, written

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["deepdig", "gate", "archive"])
    ap.add_argument("issue_id")
    ap.add_argument("--goal", default="")
    ap.add_argument("--problems", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--parent", default=None)
    ap.add_argument("--post", action="store_true")
    a = ap.parse_args()
    env = load_env()
    if a.role == "archive":
        ident, paths = archive_task(env, a.issue_id)
        print("ARCHIVED %s -> 四梯度:" % ident)
        for p in paths: print("  " + p)
        return
    if a.role == "deepdig":
        out = deepdig(env, a.issue_id, a.goal, a.problems)
        header = "【深挖人 DeepSeek · 递增下探 根因+修复方案】\n\n"
    else:
        out = gate(env, a.goal, a.evidence)
        header = "【门禁 Qwen · 真机检验判定】\n\n"
    body = header + out
    if a.post:
        ok, msg = post_comment(a.issue_id, body, a.parent)
        print("POSTED" if ok else "POST_FAIL: " + msg)
    else:
        print(body)

if __name__ == "__main__":
    main()
