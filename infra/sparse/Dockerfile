FROM python:3.12-slim
WORKDIR /service
COPY infra/sparse/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/sparse/server.py .
ENV HF_HUB_CACHE=/data OMP_NUM_THREADS=2 SPARSE_THREADS=2 HF_HUB_DISABLE_XET=1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "80", "--limit-concurrency", "4"]
