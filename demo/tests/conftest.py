import pytest
from httpx import AsyncClient, ASGITransport

from api.main import app
from api.services.link_state_service import reset_link_state_service


@pytest.fixture(autouse=True)
def reset_service():
    """Reset link state service before each test."""
    reset_link_state_service()
    yield
    reset_link_state_service()


@pytest.fixture
async def client():
    """Create async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
