import pytest
import pytest_asyncio
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from app.main import app as route_source_app, get_workforce_repo, get_adapter_repo, get_generic_adapter_repo, PRODUCTS
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository
from app.security import current_principal
from app.rate_limit import RateLimitMiddleware, reset_rate_limits

@pytest.fixture
def principal():
    return Principal("test-user","tenant-test","founder",frozenset({"*"}))

@pytest.fixture
def test_app(principal):
    repository=InMemoryWorkforceRepository(principal,set(PRODUCTS))
    instance=FastAPI()
    instance.include_router(route_source_app.router)
    instance.add_middleware(RateLimitMiddleware, use_local=True)
    async def override_principal():
        return principal
    async def override_repo(p=Depends(current_principal)):
        return repository
    instance.dependency_overrides[current_principal]=override_principal
    instance.dependency_overrides[get_workforce_repo]=override_repo
    async def override_adapter_repo():
        yield repository
    instance.dependency_overrides[get_adapter_repo]=override_adapter_repo
    instance.dependency_overrides[get_generic_adapter_repo]=override_adapter_repo
    instance.state.test_repository=repository
    yield instance
    instance.dependency_overrides.clear()

@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    reset_rate_limits()
    yield
    reset_rate_limits()

@pytest.fixture
def install_test_repository(test_app):
    return test_app.state.test_repository

@pytest_asyncio.fixture
async def client(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app),base_url="http://test") as c:
        yield c
