# WorkBuddy 积分采集与报告页
#
# 单容器：python:3.12-alpine + 这套采集脚本，HTTP 服务直接把 /app 当站点根，
# 于是 http://<nas>:3431/report.html 就是报告页、/report.json 是原始数据。
# 没有任何第三方依赖（全部标准库），所以镜像很小、构建不需要联网装包。
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="workbuddy-points-calendar collector" \
      org.opencontainers.image.description="采集 magpie 的用量与积分批次，生成 WorkBuddy 积分到期报告页" \
      org.opencontainers.image.source="https://github.com/zhenkunyang555/workbuddy-points-calendar" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    APP_DIR=/app \
    PORT=8080

WORKDIR /opt/collector
COPY collector/ /opt/collector/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    # 构建期就把语法错误挡住：这些脚本一挂，容器起来也不会自己好
    && python -m py_compile /opt/collector/*.py

# 状态与产物都落在 /app（report.json / report.html / today.json / state.json / web.key）
VOLUME ["/app"]
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
