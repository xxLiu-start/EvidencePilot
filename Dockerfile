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
