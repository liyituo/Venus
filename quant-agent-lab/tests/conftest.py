from datetime import UTC, datetime

import pytest

from quant_agent.infrastructure.clock import FrozenClock
from quant_agent.infrastructure.paths import ProjectPaths
from quant_agent.orchestration.service import ApplicationService


@pytest.fixture
def service(tmp_path):
    clock = FrozenClock(datetime(2026, 8, 11, 8, 0, tzinfo=UTC))
    application = ApplicationService(paths=ProjectPaths(tmp_path), clock=clock)
    application.seed_demo()
    return application


@pytest.fixture
def clock(service):
    return service.clock
