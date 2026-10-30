FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bookingbot \
    && useradd --uid 10001 --gid bookingbot --create-home bookingbot

COPY --chown=bookingbot:bookingbot booking_bot/ ./booking_bot/

USER bookingbot

ENTRYPOINT ["python", "-m", "booking_bot"]
