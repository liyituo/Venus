import { BackendError } from "./backend-client.mjs";

export const UI_RESOURCE_URI = "ui://quant-agent-dashboard/dashboard.html";

const uiMeta = {
  ui: {
    resourceUri: UI_RESOURCE_URI,
    csp: { connectDomains: [] },
  },
};

const readOnly = { readOnlyHint: true, destructiveHint: false };
const sideEffect = { readOnlyHint: false, destructiveHint: false };

export const TOOL_DEFINITIONS = [
  {
    name: "quant_get_dashboard",
    description: "读取 Quant Agent Lab 当前 Paper Trading 仪表盘；不生成计划、不批准、不执行订单。",
    inputSchema: {
      type: "object",
      properties: { report_id: { type: "string", description: "可选的报告 ID" } },
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_generate_daily_plan",
    description: "通过 Quant Agent Lab ApplicationService 生成离线日计划报告；仅 Paper Trading。",
    inputSchema: {
      type: "object",
      properties: {
        date: { type: "string", description: "YYYY-MM-DD，可选" },
        request_id: { type: "string", minLength: 1 },
      },
      required: ["request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_get_report",
    description: "读取指定的完整结构化报告，包括计划、风险检查和订单候选。",
    inputSchema: {
      type: "object",
      properties: { report_id: { type: "string", minLength: 1 } },
      required: ["report_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_submit_approval",
    description: "提交显式的人类批准选择；后端重新验证报告版本、计划哈希、风险允许订单和审批绑定。",
    inputSchema: {
      type: "object",
      properties: {
        report_id: { type: "string", minLength: 1 },
        scope: { type: "string", enum: ["ALL", "PARTIAL"] },
        order_ids: { type: "array", items: { type: "string" } },
        approver: { type: "string", minLength: 1 },
        request_id: { type: "string", minLength: 1 },
      },
      required: ["report_id", "scope", "approver", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_reject_plan",
    description: "拒绝待审批计划；不调用任何 broker。",
    inputSchema: {
      type: "object",
      properties: {
        report_id: { type: "string", minLength: 1 },
        approver: { type: "string", minLength: 1 },
        request_id: { type: "string", minLength: 1 },
      },
      required: ["report_id", "approver", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_execute_paper_plan",
    description: "在审批有效、执行前风险复核通过且 Kill Switch 关闭时执行 PaperBroker；没有 live 模式。",
    inputSchema: {
      type: "object",
      properties: {
        report_id: { type: "string", minLength: 1 },
        request_id: { type: "string", minLength: 1 },
      },
      required: ["report_id", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_set_kill_switch",
    description: "设置项目本地 Kill Switch；启用后后端阻断新执行。",
    inputSchema: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        reason: { type: "string", minLength: 1 },
        actor: { type: "string", minLength: 1 },
        request_id: { type: "string", minLength: 1 },
      },
      required: ["enabled", "reason", "actor", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_get_execution",
    description: "读取 Paper Trading 执行结果与 reconciliation。",
    inputSchema: {
      type: "object",
      properties: { execution_id: { type: "string", minLength: 1 } },
      required: ["execution_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_get_audit_events",
    description: "读取结构化、脱敏的本地审计事件。",
    inputSchema: {
      type: "object",
      properties: {
        report_id: { type: "string" },
        limit: { type: "integer", minimum: 1, maximum: 200, default: 100 },
      },
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_get_chart_data",
    description: "读取固定离线行情快照的 OHLCV、指标、策略信号、候选订单和 Paper 成交标记；不生成新的权威交易状态。",
    inputSchema: {
      type: "object",
      properties: {
        symbol: { type: "string", minLength: 1 },
        timeframe: { type: "string", enum: ["1m", "5m", "15m", "1h", "1d"] },
        strategy_id: { type: "string" },
        version: { type: "string" },
        snapshot_id: { type: "string" },
        start: { type: "string" },
        end: { type: "string" },
        max_bars: { type: "integer", minimum: 1, maximum: 500 },
        report_id: { type: "string" },
      },
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_list_strategies",
    description: "列出版本化策略及其状态；包含声明式策略和明确禁用的 Python runner 状态。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_get_strategy",
    description: "读取策略 manifest、声明式 DSL、参数和 source_hash。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string", minLength: 1 }, version: { type: "string" } },
      required: ["strategy_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_validate_strategy",
    description: "校验安全声明式策略 DSL、参数范围、指标、规则和输出；不会执行用户 Python。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string" }, version: { type: "string" }, dsl: { type: "object" }, parameters: { type: "object" }, request_id: { type: "string", minLength: 1 } },
      required: ["request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_save_strategy_draft",
    description: "保存声明式策略草稿；保存不等于验证、Paper 候选或启用。",
    inputSchema: {
      type: "object",
      properties: { dsl: { type: "object" }, parameters: { type: "object" }, request_id: { type: "string", minLength: 1 } },
      required: ["dsl", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_run_strategy_debug",
    description: "在固定 snapshot 上运行确定性的逐根 K 线 DebugTrace；不会写账户、审批、报告或 PaperBroker。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string" }, version: { type: "string" }, parameters: { type: "object" }, symbol: { type: "string" }, timeframe: { type: "string" }, snapshot_id: { type: "string" }, start: { type: "string" }, end: { type: "string" }, max_bars: { type: "integer", minimum: 1, maximum: 500 }, run_id: { type: "string" }, request_id: { type: "string", minLength: 1 } },
      required: ["request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_get_debug_trace",
    description: "分页读取确定性策略 DebugTrace。",
    inputSchema: {
      type: "object",
      properties: { run_id: { type: "string", minLength: 1 }, start: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 200 } },
      required: ["run_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_run_backtest",
    description: "运行离线、长仓、下一根开盘执行的确定性回测；不调用 PaperBroker。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string" }, version: { type: "string" }, parameters: { type: "object" }, symbol: { type: "string" }, timeframe: { type: "string" }, snapshot_id: { type: "string" }, start: { type: "string" }, end: { type: "string" }, max_bars: { type: "integer", minimum: 1, maximum: 500 }, run_id: { type: "string" }, initial_cash: { type: "string" }, fee_bps: { type: "string" }, slippage_bps: { type: "string" }, max_position_notional: { type: "string" }, request_id: { type: "string", minLength: 1 } },
      required: ["request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_get_backtest_result",
    description: "读取持久化的离线回测结果、净值、回撤、交易、公式和假设。",
    inputSchema: {
      type: "object",
      properties: { run_id: { type: "string", minLength: 1 } },
      required: ["run_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_compare_backtests",
    description: "比较 1 到 8 个回测 run 的指标和 source_hash。",
    inputSchema: {
      type: "object",
      properties: { run_ids: { type: "array", minItems: 1, maxItems: 8, items: { type: "string" } }, request_id: { type: "string", minLength: 1 } },
      required: ["run_ids", "request_id"],
      additionalProperties: false,
    },
    annotations: readOnly,
    _meta: uiMeta,
  },
  {
    name: "quant_promote_strategy_candidate",
    description: "将已回测策略显式提升为 PAPER_CANDIDATE；不会替换当前每日策略，也不会产生交易。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string", minLength: 1 }, version: { type: "string", minLength: 1 }, backtest_run_id: { type: "string", minLength: 1 }, request_id: { type: "string", minLength: 1 } },
      required: ["strategy_id", "version", "backtest_run_id", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
  {
    name: "quant_enable_paper_strategy",
    description: "在显式确认、重新校验后创建新的 PAPER_ENABLED 版本；不会继承旧报告审批。",
    inputSchema: {
      type: "object",
      properties: { strategy_id: { type: "string", minLength: 1 }, version: { type: "string", minLength: 1 }, confirm: { type: "boolean" }, request_id: { type: "string", minLength: 1 } },
      required: ["strategy_id", "version", "confirm", "request_id"],
      additionalProperties: false,
    },
    annotations: sideEffect,
    _meta: uiMeta,
  },
];

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function requireString(args, key) {
  if (typeof args[key] !== "string" || args[key].trim() === "") {
    throw new ToolInputError(`${key} is required`);
  }
}

export class ToolInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "ToolInputError";
    this.code = "INVALID_TOOL_INPUT";
  }
}

async function callBackend(name, rawArgs, backend) {
  const args = asObject(rawArgs);
  switch (name) {
    case "quant_get_dashboard":
      return backend.getDashboard(args.report_id);
    case "quant_generate_daily_plan":
      requireString(args, "request_id");
      return backend.generateDailyPlan(args);
    case "quant_get_report":
      requireString(args, "report_id");
      return backend.getReport(args.report_id);
    case "quant_submit_approval": {
      requireString(args, "report_id");
      requireString(args, "approver");
      requireString(args, "request_id");
      if (args.scope !== "ALL" && args.scope !== "PARTIAL") {
        throw new ToolInputError("scope must be ALL or PARTIAL");
      }
      const orderIds = Array.isArray(args.order_ids) ? args.order_ids : [];
      if (args.scope === "PARTIAL" && orderIds.length === 0) {
        throw new ToolInputError("PARTIAL approval requires order_ids");
      }
      return backend.submitApproval({ ...args, order_ids: orderIds });
    }
    case "quant_reject_plan":
      requireString(args, "report_id");
      requireString(args, "approver");
      requireString(args, "request_id");
      return backend.rejectPlan(args);
    case "quant_execute_paper_plan":
      requireString(args, "report_id");
      requireString(args, "request_id");
      return backend.executePaperPlan(args);
    case "quant_set_kill_switch":
      requireString(args, "reason");
      requireString(args, "actor");
      requireString(args, "request_id");
      if (typeof args.enabled !== "boolean") throw new ToolInputError("enabled is required");
      return backend.setKillSwitch(args);
    case "quant_get_execution":
      requireString(args, "execution_id");
      return backend.getExecution(args.execution_id);
    case "quant_get_audit_events":
      return backend.getAuditEvents(args);
    case "quant_get_chart_data":
      return backend.getChartData(args);
    case "quant_list_strategies":
      return backend.listStrategies();
    case "quant_get_strategy":
      requireString(args, "strategy_id");
      return backend.getStrategy(args.strategy_id, args.version);
    case "quant_validate_strategy":
      requireString(args, "request_id");
      return backend.validateStrategy(args);
    case "quant_save_strategy_draft":
      requireString(args, "request_id");
      if (!args.dsl || typeof args.dsl !== "object" || Array.isArray(args.dsl)) throw new ToolInputError("dsl is required");
      return backend.saveStrategyDraft(args);
    case "quant_run_strategy_debug":
      requireString(args, "request_id");
      return backend.runStrategyDebug(args);
    case "quant_get_debug_trace":
      requireString(args, "run_id");
      return backend.getDebugTrace(args.run_id, args.start ?? 0, args.limit ?? 100);
    case "quant_run_backtest":
      requireString(args, "request_id");
      return backend.runBacktest(args);
    case "quant_get_backtest_result":
      requireString(args, "run_id");
      return backend.getBacktestResult(args.run_id);
    case "quant_compare_backtests":
      if (!Array.isArray(args.run_ids) || args.run_ids.length === 0) throw new ToolInputError("run_ids are required");
      requireString(args, "request_id");
      return backend.compareBacktests(args.run_ids);
    case "quant_promote_strategy_candidate":
      requireString(args, "strategy_id");
      requireString(args, "version");
      requireString(args, "backtest_run_id");
      requireString(args, "request_id");
      return backend.promoteStrategyCandidate(args);
    case "quant_enable_paper_strategy":
      requireString(args, "strategy_id");
      requireString(args, "version");
      requireString(args, "request_id");
      if (args.confirm !== true) throw new ToolInputError("confirm must be true");
      return backend.enablePaperStrategy(args);
    default:
      throw new ToolInputError(`unknown tool: ${name}`);
  }
}

function jsonText(value) {
  return JSON.stringify(value, null, 2);
}

export async function callTool(name, args, backend) {
  try {
    const data = await callBackend(name, args, backend);
    return {
      content: [{ type: "text", text: jsonText(data) }],
      structuredContent: data,
      isError: false,
    };
  } catch (error) {
    const payload = {
      code: error.code ?? (error instanceof BackendError ? error.code : "TOOL_ERROR"),
      message: error.message,
      status: error.status ?? undefined,
    };
    return {
      content: [{ type: "text", text: jsonText({ error: payload }) }],
      structuredContent: { error: payload },
      isError: true,
    };
  }
}
