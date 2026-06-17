# syntax=docker/dockerfile:1.7
# BuildKit syntax enables --mount=type=cache, which lets apt and pip caches
# survive between builds. Editing one layer no longer forces re-downloading the
# whole world; only the actual diff is fetched.

FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps (rarely changes). BuildKit cache mounts keep the apt cache around
# between builds, so adding a single package later only downloads that package
# rather than re-fetching ffmpeg + the fonts each time.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        build-essential \
        fonts-dejavu \
        fonts-noto-color-emoji \
        fontconfig \
    && fc-cache -f

WORKDIR /app

# Python deps. Same trick — pip cache survives across builds. requirements.txt
# is copied alone first so editing app code never invalidates this layer.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=1000 -r requirements.txt

# App code last, since it changes the most often.
COPY . .

RUN mkdir -p /app/data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
