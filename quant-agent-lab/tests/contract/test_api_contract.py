from fastapi.testclient import TestClient

from quant_agent.api.app import create_app


def test_api_and_service_share_the_same_closed_loop(service):
    client = TestClient(create_app(service))
    assert client.get("/api/v1/health").json()["live_broker"] == "disabled"
    generated = client.post(
        "/api/v1/daily-plans", json={"date": "2026-08-11", "request_id": "contract-plan"}
    )
    assert generated.status_code == 200
    report_id = generated.json()["report_id"]
    approved = client.post(
        f"/api/v1/reports/{report_id}/approve",
        json={"all": True, "request_id": "contract-approval"},
    )
    assert approved.status_code == 200
    executed = client.post(
        f"/api/v1/reports/{report_id}/execute",
        json={"mode": "paper", "request_id": "contract-execution"},
    )
    assert executed.status_code == 200
    assert executed.json()["mode"] == "paper"

    dashboard = client.get(f"/api/v1/dashboard?report_id={report_id}")
    assert dashboard.status_code == 200
    assert dashboard.json()["schema_version"] == "dashboard.v1"
    assert dashboard.json()["paper_only"] is True
    assert dashboard.json()["execution"]["execution_id"] == executed.json()["execution_id"]

    audit = client.get(f"/api/v1/audit?report_id={report_id}&limit=20")
    assert audit.status_code == 200
    assert any(event["request_id"] == "contract-approval" for event in audit.json())


def test_api_errors_have_stable_codes(service):
    client = TestClient(create_app(service))

    response = client.get("/api/v1/reports/missing")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REPORT_NOT_FOUND"


def test_research_v2_contract_is_available_without_trade_side_effects(service):
    client = TestClient(create_app(service))
    chart = client.post("/api/v2/chart-data", json={"symbol": "AAPL", "timeframe": "1d"})
    assert chart.status_code == 200
    assert chart.json()["schema_version"] == "chart-data.v2"
    assert chart.json()["is_synthetic"] is True
    assert isinstance(chart.json()["bars"][0]["close"], str)

    strategies = client.get("/api/v2/strategies")
    assert strategies.status_code == 200
    assert any(
        item["strategy_id"] == "moving-average-demo" for item in strategies.json()["strategies"]
    )
    assert strategies.json()["python_runner"]["available"] is False

    debug = client.post(
        "/api/v2/strategies/debug",
        json={
            "strategy_id": "moving-average-demo",
            "symbol": "AAPL",
            "timeframe": "1d",
            "request_id": "api-debug",
        },
    )
    assert debug.status_code == 200
    assert debug.json()["status"] == "COMPLETED"

    backtest = client.post(
        "/api/v2/backtests",
        json={
            "strategy_id": "moving-average-demo",
            "symbol": "AAPL",
            "timeframe": "1d",
            "request_id": "api-backtest",
        },
    )
    assert backtest.status_code == 200
    assert backtest.json()["status"] == "COMPLETED"
    assert backtest.json()["assumptions"]
