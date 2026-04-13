import fakeredis
import pytest


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)
