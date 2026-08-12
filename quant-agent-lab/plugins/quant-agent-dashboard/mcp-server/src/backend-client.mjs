export class BackendError extends Error {
  constructor(status, payload, cause) {
    const message = payload?.detail?.message ?? payload?.detail ?? payload?.message ?? "backend request failed";
    super(String(message), { cause });
    this.name = "BackendError";
    this.status = status;
    this.payload = payload;
    this.code = payload?.detail?.code ?? (status === 0 ? "BACKEND_UNAVAILABLE" : "BACKEND_ERROR");
  }
}

export class BackendClient {
  constructor(baseUrl = process.env.QUANT_AGENT_BACKEND_URL ?? "http://127.0.0.1:8014") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async request(path, options = {}) {
    let response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...options,
        headers: {
          accept: "application/json",
          ...(options.body ? { "content-type": "application/json" } : {}),
          ...(options.headers ?? {}),
        },
      });
    } catch (error) {
      throw new BackendError(0, { message: "backend is unavailable" }, error);
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new BackendError(response.status, payload);
    }
    return payload;
  }

  getDashboard(reportId) {
    const query = reportId ? `?report_id=${encodeURIComponent(reportId)}` : "";
    return this.request(`/api/v1/dashboard${query}`);
  }

  generateDailyPlan({ date, request_id }) {
    return this.request("/api/v1/daily-plans", {
      method: "POST",
      body: JSON.stringify({ date, request_id }),
    });
  }

  getReport(reportId) {
    return this.request(`/api/v1/reports/${encodeURIComponent(reportId)}`);
  }

  submitApproval({ report_id, scope, order_ids, approver, request_id }) {
    return this.request(`/api/v1/reports/${encodeURIComponent(report_id)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        all: scope === "ALL",
        order_ids: scope === "PARTIAL" ? order_ids : [],
        approver,
        request_id,
      }),
    });
  }

  rejectPlan({ report_id, approver, request_id }) {
    return this.request(`/api/v1/reports/${encodeURIComponent(report_id)}/reject`, {
      method: "POST",
      body: JSON.stringify({ approver, request_id }),
    });
  }

  executePaperPlan({ report_id, request_id }) {
    return this.request(`/api/v1/reports/${encodeURIComponent(report_id)}/execute`, {
      method: "POST",
      body: JSON.stringify({ mode: "paper", request_id }),
    });
  }

  setKillSwitch({ enabled, reason, actor, request_id }) {
    return this.request("/api/v1/kill-switch", {
      method: "POST",
      body: JSON.stringify({ enabled, reason, actor, request_id }),
    });
  }

  getExecution(executionId) {
    return this.request(`/api/v1/executions/${encodeURIComponent(executionId)}`);
  }

  getAuditEvents({ report_id, limit = 100 }) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (report_id) query.set("report_id", report_id);
    return this.request(`/api/v1/audit?${query}`);
  }

  getChartData(payload) {
    return this.request("/api/v2/chart-data", { method: "POST", body: JSON.stringify(payload) });
  }

  listStrategies() {
    return this.request("/api/v2/strategies");
  }

  getStrategy(strategyId, version) {
    const query = version ? `?version=${encodeURIComponent(version)}` : "";
    return this.request(`/api/v2/strategies/${encodeURIComponent(strategyId)}${query}`);
  }

  validateStrategy(payload) {
    return this.request("/api/v2/strategies/validate", { method: "POST", body: JSON.stringify(payload) });
  }

  saveStrategyDraft(payload) {
    return this.request("/api/v2/strategies/drafts", { method: "POST", body: JSON.stringify(payload) });
  }

  runStrategyDebug(payload) {
    return this.request("/api/v2/strategies/debug", { method: "POST", body: JSON.stringify(payload) });
  }

  getDebugTrace(runId, start = 0, limit = 100) {
    return this.request(`/api/v2/debug/${encodeURIComponent(runId)}?start=${start}&limit=${limit}`);
  }

  runBacktest(payload) {
    return this.request("/api/v2/backtests", { method: "POST", body: JSON.stringify(payload) });
  }

  getBacktestResult(runId) {
    return this.request(`/api/v2/backtests/${encodeURIComponent(runId)}`);
  }

  compareBacktests(runIds) {
    return this.request("/api/v2/backtests/compare", { method: "POST", body: JSON.stringify({ run_ids: runIds }) });
  }

  promoteStrategyCandidate(payload) {
    return this.request("/api/v2/strategies/promote", { method: "POST", body: JSON.stringify(payload) });
  }

  enablePaperStrategy(payload) {
    return this.request("/api/v2/strategies/enable-paper", { method: "POST", body: JSON.stringify(payload) });
  }
}
