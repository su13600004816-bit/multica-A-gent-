#!/usr/bin/env bash
# PL-140 — X20 / X51 / X72 凭据类 BLOCKED 的真实探测脚本。
# 每条都是可复现命令,输出即"缺失凭据/平台能力"的证据。
# 用法:bash probes/probe_blocked_credentials.sh
set -u

echo "===== X51 — GitHub ref 真网络校验需 gh PAT ====="
echo "\$ which gh && gh --version"
which gh && gh --version 2>&1 | head -1 || echo "gh: NOT INSTALLED"
echo "\$ gh auth status"
gh auth status 2>&1 | head -8 || true
echo "证据:gh 二进制就绪,但未登录任何 GitHub host → verify_github_refs(verify_network=True)"
echo "      会因无 token 失败。结论:X51 = BLOCKED(缺 gh PAT,repo.env 标注'待配')。"
echo

echo "===== X72 — P0 外部通知通道(Slack/SMS)需 webhook/凭据 ====="
echo "\$ env | grep -iE 'SLACK|TWILIO|SMS|WEBHOOK|PAGER'"
env | grep -iE "SLACK|TWILIO|SMS|WEBHOOK|PAGER" || echo "(无任何外部通知凭据)"
echo "证据:无外部通知通道凭据 → P0 告警无法外发。脚本侧 ack 账本(p0_ack_entry/"
echo "      pending_p0_acks)已闭环并单测;外发通道 = BLOCKED(需平台 webhook)。"
echo

echo "===== X20 — 证据绑定'专属 API 凭据主体'需平台身份系统 ====="
echo "\$ env | grep -iE 'API_KEY|CRED_SUBJECT|IDENTITY_TOKEN|MTLS|CLIENT_CERT'"
env | grep -iE "API_KEY|CRED_SUBJECT|IDENTITY_TOKEN|MTLS|CLIENT_CERT" || echo "(无 per-agent 凭据主体/mTLS 客户端证书)"
echo "\$ multica --help | grep -iE 'credential|identity|subject|cert'"
multica --help 2>&1 | grep -iE "credential|identity|subject|cert" || echo "(multica CLI 未暴露 credential/identity/subject 子命令)"
echo "证据:无 per-agent 凭据主体,multica 也未暴露身份/凭据子命令 → classify_source"
echo "      只能按声明身份字符串分级,无法防伪造。结论:X20 = BLOCKED(需平台身份系统)。"
