import pytest
from fastapi.testclient import TestClient

from bill_extractor.api import app
from bill_extractor.config import get_settings


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BILLX_BACKEND", "mock")
    monkeypatch.setenv("BILLX_MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["backend"] == "mock"


def test_extract_pdf(client, bill):
    _, pdf = bill
    response = client.post(
        "/v1/bills/extract",
        files={"file": ("bill.pdf", pdf, "application/pdf")},
        headers={"X-Request-ID": "req-123"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-123"
    body = response.json()
    assert body["extraction"]["supplier_name"] == "Fernwick Energy"
    assert body["derived"]["period_days"] == 30
    assert body["needs_review"] is False
    assert body["meta"]["request_id"] == "req-123"
    assert body["meta"]["pages_sent"] == 1


def test_rejects_unsupported_file(client):
    response = client.post(
        "/v1/bills/extract", files={"file": ("bill.pdf", b"not a pdf", "application/pdf")}
    )
    assert response.status_code == 415


def test_rejects_empty_file(client):
    response = client.post(
        "/v1/bills/extract", files={"file": ("bill.pdf", b"", "application/pdf")}
    )
    assert response.status_code == 400


def test_rejects_oversized_file(client):
    big = b"%PDF" + b"0" * (1024 * 1024 + 10)
    response = client.post(
        "/v1/bills/extract", files={"file": ("bill.pdf", big, "application/pdf")}
    )
    assert response.status_code == 413


def test_demo_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Electricity bill extractor" in response.text
