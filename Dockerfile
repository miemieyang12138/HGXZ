ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/python:3.13-slim
# 国内网络下官方 apt/pip 源极慢，构建时换用阿里云镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates tzdata openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
COPY HGXZ /app/HGXZ
ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai DAEMON=true SCHEDULE=03:30
EXPOSE 8099
ENTRYPOINT ["python", "-m", "HGXZ.cli"]
CMD ["--daemon"]
