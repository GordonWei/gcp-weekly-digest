# GCP Weekly Digest — Cloud Run Job
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 兩個檔案都要複製。account_context.py 是 2026-09-01 新增的模組，而
# main.py 第 41 行的 `import account_context` 是**頂層、沒有 try 包**——
# 少複製它的話容器一啟動就 ImportError，不是「新功能沒作用」而是**整份週報停掉**。
# 加新模組時記得回來補這裡。
COPY main.py account_context.py ./

# Cloud Run Job：容器啟動即執行一次到完成，無需常駐 HTTP Server
ENTRYPOINT ["python", "main.py"]
