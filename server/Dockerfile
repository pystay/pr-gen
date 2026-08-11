FROM python:3.12-slim

WORKDIR /app

# 中文字体（PDF 报价单）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ /app/

ENV DATA_DIR=/app/data \
    MAIL_OUT_DIR=/app/mail_out \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
