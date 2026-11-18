import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from app.main import app, get_workforce_repo, PRODUCTS
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository
from app.security import current_principal

@pytest.fixture
def principal():
    return Principal("test-user","tenant-test","founder",frozenset({"*"}))

@pytest.fixture
def install_test_repository(principal):
    repository=InMemoryWorkforceRepository(principal,set(PRODUCTS))
    async def override_repo(p=Depends(current_principal)):
        return repository
    app.dependency_overrides[get_workforce_repo]=override_repo
    app.dependency_overrides[current_principal]=lambda: principal
    app.state.test_repository=repository
    yield repository
    app.dependency_overrides.clear()
    if hasattr(app.state,"test_repository"): delattr(app.state,"test_repository")

@pytest.fixture
async def client(install_test_repository):
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        yield c
