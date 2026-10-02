from __future__ import annotations


def test_ai_poc_route_is_disabled_by_default(client):
    response = client.post(
        "/api/v1/ai-poc/dynatrace/problems",
        json={"instance": "Dynatrace-POC", "hostname": "MW10"},
    )
    assert response.status_code == 503
    assert "ENABLE_AI_POC=true" in response.json()["detail"]
