FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install uv

WORKDIR /app

# Copy lockfile and pyproject
COPY pyproject.toml uv.lock ./

# Install dependencies via uv
RUN uv pip install --system -r pyproject.toml

# Copy the rest of the application
COPY . .

# Install the application itself
RUN uv pip install --system --no-deps -e .

EXPOSE 8080

ENTRYPOINT ["python", "-m", "maxwell_daemon.launcher"]
