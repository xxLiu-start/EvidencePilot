"""Celery application for durable research task execution."""
from celery import Celery

from app.config import settings


celery_app = Celery(
    "evidencepilot",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
    timezone="UTC",
    enable_utc=True,
)
