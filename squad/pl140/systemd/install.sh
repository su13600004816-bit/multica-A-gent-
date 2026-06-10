#!/usr/bin/env bash
# 一键安装 PL1 线看门狗 systemd timer(每2分钟巡查 T01/T02/T03,告警贴 PL-94 并派单给危机处理员)
# 需 root:  sudo bash /home/fleet/line-config/systemd/install.sh
set -e
SRC=/home/fleet/line-config/systemd
cp "$SRC/line-watchdog.service" /etc/systemd/system/line-watchdog.service
cp "$SRC/line-watchdog.timer"   /etc/systemd/system/line-watchdog.timer
systemctl daemon-reload
systemctl enable --now line-watchdog.timer
echo "=== 已启用,定时器状态: ==="
systemctl list-timers line-watchdog.timer --all
