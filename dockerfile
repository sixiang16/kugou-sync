FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/
ENV DOWNLOAD_DIR=/music \
    KUGOU_COOKIE="" \
    INTERVAL_MIN=60 \
    WEB_PORT=5000
VOLUME [ "/music" ]
EXPOSE 5000
CMD [ "python", "app.py" ]
