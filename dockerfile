FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sync.py .
ENV DOWNLOAD_DIR=/music \
    KUGOU_COOKIE="" \
    INTERVAL_MIN=60 \
    PLAYLIST_IDS="" \
    PLAYLIST_NAMES=""
VOLUME [ "/music" ]
CMD [ "python", "sync.py" ]
