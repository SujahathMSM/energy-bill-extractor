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
    assert "Turn an electricity bill into checked, structured data" in response.text
    assert "/static/logo.svg" in response.text


def test_health_reports_upload_limits(client):
    limits = client.get("/health").json()["limits"]
    assert limits == {"max_upload_mb": 1.0, "max_pages": 3}


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/logo.svg", "image/svg+xml"),
        ("/static/samples/bill_000.pdf", "application/pdf"),
        ("/static/samples/bill_001.png", "image/png"),
        ("/static/samples/bill_002_photo.jpg", "image/jpeg"),
    ],
)
def test_static_assets_are_served(client, path, content_type):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


def test_demo_page_samples_extract(client):
    """Every sample button on the page points at a file the API can read."""
    import re

    page = client.get("/").text
    sources = re.findall(r'data-src="([^"]+)"', page)
    assert len(sources) == 3
    for src in sources:
        data = client.get(src).content
        response = client.post("/v1/bills/extract", files={"file": (src.split("/")[-1], data)})
        assert response.status_code == 200, src


def test_launcher_uses_port_from_settings(monkeypatch):
    import bill_extractor.__main__ as launcher

    calls = {}
    monkeypatch.setenv("BILLX_PORT", "8010")
    monkeypatch.setattr(launcher.uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    get_settings.cache_clear()
    try:
        launcher.main()
    finally:
        get_settings.cache_clear()
    assert calls == {"app": "bill_extractor.api:app", "host": "127.0.0.1", "port": 8010}
