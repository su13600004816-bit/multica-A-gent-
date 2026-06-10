#!/usr/bin/env bash
# 一键安装 PL1 线看门狗 systemd timer(每2分钟巡查 T01/T02/T03,告警贴 PL-94 并派单给危机处理员)
# 需 root:  sudo bash /home/fleet/line-config/systemd/install.sh
set -e
SRC=/home/fleet/line-config/systemd
cp "$SRC/line-watchdog.service" /etc/systemd/system/line-watchdog.service
cp "$SRC/line-watchdog.timer"   /etc/systemd/system/line-watchdog.timer
# X63:进程外心跳 watcher —— 看门狗自身 cron/进程死掉时它仍独立巡查,停摆即落 fallback 告警 + 非 0 退出码。
cp "$SRC/line-watchdog-heartbeat.service" /etc/systemd/system/line-watchdog-heartbeat.service
cp "$SRC/line-watchdog-heartbeat.timer"   /etc/systemd/system/line-watchdog-heartbeat.timer
systemctl daemon-reload
systemctl enable --now line-watchdog.timer
systemctl enable --now line-watchdog-heartbeat.timer
echo "=== 已启用,定时器状态: ==="
systemctl list-timers line-watchdog.timer line-watchdog-heartbeat.timer --all
