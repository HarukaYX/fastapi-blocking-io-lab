FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /work
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

COPY pyproject.toml ./
RUN uv sync --no-dev

COPY app ./app
COPY scripts ./scripts

ENV PATH="/work/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
