FROM python:3.12-slim-trixie
# Inter / Roboto Condensed / DejaVu Mono stand in for Apple's SF fonts (see render.FONTS)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-inter fonts-roboto-unhinted fonts-dejavu-mono snmp \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv==0.12.3
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY templates ./templates-default
RUN uv sync --frozen --no-dev
ENV PATH=/venv/bin:$PATH PTLABEL_TEMPLATES=/data/templates PYTHONUNBUFFERED=1
EXPOSE 8765
# seed the template volume on first run, then serve on all interfaces
CMD ["sh", "-c", "mkdir -p /data/templates && [ -n \"$(ls -A /data/templates)\" ] || cp templates-default/*.json /data/templates/; exec ptlabel serve --host 0.0.0.0 --port 8765"]
