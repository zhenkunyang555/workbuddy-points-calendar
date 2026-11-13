#!/bin/sh
# 容器入口：把代码铺进 /app（状态目录），注入钥匙，然后跑采集循环。
#
# 为什么代码和状态放同一个目录：脚本里写死了 Path("/app")（guard/valve/today/report 都这样），
# 状态 json、report.html 也生成在 /app。把代码铺进去 + 宿主机目录挂到 /app，
# 就同时拿到「状态持久化」和「代码可随镜像更新」。
set -eu

APP="${APP_DIR:-/app}"
SRC=/opt/collector
mkdir -p "$APP"

# 代码每次启动都覆盖（升级镜像即生效）；状态 json 不在 SRC 里，不会被碰
for f in "$SRC"/*.py "$SRC"/run.sh; do
  [ -f "$f" ] || continue
  cp -f "$f" "$APP/$(basename "$f")"
done

# magpie 的网页钥匙：不烤进镜像，用环境变量注入；脚本读 /app/web.key
if [ -n "${MAGPIE_WEB_KEY:-}" ]; then
  printf '%s' "$MAGPIE_WEB_KEY" > "$APP/web.key"
  chmod 600 "$APP/web.key"
fi

# 采集端要读 magpie 的账号令牌（只读挂载到 /cfg）
if [ ! -d /cfg/magpie ]; then
  echo "[entrypoint] 警告：/cfg/magpie 不存在，report.py 读不到账号令牌；" \
       "把 magpie 的配置目录只读挂到 /cfg（compose 里那行 volumes）" >&2
fi

exec sh "$APP/run.sh"
