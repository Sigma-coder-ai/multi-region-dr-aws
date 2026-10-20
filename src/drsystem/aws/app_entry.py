"""Lambda entry point for the regional app. Builds the app at import (INIT phase, which gets a CPU
boost) so the first request does not pay for importing boto3/FastAPI/pydantic."""

from .lambda_handlers import _app_handler, app_handler

_app_handler()

handler = app_handler
