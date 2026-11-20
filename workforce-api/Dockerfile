FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /workspace
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY workforce-api/ /workspace/workforce-api/
COPY data/ /workspace/data/
WORKDIR /workspace/workforce-api
RUN python -m pip install --upgrade pip && pip install .
RUN useradd --system --uid 10001 --create-home workforce
USER workforce
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
