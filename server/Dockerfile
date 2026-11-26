# 仅用于「本地容器构建与联调」；生产由微信云托管托管，不自建服务器（架构 D1/§8.1）
# 构建上下文必须是仓库根目录（需要 packages/shared 契约）：
#   docker build -f server/Dockerfile -t voice-inventory-lite:local .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=prod \
    CONTRACTS_DIR=/app/packages/shared \
    PORT=8080

WORKDIR /app

# 先装依赖再拷代码，最大化利用层缓存
COPY server/pyproject.toml /app/pyproject.toml
COPY server/README.md /app/README.md
RUN mkdir -p /app/app && printf '__all__ = []\n' > /app/app/__init__.py \
    && pip install --upgrade pip \
    && pip install "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.29" "pydantic>=2.6,<3" "redis>=5,<7" \
       "sqlalchemy>=2.0.30,<3" "alembic>=1.13,<2" "psycopg[binary]>=3.1,<4" "PyJWT>=2.8,<3" \
       "cryptography>=42.0,<45" "httpx>=0.27,<1" "tzdata>=2024.1" \
    && rm -rf /app/app

# 服务端代码 + 迁移（生产建表走 `alembic upgrade head`，不再用 create_all）
COPY server/app /app/app
COPY server/migrations /app/migrations
COPY server/alembic.ini /app/alembic.ini
# 运维脚本：`scripts/probe_env.py` 要在容器里能直接跑（部署自检门禁）
COPY server/scripts /app/scripts
# 共享契约（单一真相源，只读挂载/拷贝，不复制出第二份）
COPY packages/shared /app/packages/shared

# 非 root 运行（最小权限）
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# 健康检查（云托管可据此探活）
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

# 云托管约定：监听容器端口。**用 8080，不用 80**——容器以非 root（uid 10001）运行，
# K8s（云托管底层）默认不允许非 root 绑 1024 以下端口，绑 80 会 `Permission denied`
# 然后 CrashLoop、且错误经常不进「启动日志」。在云托管把「服务端口」配成 8080、
# 「访问端口」配 80（或都用 8080）即可。
#
# 开头的 `[boot]` 三行是**体检日志**：先于任何业务代码打印，用于区分
# 「容器根本没跑到 shell」/「环境变量没注入」/「迁移失败」/「uvicorn 没起来」。
# 云托管某些情况下不会把 Python 的 traceback 显示在「启动日志」里，所以必须自己打点。
CMD ["sh", "-c", "echo \"[boot] pid=$$ port=${PORT:-8080} migration_url_set=${MIGRATION_DATABASE_URL:+yes} db_url_set=${DATABASE_URL:+yes}\"; if [ -n \"$MIGRATION_DATABASE_URL\" ]; then echo '[boot] running alembic upgrade head'; alembic upgrade head || { echo '[boot] alembic FAILED'; exit 1; }; echo '[boot] alembic done'; fi; echo '[boot] starting uvicorn'; exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --proxy-headers --forwarded-allow-ips '*'"]
