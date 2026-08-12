const UI_RESOURCE_URI = "ui://quant-agent-dashboard/dashboard.html";

const STATUS_LABELS = {
  DRAFT: "草稿",
  GENERATED: "已生成",
  RISK_BLOCKED: "风险阻断",
  PENDING_APPROVAL: "待审批",
  APPROVED: "已批准",
  PARTIALLY_APPROVED: "部分批准",
  REJECTED: "已拒绝",
  EXPIRED: "已过期",
  EXECUTING: "执行中",
  PARTIALLY_FILLED: "部分成交",
  FILLED: "已成交",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const ROUTE_VIEWS = {
  dashboard: "dashboard",
  chart: "chart",
  "strategy-lab": "lab",
  backtests: "lab",
  risk: "dashboard",
  audit: "dashboard",
};

function routeView() {
  const route = window.location.hash.replace(/^#\//, "") || "dashboard";
  return ROUTE_VIEWS[route] ?? "dashboard";
}

function routeForView(value) {
  return value === "chart" ? "chart" : value === "lab" ? "strategy-lab" : "dashboard";
}

function navigateView(value) {
  state.view = value;
  if (window.parent !== window) {
    window.parent.postMessage({ jsonrpc: "2.0", method: "ui/route", params: { hash: `#/${routeForView(value)}` } }, "*");
  } else {
    window.location.hash = `#/${routeForView(value)}`;
  }
  render();
  if (value === "chart" && !state.chart) loadChart();
  if (value === "lab" && !state.strategies.length) loadStrategies();
}

const state = {
  phase: "loading",
  dashboard: null,
  chart: null,
  strategies: [],
  strategy: null,
  strategyValidation: null,
  strategyError: null,
  debug: null,
  debugIndex: 0,
  backtest: null,
  backtestHistory: [],
  researchError: null,
  pendingTool: null,
  selected: new Set(),
  theme: "dark",
  technicalOpen: false,
  modal: null,
  view: routeView(),
  symbol: "AAPL",
  timeframe: "1d",
  chartZoom: 40,
  chartOffset: 0,
  chartHoverIndex: null,
  editorText: "",
  labParameters: {},
  requestSequence: 0,
};

const root = document.querySelector("quant-dashboard-app");

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  let valueToSet;
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "value") valueToSet = value;
    else if (key === "onClick") element.addEventListener("click", value);
    else if (key === "onChange") element.addEventListener("change", value);
    else if (key === "onInput") element.addEventListener("input", value);
    else if (key === "onKeyDown") element.addEventListener("keydown", value);
    else if (key === "onMouseMove") element.addEventListener("mousemove", value);
    else if (key === "onMouseLeave") element.addEventListener("mouseleave", value);
    else if (key === "checked") element.checked = Boolean(value);
    else if (key === "disabled") element.disabled = Boolean(value);
    else if (key === "ariaLabel") element.setAttribute("aria-label", value);
    else if (key === "dataTestid") element.setAttribute("data-testid", value);
    else if (key === "style") element.setAttribute("style", value);
    else if (value !== undefined && value !== null) element.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  if (valueToSet !== undefined) element.value = String(valueToSet);
  return element;
}

function svgNode(tag, attributes = {}, children = []) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") element.textContent = value;
    else if (value !== undefined && value !== null) element.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return element;
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function valueOrNA(value) {
  return value === null || value === undefined || value === "" ? "N/A" : String(value);
}

function number(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "N/A";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "N/A";
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits }).format(parsed);
}

function percentage(value, digits = 2) {
  if (value === "N/A" || value === null || value === undefined) return "N/A";
  return `${number(Number(value) * 100, digits)}%`;
}

function money(value, currency = "USD") {
  const formatted = number(value, 2);
  return formatted === "N/A" ? formatted : `${formatted} ${currency}`;
}

function dateTime(value) {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" }).format(parsed);
}

function statusLabel(value) { return STATUS_LABELS[value] ?? valueOrNA(value); }

function statusClass(value) {
  if (["RISK_BLOCKED", "FAILED", "EXPIRED", "CANCELLED"].includes(value)) return "danger";
  if (["PARTIALLY_FILLED", "PARTIALLY_APPROVED", "PENDING_APPROVAL"].includes(value)) return "warn";
  if (["APPROVED", "FILLED", "PAPER_ENABLED"].includes(value)) return "success";
  if (value === "PAPER_CANDIDATE") return "accent";
  return "neutral";
}

function pill(label, kind = "neutral") { return node("span", { class: `pill pill-${kind}`, text: label }); }

function sectionHeading(kicker, title, detail = "") {
  return node("div", { class: "section-heading" }, [
    node("div", { class: "kicker", text: kicker }),
    node("h2", { text: title }),
    detail ? node("p", { class: "muted", text: detail }) : null,
  ]);
}

function metric(label, value, detail = "", className = "") {
  return node("div", { class: `metric ${className}` }, [
    node("div", { class: "metric-label", text: label }),
    node("div", { class: "metric-value", text: valueOrNA(value) }),
    detail ? node("div", { class: "metric-detail", text: detail }) : null,
  ]);
}

function errorPanel(error, retry = refresh) {
  return node("section", { class: "panel state-panel state-error", role: "alert" }, [
    node("div", { class: "state-icon", text: "!" }),
    node("div", { class: "state-copy" }, [
      node("div", { class: "kicker", text: "SYSTEM FAULT" }),
      node("h2", { text: "数据或 MCP 通信暂时不可用" }),
      node("p", { text: valueOrNA(error?.message) }),
      node("code", { class: "error-code", text: valueOrNA(error?.code ?? "UI_ERROR") }),
    ]),
    node("button", { class: "button button-secondary", type: "button", text: "重新连接", onClick: retry }),
  ]);
}

function loadingPanel(label = "正在同步本地数据") {
  return node("section", { class: "panel loading-panel", "aria-label": label }, [
    node("div", { class: "loading-orbit", text: "◌" }),
    node("div", { class: "loading-copy" }, [
      node("div", { class: "kicker", text: "OFFLINE PIPELINE" }),
      node("strong", { text: label }),
      node("span", { class: "muted", text: "等待 MCP Apps bridge 返回结构化结果…" }),
    ]),
  ]);
}

function emptyPanel() {
  return node("section", { class: "panel state-panel", "data-testid": "empty-state" }, [
    node("div", { class: "state-icon state-icon-soft", text: "◌" }),
    node("div", { class: "state-copy" }, [
      node("div", { class: "kicker", text: "NO REPORT" }),
      node("h2", { text: "先生成一份离线日计划" }),
      node("p", { text: "当前没有可展示的报告。生成只调用后端 ApplicationService，不会自动批准或执行。" }),
    ]),
    node("button", { class: "button button-primary", type: "button", text: "生成日计划", disabled: Boolean(state.pendingTool), onClick: () => runTradeTool("quant_generate_daily_plan", { request_id: requestId("plan") }) }),
  ]);
}

function renderHeader(dashboard) {
  const report = dashboard?.report;
  const connected = dashboard?.connection?.status === "connected";
  return node("header", { class: "topbar" }, [
    node("div", { class: "brand-lockup" }, [
      node("div", { class: "brand-mark", text: "⌁" }),
      node("div", {}, [
        node("div", { class: "eyebrow", text: "QUANT AGENT // LAB" }),
        node("h1", { text: "量化研究终端" }),
        node("div", { class: "brand-subline", text: "RESEARCH · PAPER · REPLAYABLE" }),
      ]),
    ]),
    node("div", { class: "topbar-actions" }, [
      pill("PAPER TRADING // SIMULATED", "paper"),
      node("span", { class: `connection-status ${connected ? "is-connected" : "is-disconnected"}` }, [
        node("span", { class: "connection-dot", "aria-hidden": "true" }),
        node("span", { text: connected ? "MCP LINKED" : "MCP OFFLINE" }),
      ]),
      node("nav", { class: "view-tabs", "aria-label": "主要视图" }, [
        ...[["dashboard", "驾驶舱"], ["chart", "K线与信号"], ["lab", "策略实验室"]].map(([value, label]) => node("button", {
          class: `tab-button ${state.view === value ? "is-active" : ""}`,
          type: "button",
          text: label,
          onClick: () => navigateView(value),
        })),
      ]),
      report ? pill(statusLabel(report.status), statusClass(report.status)) : null,
      node("button", { class: "button button-quiet", type: "button", text: "刷新", disabled: Boolean(state.pendingTool), onClick: refresh }),
      node("button", { class: "button button-quiet", type: "button", text: state.theme === "dark" ? "浅色" : "暗色", onClick: () => { state.theme = state.theme === "dark" ? "light" : "dark"; render(); } }),
      node("button", { class: "button button-quiet", type: "button", text: state.technicalOpen ? "收起" : "技术", onClick: () => { state.technicalOpen = !state.technicalOpen; render(); } }),
    ]),
  ]);
}

