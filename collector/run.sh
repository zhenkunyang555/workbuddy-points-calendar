#!/bin/sh
# magpie 守护容器入口
#   每 5 分钟：guard.py（顺序守护 + 用量记账）、valve.py（安全阀）
#   每 15 分钟：claim.py（领徽章奖励）、history.py（消耗曲线）、policy.py（账号开关）、report.py（页面）
#   每 60 分钟：extra.py（徽章墙 / 抽奖 / 能量）
#   每 3 小时：tasks.py（任务中心、派猫、连登兑换）
# 页面对外：http://<NAS>:3431/report.html
# 注意顺序就是数据流：policy 必须在 report 之前跑，否则页面上的“消耗调度”晚一档（15 分钟）。
python3 -m http.server 8080 --directory /app >/dev/null 2>&1 &
python3 /app/today.py >/dev/null 2>&1 &   # 每 5 秒：今天每个账号花了多少 → today.json（页面里轮询它）
n=0
while true; do
  python3 /app/guard.py
  python3 /app/valve.py
  if [ $((n % 36)) -eq 0 ]; then
    python3 /app/tasks.py
  fi
  if [ $((n % 12)) -eq 0 ]; then
    python3 /app/extra.py
  fi
  if [ $((n % 3)) -eq 0 ]; then
    python3 /app/claim.py
    python3 /app/history.py
    python3 /app/policy.py
    python3 /app/report.py
  fi
  n=$((n + 1))
  sleep 300
done
