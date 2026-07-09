"""Flask integration tests for the /recommend_sections endpoint."""

import pytest

import app.server as _server_module
from app.server import app, AVAILABLE_MODELS

VALID_VIEWS = {"x-", "x+", "y-", "y+", "z-", "z+"}


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_success_response_shape(client):
    """POST /recommend_sections returns 200 with the documented JSON schema."""
    response = client.post("/recommend_sections", json={})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert isinstance(body["model_index"], int)
    assert isinstance(body["sections"], list)
    assert isinstance(body["analysis_ms"], (int, float))
    for section in body["sections"]:
        assert section["view"] in VALID_VIEWS
        assert isinstance(section["depth"], int)
        assert 0 <= section["depth"] <= 100
        assert isinstance(section["label"], str) and section["label"]
        assert isinstance(section["covers"], list)


def test_sleeve_washer_gets_tunnel_label(client):
    """A model with a genuine through-hole gets at least one section labelled 'tunnel'."""
    washer_index = next(
        (i for i, p in enumerate(AVAILABLE_MODELS) if "sleeve_washer" in p.name),
        None,
    )
    if washer_index is None:
        pytest.skip("sleeve_washer.stl not found in AVAILABLE_MODELS")

    response = client.post("/recommend_sections", json={"current_model": washer_index})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"

    labels = [s["label"] for s in body["sections"]]
    assert any("tunnel" in label for label in labels), (
        f"Expected a tunnel section for sleeve_washer.stl; got sections: {body['sections']}"
    )


def test_fail_loud_503_when_algo_unavailable(client, monkeypatch):
    """If the algo package fails to import, the endpoint returns 503 — never silent degradation."""
    monkeypatch.setattr(_server_module, "_CROSS_SECTION_ALGO_AVAILABLE", False)
    monkeypatch.setattr(
        _server_module,
        "_CROSS_SECTION_ALGO_ERROR",
        "Tunnel detection unavailable: could not import skan",
    )
    response = client.post("/recommend_sections", json={})
    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "error"
    assert body["message"], "Error response must include a non-empty message"