function banner(report, dashboard) {
  if (dashboard?.kill_switch?.enabled) return node("section", { class: "banner banner-danger", role: "status" }, [node("strong", { text: "KILL SWITCH // ACTIVE" }), node("span", { text: "后端阻断新的 Paper Trading 执行。" })]);
  if (!report) return null;
  if (report.status === "RISK_BLOCKED") return node("section", { class: "banner banner-danger", role: "status" }, [node("strong", { text: "RISK BLOCKED" }), node("span", { text: "当前报告没有可执行订单；审批按钮已隐藏。" })]);
  if (report.status === "EXPIRED") return node("section", { class: "banner banner-warn", role: "status" }, [node("strong", { text: "APPROVAL EXPIRED" }), node("span", { text: "审批有效期已结束，请生成新报告。" })]);
  return null;
}

function accountSummary(dashboard) {
  const account = dashboard?.account;
  const currency = account?.currency ?? "USD";
  const positionValue = account?.positions?.reduce((total, position) => total + Number(position.market_value ?? Number(position.quantity) * Number(position.market_price)), 0);
  return node("section", { class: "panel summary-panel" }, [
    node("div", { class: "panel-index", text: "01" }),
    sectionHeading("ACCOUNT // SNAPSHOT", "账户与数据", "所有金额来自后端结构化快照；缺失时显示 N/A。"),
    node("div", { class: "metric-grid" }, [
      metric("账户", account?.account_id ?? "N/A", account?.status ?? "等待快照", "metric-account"),
      metric("净值", account ? money(account.equity, currency) : "N/A", account ? `现金 ${money(account.cash, currency)}` : "等待账户"),
      metric("持仓市值", account && Number.isFinite(positionValue) ? money(positionValue, currency) : "N/A", account ? `${account.positions?.length ?? 0} 个持仓` : "等待账户"),
      metric("数据状态", dashboard?.data_freshness?.market_status ?? "UNKNOWN", dashboard?.data_freshness?.market_as_of ? `截至 ${dateTime(dashboard.data_freshness.market_as_of)}` : "等待市场"),
    ]),
  ]);
}

function reportStrip(report) {
  if (!report) return null;
  return node("div", { class: "report-strip" }, [
    node("div", {}, [node("div", { class: "kicker", text: "ACTIVE REPORT" }), node("strong", { text: valueOrNA(report.report_id) })]),
    node("span", { class: "muted", text: `生成 ${dateTime(report.generated_at)} · 过期 ${dateTime(report.expires_at)}` }),
    node("button", { class: "text-button", type: "button", text: "询问 Agent ↗", onClick: () => bridge.sendMessage(`请解释报告 ${report.report_id} 的风险检查和审批状态。`) }),
  ]);
}

function chartWindow(chart) {
  const bars = chart?.bars ?? [];
  const count = Math.max(6, Math.min(state.chartZoom, bars.length || state.chartZoom));
  const end = Math.max(0, bars.length - state.chartOffset);
  const start = Math.max(0, end - count);
  return { bars: bars.slice(start, end), start, end };
}

function markerIndex(marker, bars) {
  return bars.findIndex((bar) => bar.timestamp === marker.timestamp);
}

