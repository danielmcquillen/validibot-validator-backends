"""
Shared pytest fixtures for validibot-validators tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_run_id() -> str:
    """Sample validation run ID for testing."""
    return "run_abc123"


@pytest.fixture
def sample_org_id() -> str:
    """Sample organization ID for testing."""
    return "org_xyz789"


@pytest.fixture
def sample_callback_url() -> str:
    """Sample callback URL for testing."""
    return "https://validibot.example.com/api/v1/validations/callback/"


@pytest.fixture
def sample_execution_bundle_uri(sample_org_id: str, sample_run_id: str) -> str:
    """Sample GCS execution bundle URI for testing."""
    return f"gs://test-bucket/{sample_org_id}/{sample_run_id}"
