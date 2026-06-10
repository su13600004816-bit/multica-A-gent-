#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线机制 B 段(证据/门禁/路由闭环)纯函数 + 可注入探针库。

PL-104 B 段 BOM 覆盖(U25-U40 + X25-X40 相关):
  U25/U26 Qwen/DeepSeek endpoint 探针   : probe_endpoint / health_alert_lines
  U27     真机/adb/scrcpy 可用性探针      : probe_device
  U28     附件/图片/URL 真实存在性验证    : extract_urls / verify_artifacts
  U29     GitHub PR/commit 证据验证        : parse_github_refs / verify_github_refs
  U30     结构化 evidence ledger(按 issue): build_ledger / write_ledger
  U31     证据来源信任策略(self vs trusted): evidence_trust / stage_requires_trusted
  U32     rerun 后证据新鲜度(绑 run_id/ts) : run_created_after / evidence_is_stale
  U36     assign/rerun 后二次确认 run 启动  : run_created_after(被 watchdog 调用)
  U37     PL-94 失败兜底(本地状态文件通道)  : fallback_record / append_fallback
  X32     gate FAIL/用户反例后旧 PASS 失效   : evidence_is_stale + 信任策略
  X38     阶段转换记录 from/to/event/source  : ledger entry 带 source/verdict
  X40     不同异常不同严重级别              : SEVERITY

设计原则(与 line_states.py 一致):
  - 判定/解析全部是纯函数,可离线单测。
  - 一切外部副作用(网络探针、gh CLI、文件写)都走**可注入参数**:
    默认实现走真实 urllib/subprocess,测试时传入 fixture callable 即可全离线。
  - 不做任何 Multica 平台写;贴台/路由仍在 line_watchdog.py。
"""
import os
import re
import json
import hashlib
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# 通用
# ─────────────────────────────────────────────────────────────────────────────
def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _result_text(run):
    """把 run.result 规整成可检索文本(dict/str/None 都支持)。返回 (text, pr_url)。"""
    res = run.get("result") if isinstance(run, dict) else None
    if res is None:
        return "", ""
    if isinstance(res, str):
        return res, ""
    if isinstance(res, dict):
        return str(res.get("output") or ""), str(res.get("pr_url") or "")
    return str(res), ""


# ─────────────────────────────────────────────────────────────────────────────
# X40:异常严重级别(stale/no_run/failed/cancelled/misrouted/evidence_missing 分级)
# ─────────────────────────────────────────────────────────────────────────────
SEVERITY = {
    "zombie": "critical",
    "failed": "critical",
    "terminal_conflict": "critical",
    "cancelled": "high",
    "gate_misrouted": "high",
    "evidence_missing": "high",
    "evidence_unverified": "high",
    "gate_fail_rework": "high",
    "stage_stale": "medium",
    "blocked_stale": "medium",
    "in_progress_no_run": "medium",
    "no_run": "medium",
    "todo_no_claim": "low",
    "personal_assignment": "high",
    "probe_down": "critical",
    "device_offline": "high",
    "route_unconfirmed": "high",
    "coverage_limit": "low",
    "schema_drift": "medium",
}


def severity_of(kind):
    """X40:把告警 kind 映射到严重级别;未知默认 medium(不静默)。"""
    return SEVERITY.get(kind, "medium")


# ─────────────────────────────────────────────────────────────────────────────
# U28:附件/图片/URL 真实存在性验证(可注入 fetcher)
# 文本里写"截图"不算数 —— 必须真有可访问的 URL,或附件清单里真有图片产物。
# ─────────────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s)\]}>'\"，。、]+", re.IGNORECASE)
_IMG_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|gif)\b", re.IGNORECASE)
_SHOT_WORD_RE = re.compile(r"(截图|screenshot|playwright|浏览器截图|browser shot)", re.IGNORECASE)


def extract_urls(text):
    return _URL_RE.findall(text or "")


def _default_url_fetcher(url, timeout=8):
    """默认 URL 探针:HEAD/GET 看是否 < 400。测试时用注入 fetcher 替代。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, getattr(r, "status", 200)
    except Exception:
        # 有些站不支持 HEAD,退回 GET 头部
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, getattr(r, "status", 200)
        except Exception as e:
            return False, str(e)[:120]


def attachments_have_image(attachments):
    """附件清单里是否真有图片产物(content_type=image/* 或文件名是图片)。"""
    for a in attachments or []:
        ct = str(a.get("content_type") or "")
        fn = str(a.get("filename") or a.get("url") or "")
        if ct.startswith("image/") or _IMG_EXT_RE.search(fn):
            return True
    return False


