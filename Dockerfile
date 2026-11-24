FROM python:3.12-slim AS frontend
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1
COPY requirements-frontend.txt ./
RUN pip install --no-cache-dir -r requirements-frontend.txt
COPY frontend ./frontend
COPY .streamlit ./.streamlit
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "frontend/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]

FROM python:3.12-slim AS api
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false
# CPU inference only. The main model is an independent service.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt
RUN python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Remote-only business runtime. The legacy api target above remains available.
FROM python:3.12-slim AS business
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 INFERENCE_MODE=remote TIKTOKEN_CACHE_DIR=/opt/tiktoken
COPY requirements-business.txt ./
RUN pip install --no-cache-dir -r requirements-business.txt \
    && mkdir -p /opt/tiktoken /workspace/data/cache/web \
    && python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")' \
    && groupadd -g 10001 ep && useradd -u 10001 -g ep -M ep \
    && chown -R ep:ep /workspace/data /opt/tiktoken
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim AS inference
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false DATABASE_URL=sqlite://
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements-inference.txt ./
RUN pip install --no-cache-dir -r requirements-inference.txt \
    && groupadd -g 10001 ep && useradd -u 10001 -g ep -M ep
COPY app/config.py ./app/config.py
COPY app/retrieval ./app/retrieval
COPY app/inference_service.py ./app/inference_service.py
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.inference_service:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