function chartSvg(chart) {
  const { bars, start, end } = chartWindow(chart);
  if (!bars.length) return node("div", { class: "chart-empty", text: "当前 snapshot 没有可展示的 K 线。" });
  const width = 1000;
  const height = 510;
  const left = 58;
  const right = 22;
  const top = 26;
  const priceBottom = 330;
  const volumeTop = 366;
  const volumeBottom = 452;
  const plotWidth = width - left - right;
  const step = plotWidth / Math.max(1, bars.length);
  const candleWidth = Math.max(4, step * 0.58);
  const indicatorValues = (chart.indicators ?? []).flatMap((indicator) => indicator.values.slice(start, end).filter((value) => value !== null && value !== undefined && value !== "").map(Number).filter(Number.isFinite));
  const highs = bars.map((bar) => Number(bar.high)).concat(indicatorValues);
  const lows = bars.map((bar) => Number(bar.low)).concat(indicatorValues);
  const maxPrice = Math.max(...highs);
  const minPrice = Math.min(...lows);
  const range = Math.max(0.0001, maxPrice - minPrice);
  const maxVolume = Math.max(...bars.map((bar) => Number(bar.volume)), 1);
  const xAt = (index) => left + step * index + step / 2;
  const yAt = (price) => top + ((maxPrice - price) / range) * (priceBottom - top);
  const volumeY = (volume) => volumeBottom - (Number(volume) / maxVolume) * (volumeBottom - volumeTop);
  const svg = svgNode("svg", { class: "market-svg", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${chart.symbol} ${chart.timeframe} K线与信号图` });
  svg.append(svgNode("rect", { x: 0, y: 0, width, height, class: "svg-canvas" }));
  for (let grid = 0; grid < 5; grid += 1) {
    const y = top + (priceBottom - top) * grid / 4;
    const value = maxPrice - range * grid / 4;
    svg.append(svgNode("line", { x1: left, x2: width - right, y1: y, y2: y, class: "svg-grid" }));
    svg.append(svgNode("text", { x: 4, y: y + 4, class: "svg-label", text: number(value, 2) }));
  }
  svg.append(svgNode("line", { x1: left, x2: width - right, y1: volumeTop - 12, y2: volumeTop - 12, class: "svg-divider" }));
  svg.append(svgNode("text", { x: left, y: volumeTop - 20, class: "svg-label svg-label-accent", text: "VOLUME" }));
  bars.forEach((bar, index) => {
    const x = xAt(index);
    const open = Number(bar.open);
    const close = Number(bar.close);
    const high = Number(bar.high);
    const low = Number(bar.low);
    const rising = close >= open;
    svg.append(svgNode("line", { x1: x, x2: x, y1: yAt(high), y2: yAt(low), class: `candle-wick ${rising ? "candle-up" : "candle-down"}` }));
    svg.append(svgNode("rect", { x: x - candleWidth / 2, y: Math.min(yAt(open), yAt(close)), width: candleWidth, height: Math.max(2, Math.abs(yAt(open) - yAt(close))), class: `candle-body ${rising ? "candle-up" : "candle-down"}` }));
    svg.append(svgNode("rect", { x: x - candleWidth / 2, y: volumeY(bar.volume), width: candleWidth, height: Math.max(1, volumeBottom - volumeY(bar.volume)), class: `volume-bar ${rising ? "volume-up" : "volume-down"}` }));
    if (index === 0 || index === bars.length - 1 || index % Math.max(1, Math.floor(bars.length / 5)) === 0) {
      svg.append(svgNode("text", { x, y: height - 25, class: "svg-label svg-time", text: String(bar.timestamp).slice(5, 10) }));
    }
  });
  const indicatorColors = ["#38e8b0", "#ffbf69", "#a98bff", "#5bb8ff", "#ff6b9a"];
  (chart.indicators ?? []).forEach((indicator, indicatorIndex) => {
    const points = indicator.values.slice(start, end).map((value, index) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? `${xAt(index)},${yAt(Number(value))}` : null).filter(Boolean).join(" ");
    if (points) svg.append(svgNode("polyline", { points, class: "indicator-line", stroke: indicatorColors[indicatorIndex % indicatorColors.length] }));
  });
  for (const signal of chart.signals ?? []) {
    const index = markerIndex(signal, bars);
    if (index < 0) continue;
    const x = xAt(index);
    const y = yAt(Number(signal.price));
    const buy = signal.direction === "BUY";
    const points = buy ? `${x},${y - 18} ${x - 8},${y - 5} ${x + 8},${y - 5}` : `${x},${y + 18} ${x - 8},${y + 5} ${x + 8},${y + 5}`;
    svg.append(svgNode("polygon", { points, class: buy ? "marker-buy" : "marker-sell" }));
    svg.append(svgNode("text", { x: x + 10, y: buy ? y - 8 : y + 13, class: `marker-label ${buy ? "marker-label-buy" : "marker-label-sell"}`, text: buy ? "BUY" : "SELL" }));
  }
  for (const marker of chart.markers ?? []) {
    const index = markerIndex(marker, bars);
    if (index < 0) continue;
    const x = xAt(index);
    const y = yAt(Number(marker.price));
    svg.append(svgNode("line", { x1: x, x2: x, y1: top, y2: priceBottom, class: `event-line event-${String(marker.kind).toLowerCase().replaceAll(" ", "-")}` }));
    svg.append(svgNode("text", { x: x + 4, y: Math.max(16, y - 8), class: "marker-label marker-label-event", text: valueOrNA(marker.label) }));
  }
  if (state.chartHoverIndex !== null && state.chartHoverIndex >= start && state.chartHoverIndex < end) {
    const index = state.chartHoverIndex - start;
    const bar = bars[index];
    const x = xAt(index);
    svg.append(svgNode("line", { x1: x, x2: x, y1: top, y2: volumeBottom, class: "crosshair" }));
    svg.append(svgNode("circle", { cx: x, cy: yAt(Number(bar.close)), r: 4, class: "crosshair-dot" }));
  }
  svg.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(0.999, (event.clientX - rect.left) / rect.width));
    state.chartHoverIndex = start + Math.min(bars.length - 1, Math.floor(ratio * bars.length));
    render();
  });
  svg.addEventListener("mouseleave", () => { state.chartHoverIndex = null; render(); });
  return svg;
}

function chartTooltip(chart) {
  const index = state.chartHoverIndex;
  if (index === null || !chart?.bars?.[index]) return node("div", { class: "chart-tooltip chart-tooltip-empty", text: "移动光标查看 OHLCV · 点击信号定位 DebugTrace" });
  const bar = chart.bars[index];
  const indicators = (chart.indicators ?? []).map((item) => `${item.label} ${number(item.values[index], 2)}`).join("  ·  ");
  return node("div", { class: "chart-tooltip" }, [
    node("strong", { text: `${String(bar.timestamp).slice(0, 16)}  ${chart.symbol}` }),
    node("span", { text: `O ${number(bar.open)}  H ${number(bar.high)}  L ${number(bar.low)}  C ${number(bar.close)}` }),
    node("span", { text: `VOL ${number(bar.volume, 0)}  ·  ${indicators || "无指标"}` }),
  ]);
}

function chartLegend(chart) {
  const entries = [...(chart?.legend ?? []), ...(chart?.indicators ?? []).map((item, index) => ({ kind: item.name, label: item.label, color: ["#38e8b0", "#ffbf69", "#a98bff", "#5bb8ff"][index % 4] }))];
  return node("div", { class: "chart-legend" }, entries.map((item) => node("span", { class: "legend-item" }, [node("i", { class: "legend-glyph", style: `--legend-color:${item.color}` }), node("span", { text: item.label })])));
}

function chartPanel(compact = false) {
  const chart = state.chart;
  if (!chart) return node("section", { class: "panel chart-panel" }, [sectionHeading("MARKET // VISUALIZER", "K线与信号", "正在加载固定 snapshot…"), loadingPanel("加载行情图")]);
  const latest = chart.latest ?? {};
  const symbols = ["AAPL", "MSFT", "DEMO"];
  const visibleBars = chartWindow(chart).bars;
  return node("section", { class: `panel chart-panel ${compact ? "chart-compact" : ""}` }, [
    node("div", { class: "panel-index", text: compact ? "02A" : "02" }),
    node("div", { class: "chart-heading" }, [
      sectionHeading("MARKET // OHLCV + SIGNALS", "K线与信号", "权威数据来自后端 snapshot；SVG 只负责呈现，不在浏览器生成交易信号。"),
      node("div", { class: "chart-controls" }, [
        node("label", { class: "control-label" }, [node("span", { text: "SYMBOL" }), node("select", { value: state.symbol, onChange: (event) => { state.symbol = event.target.value; loadChart(); } }, symbols.map((symbol) => node("option", { value: symbol, text: symbol })))]),
        node("label", { class: "control-label" }, [node("span", { text: "TIMEFRAME" }), node("select", { value: state.timeframe, onChange: (event) => { state.timeframe = event.target.value; loadChart(); } }, (chart.supported_timeframes ?? ["1d"]).map((timeframe) => node("option", { value: timeframe, text: timeframe })))]),
        node("button", { class: "button button-quiet", type: "button", text: "−", ariaLabel: "缩小时间窗口", onClick: () => { state.chartZoom = Math.max(8, state.chartZoom - 8); render(); } }),
        node("button", { class: "button button-quiet", type: "button", text: "+", ariaLabel: "放大时间窗口", onClick: () => { state.chartZoom = Math.min(chart.bars.length, state.chartZoom + 8); render(); } }),
        node("button", { class: "button button-quiet", type: "button", text: "↻", ariaLabel: "重新读取图表", onClick: loadChart }),
      ]),
    ]),
    node("div", { class: "chart-meta-grid" }, [
      metric("LAST", number(latest.price, 2), `${chart.symbol} · ${chart.timeframe}`, "metric-price"),
      metric("OHLC", `${number(latest.open)} / ${number(latest.high)} / ${number(latest.low)} / ${number(latest.close)}`, "open / high / low / close"),
      metric("CHANGE", latest.change_percent === null ? "N/A" : percentage(latest.change_percent), latest.change === null ? "等待前值" : `${number(latest.change)} price units`, Number(latest.change) >= 0 ? "metric-positive" : "metric-negative"),
      metric("DATA TIME", dateTime(chart.data_as_of), chart.stale ? "STALE · 只读展示" : "freshness checked"),
    ]),
    node("div", { class: `data-ribbon ${chart.stale ? "is-stale" : ""}` }, [
      pill(chart.is_synthetic ? "SIMULATED DATA" : "VERIFIED DATA", chart.is_synthetic ? "accent" : "success"),
      node("span", { text: `snapshot ${chart.snapshot_id}` }),
      node("span", { text: `source ${chart.data_source}` }),
      chart.stale ? node("strong", { text: "STALE // 禁止据此执行" }) : node("span", { text: "time-ordered · no fill-in" }),
    ]),
    node("div", { class: "chart-viewport" }, [chartSvg(chart)]),
    chartTooltip(chart),
    chartLegend(chart),
    node("div", { class: "chart-footer" }, [node("span", { text: `${visibleBars.length} bars visible · ${chart.bars.length} bars in snapshot` }), node("span", { text: `strategy ${chart.strategy?.strategy_id}@${chart.strategy?.version}` }), node("span", { text: "signals = backend output" })]),
  ]);
}

function strategyPanel(report) {
  const plan = report?.plan;
  const signals = plan?.signals ?? [];
  return node("section", { class: "panel strategy-panel" }, [
    node("div", { class: "panel-index", text: "03A" }),
    sectionHeading("STRATEGY // DAILY PLAN", "策略摘要", "信号仅用于解释当前离线计划，不构成投资建议。"),
    node("div", { class: "strategy-meta" }, [node("div", { class: "strategy-name", text: valueOrNA(plan?.strategy_id) }), node("div", { class: "muted", text: `v${valueOrNA(plan?.strategy_version)} · risk ${valueOrNA(plan?.risk_config_version)}` })]),
    node("div", { class: "signal-list" }, signals.length ? signals.map((signal) => node("div", { class: "signal-row" }, [pill(valueOrNA(signal.direction), signal.direction === "BUY" ? "success" : signal.direction === "SELL" ? "danger" : "neutral"), node("strong", { text: valueOrNA(signal.symbol) }), node("span", { class: "muted", text: valueOrNA(signal.reason_code) }), node("code", { text: number(signal.strength, 4) })])) : [node("p", { class: "muted", text: "暂无策略信号。" })]),
    node("p", { class: "chart-summary", text: `共 ${signals.length} 个信号；强度不是成功概率，风险真值以结构化检查为准。` }),
  ]);
}

function riskPanel(report) {
  const decision = report?.plan?.risk_decision;
  const checks = decision?.checks ?? [];
  const blockCount = checks.filter((check) => !check.passed && check.severity === "BLOCK").length;
  return node("section", { class: "panel risk-panel" }, [
    node("div", { class: "panel-index", text: "03B" }),
    sectionHeading("RISK // GATEKEEPER", "风险检查", "颜色只是辅助；每条检查同时展示文字状态、理由码和说明。"),
    node("div", { class: "risk-summary" }, [pill(decision?.allowed ? "PASS // ORDERS ALLOWED" : "BLOCK // NO EXECUTION", decision?.allowed ? "success" : "danger"), node("span", { class: "muted", text: `${blockCount} BLOCK · ${checks.length} checks` })]),
    node("div", { class: "risk-list" }, checks.length ? checks.map((check) => {
      const kind = check.passed ? "success" : check.severity === "WARNING" ? "warn" : "danger";
      return node("div", { class: `risk-row risk-${kind}` }, [
        node("div", { class: "risk-status" }, [node("span", { class: "risk-status-dot" }), node("span", { text: check.passed ? "PASS" : valueOrNA(check.severity) })]),
        node("div", { class: "risk-copy" }, [node("strong", { text: valueOrNA(check.name) }), node("span", { text: valueOrNA(check.message) })]),
        node("code", { class: "reason-code", text: valueOrNA(check.reason_code) }),
      ]);
    }) : [node("p", { class: "muted", text: "暂无风险检查。" })]),
  ]);
}

function brokerOrderFor(order, execution) { return execution?.broker_orders?.find((candidate) => candidate.order_id === order.order_id); }

function orderStatus(order, report, approval, execution) {
  const brokerOrder = brokerOrderFor(order, execution);
  if (brokerOrder) return statusLabel(brokerOrder.status);
  if (approval?.approved_order_ids?.includes(order.order_id)) return "已批准";
  if (report?.plan?.risk_decision?.blocked_order_ids?.includes(order.order_id)) return "风险阻断";
  return "待处理";
}

function ordersPanel(dashboard) {
  const report = dashboard?.report;
  const orders = report?.plan?.orders ?? [];
  const approval = dashboard?.approval;
  const execution = dashboard?.execution;
  const canSelect = report?.status === "PENDING_APPROVAL";
  return node("section", { class: "panel orders-panel" }, [
    node("div", { class: "panel-index", text: "04" }),
    sectionHeading("ORDERS // PLAN", "候选订单", `${orders.length} 个候选；界面不构造订单，执行前仍由后端复核。`),
    node("div", { class: "table-wrap" }, [node("table", {}, [
      node("caption", { class: "sr-only", text: "候选订单列表" }),
      node("thead", {}, [node("tr", {}, ["选择", "标的", "方向", "数量", "参考价", "名义金额", "状态"].map((label) => node("th", { scope: "col", text: label })))]),
      node("tbody", {}, orders.length ? orders.map((order) => {
        const allowed = report?.plan?.risk_decision?.allowed_order_ids?.includes(order.order_id);
        const status = orderStatus(order, report, approval, execution);
        return node("tr", { class: allowed ? "order-allowed" : "order-blocked" }, [
          node("td", {}, [node("input", { type: "checkbox", checked: state.selected.has(order.order_id), disabled: !canSelect || !allowed || Boolean(state.pendingTool), ariaLabel: `选择 ${order.symbol} ${order.side} 订单`, onChange: (event) => { if (event.target.checked) state.selected.add(order.order_id); else state.selected.delete(order.order_id); render(); } })]),
          node("td", { class: "symbol-cell" }, [node("strong", { text: valueOrNA(order.symbol) }), node("span", { class: "muted", text: valueOrNA(order.order_id) })]),
          node("td", {}, [pill(valueOrNA(order.side), order.side === "BUY" ? "success" : "danger")]),
          node("td", { text: number(order.quantity, 8) }),
          node("td", { text: money(order.reference_price, report?.account?.currency ?? "USD") }),
          node("td", { text: money(order.notional, report?.account?.currency ?? "USD") }),
          node("td", {}, [pill(status, statusClass(status))]),
        ]);
      }) : [node("tr", {}, [node("td", { colSpan: "7", class: "empty-cell", text: "当前计划没有候选订单。" })])]),
    ])]),
  ]);
}

function approvalPanel(dashboard) {
  const report = dashboard?.report;
  const approval = dashboard?.approval;
  const killOn = Boolean(dashboard?.kill_switch?.enabled);
  const pending = Boolean(state.pendingTool);
  const waiting = report?.status === "PENDING_APPROVAL";
  const approved = ["APPROVED", "PARTIALLY_APPROVED"].includes(report?.status);
  const execution = dashboard?.execution;
  return node("section", { class: "panel approval-panel" }, [
    node("div", { class: "panel-index", text: "05" }),
    sectionHeading("HUMAN // GATE", "审批与 Paper Trading", "审批绑定报告版本和计划哈希；执行是第二个明确动作。"),
    node("div", { class: "approval-facts" }, [
      node("div", {}, [node("span", { class: "fact-label", text: "报告版本" }), node("strong", { text: report ? `v${report.report_version}` : "N/A" })]),
      node("div", {}, [node("span", { class: "fact-label", text: "计划哈希" }), node("code", { class: "hash-value", text: valueOrNA(report?.plan?.plan_hash) })]),
      node("div", {}, [node("span", { class: "fact-label", text: "审批有效期" }), node("strong", { text: dateTime(approval?.expires_at ?? report?.expires_at) })]),
      node("div", {}, [node("span", { class: "fact-label", text: "批准订单" }), node("strong", { text: approval ? `${approval.approved_order_ids?.length ?? 0} 个` : "N/A" })]),
    ]),
    node("div", { class: "approval-safety-note" }, [node("span", { class: "safety-lock", text: "⌁" }), node("span", { text: killOn ? "Kill Switch 已启用，执行按钮被后端和界面共同阻断。" : "当前为 Paper Trading；没有任何自动执行。" })]),
    node("div", { class: "action-row" }, [
      waiting ? node("button", { class: "button button-primary", type: "button", disabled: pending, text: "批准全部风险允许订单", onClick: () => runTradeTool("quant_submit_approval", { report_id: report.report_id, scope: "ALL", approver: "dashboard-user", request_id: requestId("approve-all") }) }) : null,
      waiting ? node("button", { class: "button button-secondary", type: "button", disabled: pending || state.selected.size === 0, text: `批准选中 (${state.selected.size})`, onClick: () => runTradeTool("quant_submit_approval", { report_id: report.report_id, scope: "PARTIAL", order_ids: [...state.selected], approver: "dashboard-user", request_id: requestId("approve-partial") }) }) : null,
      waiting ? node("button", { class: "button button-danger-outline", type: "button", disabled: pending, text: "拒绝计划", onClick: () => runTradeTool("quant_reject_plan", { report_id: report.report_id, approver: "dashboard-user", request_id: requestId("reject") }) }) : null,
      approved && !execution ? node("button", { class: "button button-execute", type: "button", disabled: pending || killOn, text: "执行 Paper Trading", onClick: () => { state.modal = "execute"; render(); } }) : null,
      execution ? pill(`EXECUTION // ${statusLabel(execution.status)}`, statusClass(execution.status)) : null,
    ]),
    approved && !execution ? node("p", { class: "muted action-help", text: "批准不会自动执行；请再次点击执行并确认。" }) : null,
  ]);
}