def verify_artifacts(text, attachments=None, fetcher=None, verify_network=False):
    """U28:验证一段证据文本里的截图/URL 是不是"真存在",而非文本占位。

    返回 dict:
      claims_screenshot : 文本声称有截图(出现"截图/screenshot"或图片扩展名)
      urls              : 抽到的 URL 列表
      image_in_attach   : 附件清单里真有图片产物
      url_checked       : 是否真的发起了网络验证(verify_network=True 才会)
      reachable_urls / unreachable_urls : 网络验证结果(未验证时均空)
      verified          : 综合判定 —— 声称有视觉证据时,是否真有(图片附件 或 可达URL)
      reason            : 不通过原因(verified=False 时)
    """
    text = text or ""
    urls = extract_urls(text)
    claims_shot = bool(_SHOT_WORD_RE.search(text) or _IMG_EXT_RE.search(text))
    image_in_attach = attachments_have_image(attachments)
    reachable, unreachable = [], []
    url_checked = False
    if verify_network and urls:
        url_checked = True
        f = fetcher or _default_url_fetcher
        for u in urls:
            ok, info = f(u)
            (reachable if ok else unreachable).append(u)
    # 综合判定:声称有视觉证据(截图)时,必须有图片附件,或(已验证且有可达 URL)。
    verified = True
    reason = ""
    if claims_shot and not image_in_attach:
        if not urls:
            verified = False
            reason = "文本声称有截图,但无图片附件、无任何 URL —— 疑似文本占位"
        elif verify_network and not reachable:
            verified = False
            reason = "文本声称有截图且给了 URL,但 URL 全部不可达,且无图片附件"
        # verify_network=False 且有 URL:无法证伪也无法证实 -> 标记需联网核(下方 reason 空,verified True 但 url_checked False)
    return {
        "claims_screenshot": claims_shot,
        "urls": urls,
        "image_in_attach": image_in_attach,
        "url_checked": url_checked,
        "reachable_urls": reachable,
        "unreachable_urls": unreachable,
        "verified": verified,
        "reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# U29:GitHub PR / commit 证据验证(可注入 gh runner)
# 先做格式校验(纯离线),再可选真验(gh api / git)。
# ─────────────────────────────────────────────────────────────────────────────
_PR_URL_RE = re.compile(r"https?://github\.com/([\w.\-]+)/([\w.\-]+)/pull/(\d+)", re.IGNORECASE)
_PR_HASH_RE = re.compile(r"\bPR\s*[#＃]\s*(\d+)", re.IGNORECASE)
_COMMIT_RE = re.compile(r"\b(?:commit\s*[:：]?\s*)?([0-9a-f]{7,40})\b", re.IGNORECASE)
_COMMIT_WORD_RE = re.compile(r"(commit|提交|sha|已推送|已 ?push|已合并)", re.IGNORECASE)


def parse_github_refs(text):
    """抽取 GitHub PR URL / PR 号 / commit SHA。返回 dict。

    commit:只在文本出现 commit 类词时才把裸 16 进制串当 SHA,避免把
    普通十六进制(如 run_id 片段)误判成 commit。
    """
    text = text or ""
    pr_urls = []
    for m in _PR_URL_RE.finditer(text):
        pr_urls.append({"repo": "%s/%s" % (m.group(1), m.group(2)), "number": int(m.group(3)),
                        "url": m.group(0)})
    pr_numbers = [int(m.group(1)) for m in _PR_HASH_RE.finditer(text)]
    commits = []
    if _COMMIT_WORD_RE.search(text):
        for m in _COMMIT_RE.finditer(text):
            sha = m.group(1).lower()
            if sha not in commits and not sha.isdigit():  # 排除纯数字(PR 号/编号)
                commits.append(sha)
    return {"pr_urls": pr_urls, "pr_numbers": pr_numbers, "commits": commits}


def github_ref_wellformed(refs):
    """格式自检(纯离线):有 PR URL 且 repo/number 合法,或有 7-40 位 commit。"""
    for p in refs.get("pr_urls", []):
        if p.get("repo") and "/" in p["repo"] and p.get("number", 0) > 0:
            return True
    for c in refs.get("commits", []):
        if re.fullmatch(r"[0-9a-f]{7,40}", c):
            return True
    return False


def _default_gh_runner(args, timeout=20):
    """默认 gh CLI 调用:返回 (ok, stdout)。无 gh 或失败 -> (False, err)。"""
    try:
        p = subprocess.run(["gh"] + list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr)[:400]
    except Exception as e:
        return False, str(e)[:160]


def verify_github_refs(text, gh_runner=None, verify_network=False):
    """U29:验证 PR/commit 证据。

    verify_network=False:只做格式自检(wellformed),不触网。
    verify_network=True :对每个 PR URL 调 gh api 确认存在(可注入 gh_runner)。
    返回 dict(refs, wellformed, checked, valid_prs, invalid_prs, reason)。
    """
    refs = parse_github_refs(text)
    wf = github_ref_wellformed(refs)
    valid, invalid = [], []
    checked = False
    if verify_network and refs["pr_urls"]:
        checked = True
        runner = gh_runner or _default_gh_runner
        for p in refs["pr_urls"]:
            ok, _out = runner(["api", "repos/%s/pulls/%d" % (p["repo"], p["number"])])
            (valid if ok else invalid).append(p["url"])
    reason = ""
    if not wf:
        reason = "未发现合法 PR URL 或 commit SHA(自报 PR/commit 无法被解析校验)"
    elif checked and invalid:
        reason = "PR URL 经 gh api 校验不存在/不可达:%s" % ",".join(invalid)
    return {"refs": refs, "wellformed": wf, "checked": checked,
            "valid_prs": valid, "invalid_prs": invalid,
            "ok": wf and not (checked and invalid), "reason": reason}


_CLAIMS_PR_RE = re.compile(r"(PR\s*[#＃]|pull request|/pull/|已合并|merged)", re.IGNORECASE)


def audit_completed_evidence(text, attachments=None):
    """U28/U29 离线证据自检:一个『声称有证据』的 completed run,证据是否可被核验。

    纯格式级(不触网),专抓两类绕过:
      - 声称截图但无图片附件、无任何 URL、无图片扩展名 -> 文本占位(U28)
      - 声称 PR/commit 但解析不出合法 PR URL/commit SHA(裸『PR #5』无 URL 等)(U29)
    返回 reasons[](空=证据格式上可核验/或本就没声称这两类)。
    """
    text = text or ""
    reasons = []
    art = verify_artifacts(text, attachments=attachments)
    if art["claims_screenshot"] and not art["image_in_attach"] \
            and not art["urls"] and not _IMG_EXT_RE.search(text):
        reasons.append("声称截图但无图片附件/URL/图片文件名,疑似文本占位(U28)")
    claims_pr = bool(_CLAIMS_PR_RE.search(text))
    if claims_pr:
        gh = parse_github_refs(text)
        if not github_ref_wellformed(gh):
            reasons.append("声称 PR/commit 但无合法 PR URL 或 commit SHA,无法校验(U29)")
    return reasons


# ─────────────────────────────────────────────────────────────────────────────
# U31:证据来源信任策略 —— self-report PASS 不等于 trusted-source PASS
# ─────────────────────────────────────────────────────────────────────────────
# 可信来源:专属审计 Codex、真门禁(line_bridge gate / 门禁 Qwen)、gh 校验过的 PR、
# Playwright/真机产物。自报来源:开发自己说"我跑过了/PASS"。
TRUSTED_SOURCES = {"codex_audit", "qwen_gate", "github_verified", "playwright_artifact", "device_probe"}
SELF_REPORT_SOURCES = {"claude_dev", "self_report", "comment_claim"}

# 需要可信来源才能放行的阶段(自报不算数)。
TRUSTED_REQUIRED_STAGES = {"gate", "done", "page_evidence"}

_AUDIT_SRC_RE = re.compile(r"(专属审计|审计\s*codex|\baudit\b|审计结论|VERDICT.*审计)", re.IGNORECASE)
_GATE_SRC_RE = re.compile(r"(【门禁|门禁\s*qwen|\bqwen\b|千问)", re.IGNORECASE)
_PLAYWRIGHT_SRC_RE = re.compile(r"(playwright|真机|browser shot|浏览器截图)", re.IGNORECASE)

# B-U31:自证口吻 —— 作者自称"我跑了/我已测/自报"。这类文本即便提到 qwen/千问/门禁,
# 也只是开发自证,不能凭关键词升级为可信门禁/审计来源。
_SELF_REPORT_RE = re.compile(
    r"(自报|self[\s_-]?report|自证|"
    r"我(已经|已|自己|刚|也)?(跑|测|验证|执行|调用|确认|做)了?过?|"
    r"自己(跑|测|验证|确认)过?)",
    re.IGNORECASE)

# 可信门禁/审计 agent 身份(真实调用方)。来源 agent 落在这两个集合里时,
# 才允许凭 agent 身份采信为门禁/审计来源,而不是凭文本关键词。
TRUSTED_GATE_AGENTS = {
    "门禁 Qwen", "门禁-Qwen", "qwen_gate", "qwen-gate",
    "line_bridge", "line_bridge gate", "line_bridge.py gate",
}
TRUSTED_AUDIT_AGENTS = {
    "审计Codex", "审计 Codex", "审计Codex-T01", "专属审计 Codex",
    "codex_audit", "codex-audit",
}


def is_self_report(text):
    """B-U31:文本是否为开发/Claude 的自证口吻(我跑了/自报)。

    自报即便包含 qwen/千问/门禁 字样,也不得据此升级为可信门禁/审计来源。
    """
    return bool(_SELF_REPORT_RE.search(text or ""))


def classify_source(text, agent=None):
    """从一段回写文本(+可选作者 agent 身份)推断证据来源类型(U31)。

    B-U31 修复:仅凭文本出现 qwen/千问/门禁/审计 字样不足以判为可信来源。
      1. 可信门禁/审计 agent 身份调用 -> 直接采信对应来源。
      2. 自报口吻(『我跑了 / 自报』)即便含 Qwen 字样,一律降级为 self_report,
         禁止开发/Claude 自证文本被归为可信 qwen_gate / codex_audit。
      3. 否则才按非自报的结构化关键词推断来源(真门禁回写、审计结论、真机产物)。
    """
    t = text or ""
    a = (agent or "").strip()
    if a in TRUSTED_GATE_AGENTS:
        return "qwen_gate"
    if a in TRUSTED_AUDIT_AGENTS:
        return "codex_audit"
    if is_self_report(t):
        return "self_report"
    if _GATE_SRC_RE.search(t):
        return "qwen_gate"
    if _AUDIT_SRC_RE.search(t):
        return "codex_audit"
    if _PLAYWRIGHT_SRC_RE.search(t):
        return "playwright_artifact"
    return "self_report"


def evidence_trust(source):
    """U31:来源信任级别 -> 'trusted' / 'self_report' / 'untrusted'。"""
    if source in TRUSTED_SOURCES:
        return "trusted"
    if source in SELF_REPORT_SOURCES:
        return "self_report"
    return "untrusted"


def stage_requires_trusted(stage):
    return stage in TRUSTED_REQUIRED_STAGES


def passes_trust_policy(stage, source):
    """U31:某阶段的 PASS,其来源是否满足信任策略。

    门禁/收口/页面证据阶段必须 trusted 来源;其它阶段 self-report 也接受。
    """
    if stage_requires_trusted(stage):
        return evidence_trust(source) == "trusted"
    return evidence_trust(source) in ("trusted", "self_report")


# ─────────────────────────────────────────────────────────────────────────────
# U32 / X32 / U36:证据新鲜度 + 路由后二次确认(都基于 run_id/时间戳)
# ─────────────────────────────────────────────────────────────────────────────
def run_created_after(runs, since_iso, statuses=None):
    """返回 created_at 严格晚于 since_iso 的最新 run(可选限定 status);否则 None。

    U36:assign/rerun 后用它二次确认"确实有新 run 启动"。
    U32:也用于判断某证据 run 之后是否又有了更新的 run(旧 PASS 可能已过期)。
    """
    since = _parse_ts(since_iso)
    if since is None:
        return None
    best = None; best_ts = None
    for r in runs or []:
        if statuses and (r.get("status") not in statuses):
            continue
        ts = _parse_ts(r.get("created_at"))
        if ts is None or ts <= since:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = r, ts
    return best


def evidence_is_stale(evidence_run_id, evidence_ts, runs):
    """U32/X32:一条 PASS 证据是否已被更新的 run 覆盖(旧 PASS 盖不住新失败)。

    若 evidence 之后存在更新的关键 run(尤其 failed/cancelled),旧 PASS 失效。
    返回 (stale: bool, reason)。
    """
    newer = run_created_after(runs, evidence_ts)
    if not newer:
        return False, ""
    nid = newer.get("id")
    if nid and nid == evidence_run_id:
        return False, ""
    nstat = (newer.get("status") or "").lower()
    if nstat in ("failed", "cancelled"):
        return True, "证据时间(%s)之后又出现 %s run(%s),旧 PASS 失效,需重新门禁" % (
            evidence_ts, nstat, (nid or "")[:8])
    return True, "证据时间(%s)之后已有更新 run(%s),旧 PASS 可能已不代表当前代码" % (
        evidence_ts, (nid or "")[:8])


# 覆盖链里"终态"run(已结束):它们在 PASS 之后跑完,代表 PASS 未覆盖的新结论 -> 覆盖旧 PASS。
_TERMINAL_RUN_ST = ("completed", "failed", "cancelled")


def run_chain_deep_check(evidence_run_id, evidence_ts, runs):
    """深挖·版本追溯(DeepSeek 第2轮根因②):一条 PASS 证据所依赖的 run 是否已被
    后续 run 覆盖,并给出**完整覆盖链**,供"新 run→强制重检→回写"闭环判定。

    与 evidence_is_stale 的区别:后者只回 (bool, 一句话),且原语义只盯"新失败盖旧PASS"
    (failed/cancelled),会放过"新 completed run 盖旧 PASS"——正是 PL-94 旧 PASS 被
    f19d9605/5c64a47f 等 completed run 覆盖却被静默收口的根因。本函数把判据扩到**所有
    终态 run**(completed/failed/cancelled):它们在 PASS 之后跑完,代表 PASS 没覆盖的新结论。

    关键边界(防误报正常流水线):证据之后**仅在 running 的** run 不算覆盖 —— 它正是
    "新 run→重检"中正在跑的那次重检/门禁(健康的阶段推进),据此报 stale 会把正常
    pipeline 误杀、且会触发重复路由。这类 run 单列在 in_flight,只作信息,不进覆盖链。

    返回 dict:
      superseded   : bool —— 证据之后是否有**终态**更新 run(=旧 PASS 已被覆盖,需强制重检)
      hard_invalid : bool —— 覆盖链里是否含 failed/cancelled(旧 PASS 硬失效,不止"需重检")
      worst_status : 覆盖链中最严重的 run 状态(failed/cancelled 优先于 completed),无则 ""
      chain        : [{id,status,created_at}, ...] 证据之后的**终态**覆盖 run,时间倒序(最新在前)
      latest_run   : 覆盖链中最新的一条(=当前应据其重检的 run),无则 None
      in_flight    : [{...}, ...] 证据之后仍在 running 的 run(进行中的重检,不算覆盖)
      reason       : 人读说明(superseded=False 时为空)
    """
    since = _parse_ts(evidence_ts)
    chain, in_flight = [], []
    if since is not None:
        for r in runs or []:
            rid = r.get("id")
            if rid and rid == evidence_run_id:
                continue  # 证据 run 自身不算覆盖
            ts = _parse_ts(r.get("created_at"))
            if ts is None or ts <= since:
                continue
            rec = {"id": rid, "status": (r.get("status") or "").lower(),
                   "created_at": r.get("created_at")}
            (chain if rec["status"] in _TERMINAL_RUN_ST else in_flight).append(rec)
    chain.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    in_flight.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    if not chain:
        return {"superseded": False, "hard_invalid": False, "worst_status": "",
                "chain": [], "latest_run": None, "in_flight": in_flight, "reason": ""}
    hard = any(c["status"] in ("failed", "cancelled") for c in chain)
    worst = next((c["status"] for c in chain if c["status"] in ("failed", "cancelled")),
                 chain[0]["status"])
    brief = ",".join("%s(%s)" % ((c["id"] or "")[:8], c["status"] or "?") for c in chain[:4])
    more = "等%d条" % len(chain) if len(chain) > 4 else ""
    if hard:
        reason = ("证据时间(%s)之后出现 %d 条已结束的更新 run(含 failed/cancelled):%s%s —— "
                  "旧 PASS 硬失效,必须以最新 run 重新走门禁/验收后回写新证据,不得沿用旧 PASS 收口" % (
                      evidence_ts, len(chain), brief, more))
    else:
        reason = ("证据时间(%s)之后出现 %d 条已结束的更新 run:%s%s —— 旧 PASS 已被新 run 覆盖,"
                  "需对最新 run 强制重检并回写新证据,旧 PASS 不再代表当前代码" % (
                      evidence_ts, len(chain), brief, more))
    return {"superseded": True, "hard_invalid": hard, "worst_status": worst,
            "chain": chain, "latest_run": chain[0], "in_flight": in_flight, "reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# PL-132:旧 PASS 失效改为"基于有效证据 run/commit"判定
#
# 背景(PL-128 一晚死循环根因):run_chain_deep_check 把"证据之后**任何**终态 run"
# 都当成覆盖旧 PASS —— 但 cancelled、纯派发/看门狗自动分流、队长 no_action、个人
# mention 触发、无 VERDICT 的 completed run 都被计入,导致越 rerun 越覆盖、每 2 分钟刷屏。
#
# 新协议:旧 PASS 失效**只**认"最新一条有效门禁证据"(带 VERDICT 的 gate run)。
# 无关 run 一律不参与判定。锚点绑定 run_id + commit + 结束时间,而非裸 timestamp。
# ─────────────────────────────────────────────────────────────────────────────

# 纯派发 / 看门狗自动分流 run(只路由到小队,不构成门禁证据)。
_DISPATCH_RUN_RE = re.compile(
    r"(看门狗自动分流|自动分流|小队路由|纯脚本[,，]?\s*不判因|纯派发|续派|"
    r"assignment\s*only|dispatch\s*only)", re.IGNORECASE)
# no_action / 队长协调 run(只记录、不改代码/状态、已收口无需重开)。
_NO_ACTION_RUN_RE = re.compile(
    r"(no[_\s-]?action|只\s*记录|只做协调|我只(做)?协调|只判断这条|只判断本次|"
    r"不直接改代码|不改代码|未改状态|未改代码|无需重开|不(能|得)重开|"
    r"已(据证据)?收口|已是\s*[`'\"]?cancelled|仅记录本次|不需要回复|未重开|未评论|"
    r"只记录本次触发评估|记录本次评估为)", re.IGNORECASE)
# 个人 @agent mention 触发的 run(协议:派发只能到小队;个人 mention = protocol_violation)。
_PERSONAL_MENTION_RE = re.compile(r"mention://agent/", re.IGNORECASE)
# 门禁证据要素:测试命令 / 测试结果。
_TEST_CMD_RE = re.compile(
    r"(pytest|python3?\s+-m\s+pytest|npm\s+(run\s+)?test|yarn\s+test|go\s+test|cargo\s+test|"
    r"run_regression|测试命令|测试结果|tests?\s+(passed|failed|ok)|\d+\s+passed)", re.IGNORECASE)
_VERDICT_PASS_RE = re.compile(r"VERDICT[:：\s]*PASS", re.IGNORECASE)
_VERDICT_FAIL_RE = re.compile(r"VERDICT[:：\s]*FAIL", re.IGNORECASE)


def _run_text_blob(run):
    """把 run 的 result.output / pr_url / trigger_summary 合成一段可检索文本。返回 (blob, pr)。"""
    txt, pr = _result_text(run)
    trig = str(run.get("trigger_summary") or "") if isinstance(run, dict) else ""
    return (txt + " " + pr + " " + trig).strip(), pr


def run_finished_at(run):
    """一条 run 的结束时间锚点:completed_at > finished_at > created_at。"""
    if not isinstance(run, dict):
        return None
    return run.get("completed_at") or run.get("finished_at") or run.get("created_at")


def run_evidence_role(run):
    """PL-132 核心:判定一条 run 在"旧 PASS 失效"判据里的角色。

    返回 dict:
      category      : gate | dispatch | no_action | personal_mention | cancelled | running | empty
      gate_relevant : bool   —— 只有 gate_relevant 的新 run 才可能使旧 PASS 失效/构成新证据
      verdict       : 'PASS' | 'FAIL' | None  (仅 gate 类解析)
      has_evidence  : bool   —— 是否带 VERDICT/commit/测试命令/PR/截图 之一
      commit        : str    —— 抓到的首个 commit sha(无则 '')
      reason        : str

    判定顺序(先排除一切"非门禁证据" run,最后才解析 gate 裁决):
      running/queued -> 进行中,不算覆盖
      cancelled      -> 协议:单独不得使旧 PASS 失效
      personal @agent mention 触发 -> 协议违规,不得作为覆盖依据
      纯派发/自动分流 -> 只路由,不构成门禁证据
      no_action/协调 -> 不改代码/状态,不构成门禁证据
      空 run         -> 无产出,不构成门禁证据
      其余 completed/failed -> gate 类;gate_relevant 仅当带 VERDICT 裁决。
    """
    status = ((run.get("status") if isinstance(run, dict) else "") or "").lower()
    blob, pr = _run_text_blob(run)

    def _r(category, reason, gate_relevant=False, verdict=None, has_evidence=False, commit=""):
        return {"category": category, "gate_relevant": gate_relevant, "verdict": verdict,
                "has_evidence": has_evidence, "commit": commit, "reason": reason}

    if status in ("running", "queued", "pending", "dispatched", "in_progress", "started"):
        return _r("running", "run 仍在进行中,不构成对旧 PASS 的覆盖")
    if status == "cancelled":
        return _r("cancelled", "cancelled run 不得单独使旧 PASS 失效")
    if _PERSONAL_MENTION_RE.search(blob):
        return _r("personal_mention",
                  "个人 @agent mention 触发的 run(协议违规:派发只能到小队),不得作为旧 PASS 覆盖依据")
    if _DISPATCH_RUN_RE.search(blob):
        return _r("dispatch", "纯派发/看门狗自动分流 run,不构成门禁证据")
    if _NO_ACTION_RUN_RE.search(blob):
        return _r("no_action", "no_action/协调 run(不改代码/状态),不构成门禁证据")
    if not blob:
        return _r("empty", "终态 run 无任何产出,不构成门禁证据")

    verdict = "PASS" if _VERDICT_PASS_RE.search(blob) else (
        "FAIL" if _VERDICT_FAIL_RE.search(blob) else None)
    gh = parse_github_refs(blob)
    commit = gh["commits"][0] if gh["commits"] else ""
    art = verify_artifacts(blob)
    has_evidence = bool(verdict or commit or gh["pr_urls"] or _TEST_CMD_RE.search(blob)
                        or art["urls"] or art["claims_screenshot"])
    # gate_relevant 必须带可识别的门禁裁决(VERDICT)。仅有 commit/测试命令但无 VERDICT 的
    # completed run,算"做了门禁动作但裁决缺失" -> 后续走 evidence_missing,**不**算覆盖旧 PASS。
    gate_relevant = verdict is not None
    reason = ("门禁裁决 VERDICT:%s" % verdict) if verdict else \
        "completed run 做了门禁/代码动作但缺 VERDICT 裁决(evidence_missing)"
    return _r("gate", reason, gate_relevant=gate_relevant, verdict=verdict,
              has_evidence=has_evidence, commit=commit)


def latest_valid_evidence(runs):
    """PL-132:在 runs 里找"最新一条有效门禁证据"(gate 且带 VERDICT)。

    锚点绑定 run_id + commit + 结束时间(不再只用裸 timestamp)。无有效证据返回 None。
    返回 dict:{run_id, verdict, commit, evidence_at, gate_status}。
    """
    best = None; best_ts = None
    for r in runs or []:
        role = run_evidence_role(r)
        if not role["gate_relevant"]:
            continue
        ts = _parse_ts(run_finished_at(r))
        if ts is None:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = (r, role), ts
    if not best:
        return None
    r, role = best
    return {
        "run_id": r.get("id"),
        "verdict": role["verdict"],
        "commit": role["commit"],
        "evidence_at": run_finished_at(r),
        "gate_status": "pass" if role["verdict"] == "PASS" else "fail",
    }


def evidence_gate_decision(runs):
    """PL-132:对一个 issue 的 runs 给出门禁判定,根治"旧 PASS 被无关 run 覆盖"循环。

    判定**只**认"最新一条有效门禁证据"(VERDICT run);无关 run(cancelled / 纯派发 /
    no_action / 个人 mention / 空 run / 无裁决 completed)一律不参与旧 PASS 失效判定。

    返回 dict:
      status      : pass | fail | evidence_missing | no_evidence
      anchor      : latest_valid_evidence(...) 结果或 None
      reason      : 人读说明
      missing_run : evidence_missing 时,缺裁决的最新 completed gate run 摘要({id,at}),用于去重/冷却
    """
    anchor = latest_valid_evidence(runs)
    if anchor:
        if anchor["verdict"] == "PASS":
            return {"status": "pass", "anchor": anchor, "missing_run": None,
                    "reason": "最新有效门禁证据为 PASS(run=%s commit=%s @%s);无关 run 不使其失效" % (
                        (anchor["run_id"] or "")[:8], (anchor["commit"] or "-")[:8] or "-",
                        anchor["evidence_at"])}
        return {"status": "fail", "anchor": anchor, "missing_run": None,
                "reason": "最新有效门禁证据为 FAIL(run=%s @%s),应按 FAIL 返工,不得用旧 PASS 收口" % (
                    (anchor["run_id"] or "")[:8], anchor["evidence_at"])}
    # 无任何有效 VERDICT 证据:看是否有"做了门禁/代码动作但缺裁决"的 completed run。
    missing = None; missing_ts = None
    for r in runs or []:
        if ((r.get("status") or "").lower()) != "completed":
            continue
        role = run_evidence_role(r)
        if role["category"] != "gate":   # gate 但无 VERDICT(gate_relevant=False)
            continue
        ts = _parse_ts(run_finished_at(r))
        if missing_ts is None or (ts is not None and ts > missing_ts):
            missing, missing_ts = r, ts
    if missing:
        return {"status": "evidence_missing", "anchor": None,
                "missing_run": {"id": missing.get("id"), "at": run_finished_at(missing)},
                "reason": "completed run(%s)做了门禁/代码动作但无 VERDICT/测试结果/证据摘要,"
                          "标记 evidence_missing(去重冷却,不无限 rerun)" % ((missing.get("id") or "")[:8])}
    return {"status": "no_evidence", "anchor": None, "missing_run": None,
            "reason": "无任何门禁证据 run"}


def evidence_metadata_from_anchor(anchor):
    """PL-132:由有效证据锚点生成应 pin 的 issue metadata(供看门狗回写)。

    PASS:latest_valid_evidence_run_id / _commit / _at + gate_status=pass。
    FAIL:同样回写锚点,gate_status=fail(让"最新有效证据=FAIL"可被后续 run 直接读到)。
    """
    if not anchor:
        return {}
    md = {
        "latest_valid_evidence_run_id": anchor.get("run_id") or "",
        "latest_valid_evidence_at": anchor.get("evidence_at") or "",
        "gate_status": anchor.get("gate_status") or "",
    }
    if anchor.get("commit"):
        md["latest_valid_evidence_commit"] = anchor["commit"]
    return md


# ─────────────────────────────────────────────────────────────────────────────
# U30 / X38:结构化 evidence ledger(按 issue 落盘,可追溯)
# ─────────────────────────────────────────────────────────────────────────────
LEDGER_DIR = os.environ.get("LINE_EVIDENCE_LEDGER_DIR", "/home/fleet/line-config/memory/.evidence")


# ─────────────────────────────────────────────────────────────────────────────
# X46  L4 证据 QA × L6 并发时序:截图上传延迟时不得立即把任务判 evidence_missing
#
# 一个 completed run 刚回写,附件(截图)可能还在上传/异步落库,本轮看门狗拉不到图片。
# 旧逻辑此刻直接判 evidence_missing,会把"图还在传"误报成"无证据"并触发返工。
# 给一个证据宽限窗口:completed run 距完成时间在 grace_min 内、且文本已声称有截图/URL
# (即确有上传意图)时,先不判 evidence_missing —— 等下一轮附件到位再核。
# ─────────────────────────────────────────────────────────────────────────────
def evidence_grace_active(run_finished_iso, now, grace_min, claims_artifact=True):
    """X46:某 completed run 是否仍在『截图/附件上传宽限』窗口内。

    入参:
      run_finished_iso : run 完成时间(ISO,通常 finished_at/updated_at/created_at)
      now              : 当前时间(datetime)
      grace_min        : 宽限分钟数(<=0 关闭宽限,行为同旧逻辑)
      claims_artifact  : 该 run 文本是否声称有截图/URL(只有声称要传图才给宽限;
                         纯空结果不属于"上传延迟",不应被宽限掩盖)

    返回 True 表示『还在上传宽限期,本轮先别判 evidence_missing』。
    """
    if not grace_min or grace_min <= 0:
        return False
    if not claims_artifact:
        return False
    t = _parse_ts(run_finished_iso)
    if t is None or now is None:
        return False
    elapsed = (now - t).total_seconds() / 60.0
    return 0 <= elapsed <= grace_min


def claims_artifact_pending(text):
    """X46 辅助:文本是否声称有截图/URL(有上传意图)。用于决定是否给上传宽限。"""
    t = text or ""
    return bool(_SHOT_WORD_RE.search(t) or _IMG_EXT_RE.search(t) or extract_urls(t))


# ─────────────────────────────────────────────────────────────────────────────
# X48  L4 证据 QA × L7 持久化:证据 hash / 附件 hash 必须可重算
#
# ledger 里记录证据时,只存"有没有截图/URL"这类布尔标记,无法事后证明"当时的证据正文
# 没被篡改/和现在是不是同一份"。给证据正文与附件元数据各算一个稳定 sha256,写进 ledger;
# 任何时候用同样输入都能重算出同样 hash 来核对(可重算 = 可追责)。
# ─────────────────────────────────────────────────────────────────────────────
def _norm_text(text):
    """归一化文本用于稳定 hash:统一换行、去首尾空白(避免无意义空白导致 hash 抖动)。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def evidence_hash(text):
    """X48:对证据正文算可重算的 sha256(归一化后)。空文本返回空串的 hash(稳定)。"""
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


def attachment_hash(attachment):
    """X48:对单个附件的稳定标识算 sha256。

    优先用平台内容指纹(checksum/sha256/digest);没有时退化到 (id|filename|content_type|size)
    的规范串 —— 同一附件每次都得到同样 hash,可作 ledger 里的可重算指纹。
    """
    a = attachment or {}
    fp = a.get("checksum") or a.get("sha256") or a.get("digest")
    if fp:
        basis = "fp:%s" % fp
    else:
        basis = "meta:%s|%s|%s|%s" % (
            a.get("id") or "", a.get("filename") or a.get("url") or "",
            a.get("content_type") or "", a.get("size_bytes") or a.get("size") or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def attachments_digest(attachments):
    """X48:对一组附件算与顺序无关的聚合 sha256(每个附件 hash 排序后再 hash)。

    返回 {count, items:[各附件hash], digest}。空列表 digest 也稳定(空集合的 hash)。
    """
    hs = sorted(attachment_hash(a) for a in (attachments or []))
    agg = hashlib.sha256("|".join(hs).encode("utf-8")).hexdigest()
    return {"count": len(hs), "items": hs, "digest": agg}


def evidence_fingerprint(text, attachments=None):
    """X48:证据(正文 + 附件)的可重算指纹,写进 ledger 供事后核对。

    返回 {evidence_sha256, attachments_sha256, attachment_count}。
    """
    return {
        "evidence_sha256": evidence_hash(text),
        "attachments_sha256": attachments_digest(attachments)["digest"],
        "attachment_count": len(attachments or []),
    }


def fingerprint_matches(record, text, attachments=None):
    """X48:用同样输入重算指纹,核对是否与 ledger 里存的一致(证据未被篡改/同一份)。

    record 须含 evidence_sha256 / attachments_sha256。返回
    {evidence_match, attachments_match, match}。
    """
    fp = evidence_fingerprint(text, attachments)
    ev_ok = record.get("evidence_sha256") == fp["evidence_sha256"]
    at_ok = record.get("attachments_sha256") == fp["attachments_sha256"]
    return {"evidence_match": ev_ok, "attachments_match": at_ok,
            "match": ev_ok and at_ok}


def _ledger_entry(run):
    txt, pr = _result_text(run)
    combo = (txt + " " + pr).strip()
    gh = parse_github_refs(combo)
    art = verify_artifacts(combo)
    # B-U31:优先用 run 的作者 agent 身份判来源(可信门禁/审计 agent),
    # 文本关键词只作兜底;自报文本不再被误升级为可信 qwen_gate。
    agent_id = ""
    if isinstance(run, dict):
        agent_id = str(run.get("agent_name") or run.get("agent")
                       or run.get("author") or "").strip()
    src = classify_source(combo, agent=agent_id)
    verdict = None
    if re.search(r"VERDICT[:：\s]*PASS", combo, re.IGNORECASE):
        verdict = "PASS"
    elif re.search(r"VERDICT[:：\s]*FAIL", combo, re.IGNORECASE):
        verdict = "FAIL"
    tech_only = bool(re.search(r"(build|typecheck|tsc|编译|代码核查|lint)", combo, re.IGNORECASE)) \
        and not (art["urls"] or art["claims_screenshot"])
    # X48:证据(正文 + 附件)的可重算指纹,事后可用同样输入重算核对未被篡改。
    fp = evidence_fingerprint(combo, run.get("attachments") if isinstance(run, dict) else None)
    return {
        "run_id": run.get("id"),
        "created_at": run.get("created_at"),
        "status": run.get("status"),
        "source": src,                       # X38:event source
        "trust": evidence_trust(src),
        "verdict": verdict,
        "has_url": bool(art["urls"]),
        "claims_screenshot": art["claims_screenshot"],
        "pr_urls": [p["url"] for p in gh["pr_urls"]],
        "commits": gh["commits"],
        "tech_only": tech_only,
        "evidence_sha256": fp["evidence_sha256"],          # X48
        "attachments_sha256": fp["attachments_sha256"],    # X48
        "attachment_count": fp["attachment_count"],        # X48
    }


def build_ledger(issue_id, ident, runs, now=None):
    """U30:把一个 issue 的关键 run 的证据规整成结构化 ledger(可写盘/可追溯)。"""
    now = now or datetime.now(timezone.utc)
    entries = [_ledger_entry(r) for r in (runs or [])]
    return {
        "issue_id": issue_id,
        "identifier": ident,
        "generated_at": now.isoformat(),
        "entry_count": len(entries),
        "entries": entries,
    }


def build_cycle_ledger(now, scanned, alerts, scan_state, route_results=None,
                       gate_status=None, canary_pct=None):
    """U51:每轮(cron 周期)生成一份形式化 evidence ledger,后续变更可追责。

    与 build_ledger(per-issue)互补:这条是 per-cycle 全局快照 —— 本轮扫了多少、出了哪些
    kind 的告警各几条、扫描是否完整(full/partial-scan)、派单结果、门禁/灰度状态。
    纯函数;落盘由 write_cycle_ledger 负责。
    """
    now = now or datetime.now(timezone.utc)
    kind_counts = {}
    for a in alerts or []:
        k = a.get("kind") if isinstance(a, dict) else str(a)
        kind_counts[k] = kind_counts.get(k, 0) + 1
    routed_ok = routed_fail = 0
    for r in route_results or []:
        if (r.get("ok") if isinstance(r, dict) else False):
            routed_ok += 1
        else:
            routed_fail += 1
    return {
        "cycle_at": now.isoformat(),
        "scanned": int(scanned or 0),
        "scan_state": scan_state or "full",
        "alert_total": len(alerts or []),
        "alert_kinds": kind_counts,
        "routed_ok": routed_ok,
        "routed_fail": routed_fail,
        "gate_status": gate_status,
        "canary_pct": canary_pct,
    }


def write_cycle_ledger(entry, ledger_dir=None, keep=200):
    """U51:把每轮 cycle ledger 追加到 <dir>/cycles.jsonl(滚动保留最近 keep 行)。返回路径或 None。"""
    d = ledger_dir or LEDGER_DIR
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "cycles.jsonl")
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if keep and len(lines) > keep:
            lines = lines[-keep:]
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def write_ledger(ledger, ledger_dir=None):
    """把 ledger 落盘为 <dir>/<ident-or-id>.json。返回路径或 None。"""
    d = ledger_dir or LEDGER_DIR
    try:
        os.makedirs(d, exist_ok=True)
        key = ledger.get("identifier") or ledger.get("issue_id") or "unknown"
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(key))
        path = os.path.join(d, "%s.json" % safe)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# U25 / U26:Qwen / DeepSeek endpoint 探针(可注入 prober)
# ─────────────────────────────────────────────────────────────────────────────
def _default_endpoint_prober(url, timeout=6):
    """默认 endpoint 探针:对 base/health 或 base 发 GET,2xx/3xx/4xx 都算"活着"
    (能建连并回 HTTP 即视为在线;5xx/连不上视为故障)。返回 (ok, info)。"""
    base = url.rstrip("/")
    target = base + "/health" if not base.endswith(("/health", "/chat", "/v1")) else base
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = getattr(r, "status", 200)
            return (code < 500), "HTTP %s" % code
    except urllib.error.HTTPError as e:
        return (e.code < 500), "HTTP %s" % e.code
    except Exception as e:
        return False, str(e)[:120]


def probe_endpoint(name, url, prober=None):
    """U25/U26:探一个模型 endpoint。url 为空 -> 配置缺失(故障)。返回 dict。"""
    if not url:
        return {"name": name, "url": "", "ok": False, "info": "endpoint 未配置(env 缺失)"}
    p = prober or _default_endpoint_prober
    ok, info = p(url)
    return {"name": name, "url": url, "ok": bool(ok), "info": info}


def probe_model_endpoints(env, prober=None):
    """探 Qwen + DeepSeek(从 env 取 *_API_URL)。返回 [dict,...]。"""
    return [
        probe_endpoint("Qwen门禁", env.get("QWEN_API_URL", ""), prober),
        probe_endpoint("DeepSeek深挖", env.get("DEEPSEEK_API_URL", ""), prober),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# U27:真机 / adb / scrcpy 可用性探针(可注入 runner)
# ─────────────────────────────────────────────────────────────────────────────
def _default_device_runner(timeout=8):
    """默认真机探针:adb devices 解析在线设备数。返回 (online_count, raw)。"""
    try:
        p = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=timeout)
        lines = [l for l in (p.stdout or "").splitlines()[1:] if l.strip()]
        online = [l for l in lines if l.split("\t")[-1].strip() == "device"]
        return len(online), (p.stdout or "")[:300]
    except Exception as e:
        return -1, str(e)[:160]


def probe_device(runner=None):
    """U27:真机在线探针。返回 dict(ok, online, info)。

    online == -1 表示 adb 不可用(工具缺失);0 表示无设备在线;>0 在线。
    """
    r = runner or _default_device_runner
    online, raw = r()
    if online < 0:
        return {"ok": False, "online": online, "info": "adb 不可用:%s" % raw}
    if online == 0:
        return {"ok": False, "online": 0, "info": "无真机在线(adb devices 空),真机测试会假空"}
    return {"ok": True, "online": online, "info": "%d 台真机在线" % online}


def health_probe_alerts(env, endpoint_prober=None, device_runner=None, want_device=False):
    """把 U25/U26(/U27)探针结果转成统一告警三元组列表 (kind, label, why)。

    want_device=True 时才探真机(adb),否则只探模型 endpoint(避免无真机环境噪声)。
    纯收集,不贴台 —— 由 line_watchdog.py 决定是否 post/route。
    """
    out = []
    for ep in probe_model_endpoints(env, endpoint_prober):
        if not ep["ok"]:
            out.append(("probe_down", ep["name"],
                        "%s endpoint 不可用(%s):门禁/深挖会堆积,需修复或降级" % (ep["name"], ep["info"])))
    if want_device:
        dev = probe_device(device_runner)
        if not dev["ok"]:
            out.append(("device_offline", "真机",
                        "真机探针失败(%s):'真机测试'可能长期假空" % dev["info"]))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# U37:PL-94 告警台失败兜底(本地状态文件通道)
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK_F = os.environ.get("WATCHDOG_FALLBACK_F",
                            "/home/fleet/line-config/watchdog_fallback_alerts.jsonl")


def fallback_record(body, reason, now=None, host=None):
    """构造一条兜底告警记录(纯函数,便于单测)。"""
    now = now or datetime.now(timezone.utc)
    return {
        "ts": now.isoformat(),
        "host": host or _safe_hostname(),
        "channel_failed": "PL-94",
        "reason": (reason or "")[:300],
        "body": (body or "")[:4000],
    }


def _safe_hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def append_fallback(body, reason, path=None, now=None):
    """U37:PL-94 贴台失败时,把告警追加到本地 JSONL,保证告警不丢。返回路径或 None。"""
    p = path or FALLBACK_F
    rec = fallback_record(body, reason, now=now)
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return p
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 简单 CLI:打印一次健康探针(运维排查用,默认只探 endpoint,不触真机)
# ─────────────────────────────────────────────────────────────────────────────
def _load_env():
    e = dict(os.environ)
    envf = "/home/fleet/agent-control-plane/.control/.env"
    try:
        for line in open(envf, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            e.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception:
        pass
    return e


def _main(argv):
    if len(argv) >= 2 and argv[1] == "probe":
        env = _load_env()
        want_dev = "--device" in argv
        for ep in probe_model_endpoints(env):
            print("  [%s] %s -> %s (%s)" % (
                "OK" if ep["ok"] else "DOWN", ep["name"], ep["url"] or "(unset)", ep["info"]))
        if want_dev:
            dev = probe_device()
            print("  [%s] 真机 -> %s" % ("OK" if dev["ok"] else "DOWN", dev["info"]))
        alerts = health_probe_alerts(env, want_device=want_dev)
        print("HEALTH_ALERTS=%d" % len(alerts))
        for kind, label, why in alerts:
            print("  - [%s/%s] %s" % (kind, label, why))
        return 0
    print("用法: line_evidence.py probe [--device]")
    print("(本模块主要作为库被 line_watchdog.py import;判定函数全部可离线单测)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
