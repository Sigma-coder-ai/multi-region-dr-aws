import os

import pytest

from drsystem.config import load_settings
from drsystem.faults import InMemoryFaults
from drsystem.storage.filestore import FileObjectStore

# Belt and braces: tests must never reach real AWS.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.pop("AWS_PROFILE", None)
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull

HOME, DR = "ap-south-1", "ap-southeast-1"


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def stores(tmp_path):
    return {HOME: FileObjectStore(tmp_path / HOME, HOME), DR: FileObjectStore(tmp_path / DR, DR)}


@pytest.fixture
def faults():
    return {HOME: InMemoryFaults(), DR: InMemoryFaults()}
