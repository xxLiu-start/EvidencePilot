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
CMD ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Build business dependencies in a disposable development stage.
FROM cgr.dev/chainguard/python:latest-dev AS business-builder
WORKDIR /home/nonroot/workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TIKTOKEN_CACHE_DIR=/home/nonroot/workspace/data/tiktoken PATH="/home/nonroot/venv/bin:$PATH"
RUN python -m venv --without-pip /home/nonroot/venv \
    && mkdir -p /home/nonroot/workspace/data/cache/web /home/nonroot/workspace/data/tiktoken
COPY requirements-business.txt ./
RUN pip --python /home/nonroot/venv/bin/python install --no-cache-dir -r requirements-business.txt \
    && /home/nonroot/venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'

# Distroless Wolfi runtime: no Debian util-linux, perl, shell or package manager.
FROM cgr.dev/chainguard/python:latest AS business
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 INFERENCE_MODE=remote TIKTOKEN_CACHE_DIR=/workspace/data/tiktoken PATH="/home/nonroot/venv/bin:$PATH"
COPY --from=business-builder --chown=nonroot:nonroot /home/nonroot/venv /home/nonroot/venv
COPY --from=business-builder --chown=nonroot:nonroot /home/nonroot/workspace/data /workspace/data
COPY --chown=nonroot:nonroot app ./app
COPY --chown=nonroot:nonroot migrations ./migrations
COPY --chown=nonroot:nonroot alembic.ini ./alembic.ini
ENTRYPOINT ["/home/nonroot/venv/bin/python"]
EXPOSE 8000
CMD ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Install CPU-only PyTorch and inference dependencies in a throwaway builder.
FROM cgr.dev/chainguard/python:latest-dev AS inference-builder
WORKDIR /home/nonroot/workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/home/nonroot/venv/bin:$PATH"
RUN python -m venv --without-pip /home/nonroot/venv \
    && /home/nonroot/venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' \
    && python -m pip --python /home/nonroot/venv/bin/python install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision
COPY requirements-inference.txt ./
RUN python -m pip --python /home/nonroot/venv/bin/python install --no-cache-dir -r requirements-inference.txt \
    && /home/nonroot/venv/bin/python -c 'import torch, transformers, tokenizers, safetensors; print(torch.__version__, transformers.__version__, tokenizers.__version__)'

# Minimal non-root runtime containing only Python, application and installed wheels.
FROM cgr.dev/chainguard/python:latest AS inference
WORKDIR /workspace
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false DATABASE_URL=sqlite:// HF_HOME=/tmp/huggingface PATH="/home/nonroot/venv/bin:$PATH"
COPY --from=inference-builder /tmp/python-abi /tmp/python-abi
COPY --from=inference-builder --chown=nonroot:nonroot /home/nonroot/venv /home/nonroot/venv
RUN ["/usr/bin/python", "-c", "import sys; expected=open('/tmp/python-abi').read(); actual='%d.%d' % sys.version_info[:2]; assert actual == expected, (expected, actual)"]
COPY --chown=nonroot:nonroot app/config.py ./app/config.py
COPY --chown=nonroot:nonroot app/retrieval ./app/retrieval
COPY --chown=nonroot:nonroot app/inference_service.py ./app/inference_service.py
# Verify imports in the actual runtime image, not only in the builder.
RUN ["/home/nonroot/venv/bin/python", "-c", "import sys, torch, transformers, tokenizers, safetensors; from app.inference_service import app; assert sys.version_info[:2] == (3, 12); print('runtime inference imports verified', transformers.__version__, tokenizers.__version__)"]
ENTRYPOINT ["/home/nonroot/venv/bin/python"]
EXPOSE 8000
CMD ["-m", "uvicorn", "app.inference_service:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