function killSwitchPanel(dashboard) {
  const kill = dashboard?.kill_switch ?? {};
  const enabled = Boolean(kill.enabled);
  return node("section", { class: `panel kill-panel ${enabled ? "kill-active" : ""}` }, [
    node("div", { class: "panel-index", text: "06" }),
    sectionHeading("SYSTEM // CIRCUIT BREAKER", "Kill Switch", enabled ? "保护已启用：新的执行请求必须被后端阻断。" : "保护关闭：仍需有效审批和执行前风险复核。"),
    node("div", { class: "kill-layout" }, [
      node("div", { class: "kill-state" }, [node("span", { class: `kill-indicator ${enabled ? "active" : "inactive" }` }), node("strong", { text: enabled ? "ACTIVE · 已启用" : "OFF · 已关闭" }), node("span", { class: "muted", text: valueOrNA(kill.reason) })]),
      node("button", { class: enabled ? "button button-secondary" : "button button-danger", type: "button", disabled: Boolean(state.pendingTool), text: enabled ? "关闭 Kill Switch" : "启用 Kill Switch", onClick: () => runTradeTool("quant_set_kill_switch", { enabled: !enabled, reason: enabled ? "dashboard operator resume" : "dashboard operator stop", actor: "dashboard-user", request_id: requestId(enabled ? "kill-off" : "kill-on") }) }),
    ]),
  ]);
}

