FROM python:3.9-slim
WORKDIR /app

# 安装系统依赖（如果 copyKgSong 需要编译某些库则保留，否则可省略）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc-dev libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（将 copyKgSong 的依赖也加入主 requirements.txt）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY app.py .
COPY templates/ templates/
COPY copyKgSong/ copyKgSong/      # ⬅️ 新增这一行，放入 copyKgSong 整个目录

# 环境变量
ENV DOWNLOAD_DIR=/music \
    WEB_PORT=5000 \
    INTERVAL_MIN=60

# 数据卷（保存歌单配置）
VOLUME [ "/music", "/app/data" ]
EXPOSE 5000

CMD [ "python", "app.py" ]
