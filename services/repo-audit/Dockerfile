FROM python:3.12-slim
RUN useradd --create-home --uid 10001 audit
WORKDIR /worker
COPY repo_worker_v1.py /worker/repo_worker_v1.py
USER audit
ENTRYPOINT ["python", "/worker/repo_worker_v1.py"]