function timelinePanel(dashboard) {
  const events = dashboard?.audit_events ?? [];
  return node("section", { class: "panel timeline-panel" }, [
    node("div", { class: "panel-index", text: "07" }),
    sectionHeading("AUDIT // TRACE", "审计时间线", "展示最近结构化事件；研究 DebugTrace 使用独立 research 审计流。"),
    node("ol", { class: "timeline" }, events.length ? events.slice(0, 8).map((event) => node("li", { class: "timeline-item" }, [node("span", { class: "timeline-dot" }), node("div", { class: "timeline-copy" }, [node("strong", { text: valueOrNA(event.event_type) }), node("span", { text: `${dateTime(event.timestamp)} · ${valueOrNA(event.reason_code)}` }), node("small", { text: valueOrNA(event.result_summary || event.input_summary) })])])) : [node("li", { class: "timeline-empty", text: "暂无审计事件。" })]),
  ]);
}

function technicalPanel(dashboard) {
  if (!state.technicalOpen) return null;
  return node("aside", { class: "technical-drawer", "aria-label": "技术详情" }, [
    node("div", { class: "drawer-header" }, [node("div", {}, [node("div", { class: "kicker", text: "TECHNICAL // CONTRACT" }), node("h2", { text: "连接与隔离" })]), node("button", { class: "button button-quiet", type: "button", text: "关闭", onClick: () => { state.technicalOpen = false; render(); } })]),
    node("dl", { class: "technical-list" }, [node("dt", { text: "MCP UI resource" }), node("dd", { text: UI_RESOURCE_URI }), node("dt", { text: "Bridge" }), node("dd", { text: "ui/initialize · tool-input/result · tools/call · ui/message" }), node("dt", { text: "Backend" }), node("dd", { text: valueOrNA(dashboard?.connection?.transport) }), node("dt", { text: "Mode" }), node("dd", { text: "PAPER_TRADING / LiveBroker disabled" }), node("dt", { text: "Chart" }), node("dd", { text: "backend snapshot · SVG render-only" }), node("dt", { text: "Python" }), node("dd", { text: "SANDBOX_UNAVAILABLE" })]),
    node("p", { class: "muted", text: "界面没有 broker、数据库、凭据或策略执行权；自定义策略只经过声明式 DSL 解释器，回测和调试不写交易审计流。" }),
  ]);
}

function dashboardContent(dashboard) {
  const report = dashboard?.report;
  return node("main", { class: "content" }, [banner(report, dashboard), reportStrip(report), accountSummary(dashboard), chartPanel(true), report ? node("div", { class: "two-column" }, [strategyPanel(report), riskPanel(report)]) : null, report ? ordersPanel(dashboard) : null, report ? approvalPanel(dashboard) : null, killSwitchPanel(dashboard), report ? timelinePanel(dashboard) : null]);
}

function strategyList() {
  return node("aside", { class: "lab-sidebar panel" }, [
    node("div", { class: "panel-index", text: "LAB // 01" }),
    sectionHeading("REGISTRY // VERSIONS", "策略注册表", "状态变更是显式的；保存草稿不等于启用。"),
    node("div", { class: "strategy-list" }, state.strategies.length
      ? state.strategies.map((item) => node("button", {
        class: `strategy-list-item ${state.strategy?.manifest?.strategy_id === item.strategy_id && state.strategy?.manifest?.version === item.version ? "is-selected" : ""}`,
        type: "button",
        onClick: () => loadStrategy(item.strategy_id, item.version),
      }, [
        node("span", { class: "strategy-list-main" }, [node("strong", { text: item.display_name }), node("small", { text: `${item.strategy_id}@${item.version}` })]),
        pill(item.status, statusClass(item.status)),
      ]))
      : [node("p", { class: "muted", text: "注册表加载中…" })]),
    node("div", { class: "sandbox-card" }, [node("div", { class: "kicker", text: "PYTHON RUNNER" }), node("strong", { text: "SANDBOX_UNAVAILABLE" }), node("p", { text: "当前环境没有可证明的进程/容器隔离；任意 Python 策略保持禁用。声明式 DSL 仍可编辑、调试和回测。" })]),
  ]);
}

function editorPanel() {
  const validation = state.strategyValidation;
  return node("section", { class: "lab-editor panel" }, [
    node("div", { class: "panel-index", text: "LAB // 02" }),
    node("div", { class: "lab-editor-heading" }, [sectionHeading("DECLARATIVE // AST", "策略编辑器", "JSON DSL · allow-list · no eval / no exec"), node("div", { class: "editor-actions" }, [node("button", { class: "button button-quiet", type: "button", text: "格式化", onClick: () => { try { state.editorText = JSON.stringify(JSON.parse(state.editorText), null, 2); state.strategyError = null; } catch (error) { state.strategyError = { code: "SCHEMA_INVALID", message: error.message }; } render(); } }), node("button", { class: "button button-secondary", type: "button", text: "校验", disabled: Boolean(state.pendingTool), onClick: validateEditor }), node("button", { class: "button button-primary", type: "button", text: "保存草稿", disabled: Boolean(state.pendingTool), onClick: saveDraft })])]),
    node("textarea", { class: "strategy-editor", spellcheck: "false", value: state.editorText, ariaLabel: "声明式策略 JSON DSL", onInput: (event) => { state.editorText = event.target.value; state.strategyValidation = null; } }),
    validation ? node("div", { class: `validation-box ${validation.valid ? "validation-ok" : "validation-error"}`, role: "status" }, [node("strong", { text: validation.valid ? "VALID // schema accepted" : "INVALID // fix before run" }), ...(validation.errors ?? []).map((error) => node("div", { class: "validation-row" }, [node("code", { text: `${error.code} · ${error.path}` }), node("span", { text: error.message })]))]) : null,
    state.strategyError ? node("div", { class: "validation-box validation-error", role: "alert" }, [node("strong", { text: state.strategyError.code }), node("span", { text: state.strategyError.message })]) : null,
    node("div", { class: "dsl-footnote" }, [node("span", { text: "Allowed: SMA · EMA · RSI · MACD · Bollinger · rolling high/low · returns" }), node("span", { text: "Output: BUY · SELL · HOLD + reason_code" })]),
  ]);
}

