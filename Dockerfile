FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir /models && curl --fail --silent --show-error --location --retry 3 \
    https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    -o /models/yunet.onnx
COPY smartkruzhok ./smartkruzhok
ENV DATA_DIR=/data FACE_MODEL=/models/yunet.onnx PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home bot && install -d -o bot -g bot -m 0700 /data
USER bot
CMD ["python", "-m", "smartkruzhok"]
