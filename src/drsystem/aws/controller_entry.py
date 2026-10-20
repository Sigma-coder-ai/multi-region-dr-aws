"""Lambda entry point for the DR controller. Heavy imports happen at INIT, not per tick."""

import boto3  # noqa: F401

from ..orchestrator import controller, workflows  # noqa: F401
from . import client, events_sink, notify_sns, replication, s3store, settings, sigv4  # noqa: F401
from .lambda_handlers import controller_tick_handler

handler = controller_tick_handler