function labControls() {
  const manifest = state.strategy?.manifest;
  const parameterFields = manifest?.parameter_schema ?? [];
  return node("section", { class: "lab-controls panel" }, [
    node("div", { class: "panel-index", text: "LAB // 03" }),
    sectionHeading("RUN // CONTROL", "实验参数", "固定 snapshot、下一根开盘执行、long-only、无 Broker 写入。"),
    node("div", { class: "form-grid" }, [
      node("label", { class: "control-label" }, [node("span", { text: "SYMBOL" }), node("select", { value: state.symbol, onChange: (event) => { state.symbol = event.target.value; } }, ["AAPL", "MSFT", "DEMO"].map((symbol) => node("option", { value: symbol, text: symbol })))]),
      node("label", { class: "control-label" }, [node("span", { text: "TIMEFRAME" }), node("select", { value: state.timeframe, onChange: (event) => { state.timeframe = event.target.value; } }, ["1d"].map((timeframe) => node("option", { value: timeframe, text: timeframe })))]),
      node("label", { class: "control-label" }, [node("span", { text: "INITIAL CASH" }), node("input", { type: "number", value: "10000", min: "1", step: "100", id: "initial-cash" })]),
      node("label", { class: "control-label" }, [node("span", { text: "MAX BARS" }), node("input", { type: "number", value: "40", min: "1", max: "500", step: "1", id: "max-bars" })]),
    ]),
    parameterFields.length ? node("div", { class: "parameter-grid" }, parameterFields.map((field) => node("label", { class: "control-label" }, [node("span", { text: `${field.name} · ${field.value_type}` }), node("input", { type: field.value_type === "integer" ? "number" : "text", value: state.labParameters[field.name] ?? field.default, min: field.minimum ?? undefined, max: field.maximum ?? undefined, onChange: (event) => { state.labParameters[field.name] = field.value_type === "integer" ? Number(event.target.value) : event.target.value; } })]))) : node("p", { class: "muted", text: "当前策略没有可编辑参数。" }),
    node("div", { class: "lab-run-actions" }, [node("button", { class: "button button-secondary", type: "button", disabled: Boolean(state.pendingTool), text: "逐根调试", onClick: runDebug }), node("button", { class: "button button-primary", type: "button", disabled: Boolean(state.pendingTool), text: "运行离线回测", onClick: runBacktest }), state.backtestHistory.length > 1 ? node("button", { class: "button button-quiet", type: "button", disabled: Boolean(state.pendingTool), text: "比较最近两次", onClick: compareRecent }) : null]),
    node("div", { class: "promotion-flow" }, [node("span", { class: "flow-node flow-active", text: "DRAFT" }), node("span", { text: "→" }), node("span", { class: "flow-node", text: "VALIDATED" }), node("span", { text: "→" }), node("span", { class: "flow-node", text: "BACKTESTED" }), node("span", { text: "→" }), node("span", { class: "flow-node", text: "PAPER_CANDIDATE" })]),
  ]);
}

function debugPanel() {
  if (!state.debug) return node("section", { class: "panel lab-result empty-result" }, [node("div", { class: "kicker", text: "DEBUG TRACE" }), node("h3", { text: "逐根调试结果将在这里出现" }), node("p", { class: "muted", text: "运行后可逐步查看 OHLCV、指标值、规则真假、信号和忽略原因。" })]);
  const trace = state.debug.trace?.[state.debugIndex] ?? state.debug.trace?.[0];
  return node("section", { class: "panel lab-result debug-result" }, [
    node("div", { class: "result-heading" }, [sectionHeading("REPLAY // DEBUG TRACE", "逐根 K 线调试", `${state.debug.run_id} · ${state.debug.total_bars} bars`), node("div", { class: "trace-actions" }, [node("button", { class: "button button-quiet", type: "button", text: "|◀", disabled: state.debugIndex <= 0, onClick: () => { state.debugIndex = 0; render(); } }), node("button", { class: "button button-quiet", type: "button", text: "◀", disabled: state.debugIndex <= 0, onClick: () => { state.debugIndex -= 1; render(); } }), node("span", { class: "trace-counter", text: `${state.debugIndex + 1} / ${state.debug.trace.length}` }), node("button", { class: "button button-quiet", type: "button", text: "▶", disabled: state.debugIndex >= state.debug.trace.length - 1, onClick: () => { state.debugIndex += 1; render(); } }), node("button", { class: "button button-quiet", type: "button", text: "▶|", disabled: state.debugIndex >= state.debug.trace.length - 1, onClick: () => { state.debugIndex = state.debug.trace.length - 1; render(); } })])]),
    trace ? node("div", { class: "trace-grid" }, [metric("BAR", trace.bar_index, trace.warmup ? "WARMUP" : "LIVE"), metric("TIME", dateTime(trace.timestamp), "fixed snapshot"), metric("CLOSE", number(trace.ohlcv?.close, 2), "current bar"), metric("SIGNAL", trace.signal?.direction, trace.signal?.reason_code)],) : null,
    trace ? node("div", { class: "trace-detail-grid" }, [node("div", { class: "trace-card" }, [node("div", { class: "kicker", text: "INDICATORS" }), ...Object.entries(trace.indicators ?? {}).map(([name, value]) => node("div", { class: "trace-line" }, [node("code", { text: name }), node("strong", { text: number(value, 4) })]))]), node("div", { class: "trace-card" }, [node("div", { class: "kicker", text: "RULE EVALUATION" }), ...Object.entries(trace.rules ?? {}).map(([name, value]) => node("div", { class: "trace-line" }, [node("code", { text: name }), pill(value ? "TRUE" : "FALSE", value ? "success" : "neutral")]))]), node("div", { class: "trace-card trace-log" }, [node("div", { class: "kicker", text: "WHY" }), node("p", { text: trace.ignored_reason ?? trace.signal?.reason_code ?? "signal generated" }), node("code", { text: trace.signal?.reason_code ?? "NO_SIGNAL" })])]) : null,
  ]);
}

function backtestPanel() {
  if (!state.backtest) return node("section", { class: "panel lab-result empty-result" }, [node("div", { class: "kicker", text: "BACKTEST // RESULT" }), node("h3", { text: "净值与回撤结果将在这里出现" }), node("p", { class: "muted", text: "回测不会调用 PaperBroker；信号在收盘后生成，最早下一根开盘执行。" })]);
  const metrics = state.backtest.metrics ?? {};
  return node("section", { class: "panel lab-result backtest-result" }, [
    node("div", { class: "result-heading" }, [sectionHeading("RESEARCH // BACKTEST", "离线回测结果", `${state.backtest.run_id} · ${state.backtest.status}`), pill("NO BROKER WRITE", "accent")]),
    node("div", { class: "backtest-metric-grid" }, [metric("TOTAL RETURN", percentage(metrics.total_return), "strategy result"), metric("MAX DRAWDOWN", percentage(metrics.max_drawdown), "peak-to-trough"), metric("TRADES", metrics.trade_count, `win ${percentage(metrics.win_rate)}`), metric("SHARPE", metrics.sharpe_ratio, "N/A if sample short"), metric("FEES", number(metrics.fees, 2), "basis-point model"), metric("SLIPPAGE", number(metrics.slippage_cost, 2), "deterministic cost")]),
    node("div", { class: "mini-curves" }, [curveSvg(state.backtest.equity_curve, "equity", "NET VALUE"), curveSvg(state.backtest.drawdown_curve, "drawdown", "DRAWDOWN")]),
    node("div", { class: "assumptions-strip" }, (state.backtest.assumptions ?? []).map((item) => node("span", { text: `· ${item}` }))),
    state.backtest.trades?.length ? tradeTable(state.backtest.trades) : null,
  ]);
}

