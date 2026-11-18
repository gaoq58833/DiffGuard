from pathlib import Path

from fastapi.testclient import TestClient

from diffguard.app import app

PATCH = (Path(__file__).parent / "fixtures" / "sample.patch").read_text(encoding="utf-8")


def test_health_reports_model_map():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["models"]) >= {"security", "style", "perf"}
    assert body["max_concurrent_reviews"] >= 1


def test_review_endpoint_rule_only():
    with TestClient(app) as client:
        response = client.post(
            "/review",
            json={"diff": PATCH, "pr_title": "feat: 按 uid 查询用户", "use_llm": False},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "block"
    assert {i["rule_id"] for i in body["issues"]} >= {
        "SEC_SQL_INJECTION",
        "SEC_HARDCODED_SECRET",
        "SEC_EVAL_EXEC",
    }


def test_review_endpoint_rejects_empty_diff():
    with TestClient(app) as client:
        response = client.post("/review", json={"diff": ""})

    assert response.status_code == 422
