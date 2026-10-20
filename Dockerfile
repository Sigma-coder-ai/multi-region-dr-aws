# Hosted demo of the SIMULATED two-region DR system (e.g. on Railway).
# Only the edge router + dashboard is public, on $PORT; the two "regions" stay on localhost inside the container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY config ./config
COPY web ./web

# PROJECT_ROOT resolves from the source tree, so run from it instead of site-packages
ENV PYTHONPATH=/app/src PORT=8100
EXPOSE 8100
CMD ["python", "-m", "drsystem", "sim"]