function tradeTable(trades) {
  return node("div", { class: "table-wrap" }, [node("table", {}, [
    node("thead", {}, [node("tr", {}, ["entry", "exit", "qty", "net pnl", "status"].map((label) => node("th", { text: label })))]),
    node("tbody", {}, trades.map((trade) => node("tr", {}, [
      node("td", { text: dateTime(trade.entry_timestamp) }),
      node("td", { text: dateTime(trade.exit_timestamp) }),
      node("td", { text: number(trade.quantity, 0) }),
      node("td", { text: number(trade.net_pnl, 2) }),
      node("td", {}, [pill(trade.status ?? "CLOSED", trade.net_pnl > 0 ? "success" : "warn")]),
    ]))),
  ])]);
}

function curveSvg(points, kind, label) {
  const values = (points ?? []).map((item) => Number(item[kind])).filter(Number.isFinite);
  if (!values.length) return node("div", { class: "curve-card" }, [node("span", { class: "kicker", text: label }), node("span", { class: "muted", text: "N/A" })]);
  const width = 440; const height = 115; const min = Math.min(...values); const max = Math.max(...values); const range = Math.max(0.0001, max - min);
  const coords = values.map((value, index) => `${(index / Math.max(1, values.length - 1)) * width},${height - ((value - min) / range) * (height - 20)}`).join(" ");
  return node("div", { class: "curve-card" }, [node("span", { class: "kicker", text: label }), svgNode("svg", { viewBox: `0 0 ${width} ${height}`, class: "curve-svg", role: "img", "aria-label": label }, [svgNode("polyline", { points: coords, class: `curve-line curve-${kind}` })]), node("span", { class: "muted", text: `${number(values[0], 2)} → ${number(values.at(-1), 2)}` })]);
}

function strategyLabView() {
  return node("main", { class: "content lab-content" }, [
    node("section", { class: "lab-hero panel" }, [node("div", { class: "hero-gridline" }), node("div", { class: "kicker", text: "RESEARCH // CONTROLLED EXPERIMENT" }), node("h2", { text: "策略实验室" }), node("p", { text: "编辑声明式 AST，校验、逐根回放、离线回测，再显式提升为 Paper Candidate。每个 run 绑定 strategy version、source_hash 与 market snapshot。" }), node("div", { class: "hero-badges" }, [pill("DECLARATIVE ONLY", "accent"), pill("NO EVAL / NO EXEC", "success"), pill("REPLAYABLE", "neutral")])]),
    node("div", { class: "lab-layout" }, [strategyList(), editorPanel(), labControls()]),
    state.researchError ? errorPanel(state.researchError, () => { state.researchError = null; render(); }) : null,
    node("div", { class: "lab-results" }, [debugPanel(), backtestPanel()]),
    state.backtest?.status === "COMPLETED" && state.strategy?.manifest?.status === "BACKTESTED" ? node("section", { class: "panel candidate-panel" }, [node("div", { class: "kicker", text: "PROMOTION // EXPLICIT" }), node("h3", { text: "回测通过后，可创建 Paper Candidate" }), node("p", { class: "muted", text: "这不会替换当前每日策略、继承审批或产生任何交易。" }), node("button", { class: "button button-primary", type: "button", text: "提升为 PAPER_CANDIDATE", onClick: () => { state.modal = "candidate"; render(); } })]) : null,
  ]);
}

function modalPanel() {
  if (!state.modal) return null;
  if (state.modal === "candidate") {
    return node("div", { class: "modal-backdrop", role: "presentation" }, [
      node("div", { class: "modal-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "candidate-dialog-title" }, [
        node("div", { class: "kicker", text: "VERSION GATE" }),
        node("h2", { id: "candidate-dialog-title", text: "创建 Paper Candidate？" }),
        node("p", { text: "将固化策略版本、source_hash、snapshot 和回测 run；不会自动进入交易流程。" }),
        node("div", { class: "modal-actions" }, [
          node("button", { class: "button button-secondary", type: "button", text: "取消", onClick: () => { state.modal = null; render(); } }),
          node("button", { class: "button button-primary", type: "button", text: "确认提升", onClick: () => { state.modal = null; promoteCandidate(); } }),
        ]),
      ]),
    ]);
  }
  return node("div", { class: "modal-backdrop", role: "presentation" }, [
    node("div", { class: "modal-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "execute-dialog-title" }, [
      node("div", { class: "kicker", text: "SECOND CONFIRMATION" }),
      node("h2", { id: "execute-dialog-title", text: "确认 Paper Trading 执行？" }),
      node("p", { text: "这只会请求隔离项目的 PaperBroker。后端会再次检查审批绑定、快照、风险规则和 Kill Switch；不会连接真实券商。" }),
      node("div", { class: "modal-actions" }, [
        node("button", { class: "button button-secondary", type: "button", text: "取消", onClick: () => { state.modal = null; render(); } }),
        node("button", { class: "button button-primary", type: "button", text: "确认模拟执行", onClick: () => { state.modal = null; runTradeTool("quant_execute_paper_plan", { report_id: state.dashboard?.report?.report_id, request_id: requestId("execute") }); } }),
      ]),
    ]),
  ]);
}

function render() {
  if (!root) return;
  document.documentElement.dataset.theme = state.theme;
  clear(root);
  const dashboard = state.dashboard;
  const shell = node("div", { class: "app-shell" }, [renderHeader(dashboard), state.phase === "error" && state.view !== "lab" ? node("main", { class: "content" }, [errorPanel(state.error)]) : null, state.phase === "loading" && state.view !== "lab" ? node("main", { class: "content" }, [loadingPanel()]) : null, state.phase === "ready" && state.view === "lab" ? strategyLabView() : null, state.phase === "ready" && state.view !== "lab" && !dashboard?.report ? node("main", { class: "content" }, [emptyPanel(), killSwitchPanel(dashboard), state.view === "chart" ? chartPanel() : null]) : null, state.phase === "ready" && state.view === "dashboard" && dashboard?.report ? dashboardContent(dashboard) : null, state.phase === "ready" && state.view === "chart" ? node("main", { class: "content" }, [chartPanel()]) : null, technicalPanel(dashboard), modalPanel(), node("footer", { class: "footer-note", text: "Quant Agent Lab · offline research terminal · not investment advice · PAPER TRADING ONLY" })]);
  root.append(shell);
}

function requestId(prefix) { state.requestSequence += 1; return `dashboard-${prefix}-${state.requestSequence}`; }
function unwrap(result) { return result?.structuredContent ?? result; }

function applyDashboardResult(result) {
  const data = unwrap(result);
  if (result?.isError || data?.error) { state.phase = "error"; state.error = data?.error ?? { code: "TOOL_ERROR", message: "MCP tool returned an error" }; state.dashboard = null; return; }
  state.phase = "ready"; state.error = null; state.dashboard = data;
  if (data?.report?.plan?.orders) {
    const allowed = new Set(data.report.plan.risk_decision?.allowed_order_ids ?? []);
    state.selected = new Set([...state.selected].filter((id) => allowed.has(id)));
  }
}

async function refresh() { await runTradeTool("quant_get_dashboard", { report_id: state.dashboard?.report_id }); }

async function runTradeTool(name, args) {
  if (state.pendingTool) return;
  state.pendingTool = name; state.phase = state.dashboard ? "ready" : "loading"; state.error = null; render();
  try {
    const result = await bridge.callTool(name, args); const data = unwrap(result);
    if (result?.isError || data?.error) applyDashboardResult(result);
    else if (name === "quant_get_dashboard") applyDashboardResult(result);
    else { const reportId = data?.report_id ?? data?.report?.report_id ?? state.dashboard?.report_id; applyDashboardResult(await bridge.callTool("quant_get_dashboard", { report_id: reportId })); state.selected.clear(); }
  } catch (error) { state.phase = "error"; state.error = { code: error.code ?? "MCP_DISCONNECTED", message: error.message ?? "MCP connection failed" }; }
  finally { state.pendingTool = null; render(); }
}

