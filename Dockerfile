FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /tmp/matplotlib

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data
ENV MPLCONFIGDIR=/tmp/matplotlib

CMD ["python", "bot.py"]