async function runResearch(name, args, handler) {
  if (state.pendingTool) return;
  state.pendingTool = name; state.researchError = null; render();
  try {
    const result = await bridge.callTool(name, args); const data = unwrap(result);
    if (result?.isError || data?.error) state.researchError = data?.error ?? { code: "RESEARCH_ERROR", message: "research tool failed" };
    else handler(data);
  } catch (error) { state.researchError = { code: error.code ?? "MCP_DISCONNECTED", message: error.message ?? "research bridge unavailable" }; }
  finally { state.pendingTool = null; render(); }
}

function loadChart() {
  return runResearch("quant_get_chart_data", { symbol: state.symbol, timeframe: state.timeframe, strategy_id: state.strategy?.manifest?.strategy_id ?? "moving-average-demo", version: state.strategy?.manifest?.version, max_bars: 500, report_id: state.dashboard?.report_id }, (data) => { state.chart = data; state.chartHoverIndex = null; });
}

function loadStrategies() {
  return runResearch("quant_list_strategies", {}, (data) => {
    state.strategies = data.strategies ?? [];
    const current = state.strategy?.manifest;
    const first = state.strategies.find((item) => current && item.strategy_id === current.strategy_id && item.version === current.version)
      ?? state.strategies.find((item) => current && item.strategy_id === current.strategy_id)
      ?? state.strategies.find((item) => item.strategy_id === "moving-average-demo")
      ?? state.strategies[0];
    if (first) window.setTimeout(() => loadStrategy(first.strategy_id, first.version), 0);
  });
}

function loadStrategy(strategyId, version) {
  return runResearch("quant_get_strategy", { strategy_id: strategyId, version }, (data) => { state.strategy = data; state.editorText = JSON.stringify(data.dsl, null, 2); state.labParameters = { ...(data.parameters ?? {}) }; state.strategyValidation = null; state.researchError = null; });
}

function parseEditor() {
  try { const dsl = JSON.parse(state.editorText); if (!dsl || typeof dsl !== "object" || Array.isArray(dsl)) throw new Error("DSL must be a JSON object"); return dsl; }
  catch (error) { state.strategyError = { code: "SCHEMA_INVALID", message: error.message }; render(); return null; }
}

function labPayload() {
  const dsl = parseEditor();
  return dsl ? { dsl, parameters: state.labParameters, request_id: requestId("strategy") } : null;
}

function validateEditor() { const payload = labPayload(); if (payload) runResearch("quant_validate_strategy", payload, (data) => { state.strategyValidation = data.validation; state.strategyError = null; }); }
function saveDraft() { const payload = labPayload(); if (payload) runResearch("quant_save_strategy_draft", payload, (data) => { state.strategy = data.strategy; state.strategyValidation = data.validation; state.strategyError = null; window.setTimeout(loadStrategies, 0); }); }
function researchRunPayload() { const payload = labPayload(); if (!payload) return null; return { ...payload, strategy_id: payload.dsl.strategy_id, version: payload.dsl.version, symbol: state.symbol, timeframe: state.timeframe, max_bars: Number(document.querySelector("#max-bars")?.value ?? 40) }; }
function runDebug() { const payload = researchRunPayload(); if (payload) runResearch("quant_run_strategy_debug", payload, (data) => { state.debug = data; state.debugIndex = 0; state.strategyError = null; }); }
function runBacktest() { const payload = researchRunPayload(); if (!payload) return; payload.initial_cash = document.querySelector("#initial-cash")?.value ?? "10000"; runResearch("quant_run_backtest", payload, (data) => { state.backtest = data; state.backtestHistory = [...state.backtestHistory.filter((item) => item.run_id !== data.run_id), data]; if (state.strategy?.manifest) state.strategy.manifest.status = "BACKTESTED"; state.strategyError = null; window.setTimeout(loadStrategies, 0); }); }
function compareRecent() { const runs = state.backtestHistory.slice(-2).map((item) => item.run_id); if (runs.length < 2) return; runResearch("quant_compare_backtests", { run_ids: runs, request_id: requestId("compare") }, (data) => { state.backtest.comparison = data; }); }
function promoteCandidate() { const manifest = state.strategy?.manifest; if (!manifest || !state.backtest) return; runResearch("quant_promote_strategy_candidate", { strategy_id: manifest.strategy_id, version: manifest.version, backtest_run_id: state.backtest.run_id, request_id: requestId("promote") }, (data) => { state.strategy = data.strategy; state.strategyValidation = null; window.setTimeout(loadStrategies, 0); }); }

class BridgeClient {
  constructor() {
    this.sequence = 0; this.pending = new Map(); this.onToolInput = () => {}; this.onToolResult = () => {}; this.onHostContext = (context) => applyHostContext(context);
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent && event.source !== window) return;
      const message = event.data; if (!message || message.jsonrpc !== "2.0") return;
      if (message.id !== undefined && this.pending.has(message.id)) { const pending = this.pending.get(message.id); this.pending.delete(message.id); if (message.error) pending.reject(Object.assign(new Error(message.error.message), { code: message.error.code })); else pending.resolve(message.result); return; }
      if (message.method === "ui/notifications/tool-input") this.onToolInput(message.params ?? {});
      if (message.method === "ui/notifications/tool-result") this.onToolResult(message.params?.result ?? message.params ?? {});
      if (message.method === "ui/notifications/host-context-changed") this.onHostContext(message.params ?? {});
    });
  }

  post(method, params = {}, timeout = 10000) {
    if (window.parent === window && !(window.openai && typeof window.openai.callTool === "function")) return Promise.reject(Object.assign(new Error("MCP host bridge is not available"), { code: "MCP_DISCONNECTED" }));
    const id = `ui-${++this.sequence}`;
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => { this.pending.delete(id); reject(Object.assign(new Error("MCP host did not respond in time"), { code: "MCP_TIMEOUT" })); }, timeout);
      this.pending.set(id, { resolve: (value) => { window.clearTimeout(timer); resolve(value); }, reject: (error) => { window.clearTimeout(timer); reject(error); } });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    });
  }

  async initialize() {
    try { const result = await this.post("ui/initialize", { protocolVersion: "2025-06-18", capabilities: { toolCalls: true, messages: true }, clientInfo: { name: "quant-agent-dashboard-ui", version: "0.2.0" } }); this.onHostContext(result?.hostContext ?? result?.host_context ?? {}); return result; }
    catch (error) { if (window.openai && typeof window.openai.callTool === "function") return { hostContext: {} }; throw error; }
  }

  async callTool(name, argumentsValue) { if (window.openai && typeof window.openai.callTool === "function" && window.parent === window) return window.openai.callTool(name, argumentsValue); return this.post("tools/call", { name, arguments: argumentsValue }); }
  sendMessage(message) { const params = { role: "user", content: [{ type: "text", text: message }] }; if (window.openai && typeof window.openai.sendFollowUpMessage === "function" && window.parent === window) return window.openai.sendFollowUpMessage(params); window.parent.postMessage({ jsonrpc: "2.0", id: `ui-${++this.sequence}`, method: "ui/message", params }, "*"); return Promise.resolve(); }
}

function applyHostContext(context) { state.theme = context?.theme === "light" || context?.theme === "dark" ? context.theme : state.theme; document.documentElement.dataset.theme = state.theme; render(); }

const bridge = new BridgeClient();
window.addEventListener("hashchange", () => {
  const next = routeView();
  if (state.view !== next) {
    state.view = next;
    render();
    if (next === "chart" && !state.chart) loadChart();
    if (next === "lab" && !state.strategies.length) loadStrategies();
  }
});
bridge.onToolInput = () => render();
bridge.onToolResult = (result) => { if (!state.pendingTool) { const data = unwrap(result); if (data?.schema_version === "dashboard.v1") applyDashboardResult(result); } render(); };

render();
bridge.initialize().then(async () => { await refresh(); await loadStrategies(); await loadChart(); }).catch((error) => { state.phase = "error"; state.error = { code: error.code ?? "MCP_DISCONNECTED", message: error.message ?? "MCP bridge unavailable" }; render(); });
