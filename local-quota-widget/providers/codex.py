"""Codex 额度（读取 ~/.codex/auth.json，调用 wham/usage）。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cursor import ProviderQuota, QuotaWindow


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _load_auth() -> tuple[str, str, str]:
    auth_path = _codex_home() / "auth.json"
    if not auth_path.exists():
        return "", "", f"未找到 {auth_path}，请先在 Codex CLI/App 登录"

    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "", "", f"无法读取 auth.json: {exc}"

    if not isinstance(data, dict):
        return "", "", "auth.json 格式无效"

    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}

    access = str(tokens.get("access_token") or tokens.get("accessToken") or "").strip()
    account_id = str(
        tokens.get("account_id")
        or tokens.get("accountId")
        or data.get("account_id")
        or data.get("accountId")
        or ""
    ).strip()

    if not access:
        store_mode = ""
        cfg_path = _codex_home() / "config.toml"
        if cfg_path.exists():
            try:
                text = cfg_path.read_text(encoding="utf-8")
                if "cli_auth_credentials_store" in text and '"keyring"' in text:
                    store_mode = "keyring"
                elif "cli_auth_credentials_store" in text and "'keyring'" in text:
                    store_mode = "keyring"
            except OSError:
                pass
        hint = "请在 ~/.codex/config.toml 设置 cli_auth_credentials_store = \"file\" 后重新登录"
        if store_mode == "keyring":
            return "", "", f"凭据在 Windows Credential Manager 中，{hint}"
        return "", "", "auth.json 中没有 access_token，请重新登录 Codex"

    return access, account_id, ""


def _pick_window(obj: Any) -> QuotaWindow | None:
    if not isinstance(obj, dict):
        return None
    used = obj.get("used_percent")
    if used is None:
        used = obj.get("usedPercent")
    try:
        used_f = float(used)
    except (TypeError, ValueError):
        return None
    reset = obj.get("reset_at") or obj.get("resetsAt")
    try:
        reset_i = int(reset) if reset is not None else None
    except (TypeError, ValueError):
        reset_i = None
    mins = obj.get("limit_window_seconds") or obj.get("windowDurationMins")
    label = "窗口"
    try:
        if mins is not None:
            mins_i = int(mins)
            label = "5 小时" if mins_i <= 400 else "7 天"
    except (TypeError, ValueError):
        pass
    return QuotaWindow(label, used_f, reset_i)


def _request_usage(access_token: str, account_id: str) -> Any:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "local-quota-widget/1.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_codex_quota() -> ProviderQuota:
    access, account_id, err = _load_auth()
    if err:
        return ProviderQuota(ok=False, title="Codex", error=err)

    try:
        data = _request_usage(access, account_id)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return ProviderQuota(
                ok=False,
                title="Codex",
                error="Codex 登录已过期，请重新 codex login",
            )
        body = exc.read().decode("utf-8", "replace")[:200]
        return ProviderQuota(
            ok=False,
            title="Codex",
            error=f"HTTP {exc.code}: {body or exc.reason}",
        )
    except urllib.error.URLError as exc:
        return ProviderQuota(ok=False, title="Codex", error=f"网络错误: {exc.reason}")
    except json.JSONDecodeError:
        return ProviderQuota(ok=False, title="Codex", error="Codex 返回了非 JSON 数据")
    except TimeoutError:
        return ProviderQuota(ok=False, title="Codex", error="请求超时")

    windows: list[QuotaWindow] = []
    extra: list[str] = []
    plan = str(data.get("plan_type") or data.get("planType") or "")

    rate_limit = data.get("rate_limit") or data.get("rateLimits") or {}
    if isinstance(rate_limit, dict):
        primary = rate_limit.get("primary_window") or rate_limit.get("primary")
        secondary = rate_limit.get("secondary_window") or rate_limit.get("secondary")
        w1 = _pick_window(primary)
        w2 = _pick_window(secondary)
        if w1:
            if w1.label == "窗口" and w1.used_percent is not None:
                w1.label = "5 小时"
            windows.append(w1)
        if w2:
            if w2.label == "窗口":
                w2.label = "7 天"
            windows.append(w2)

        credits = rate_limit.get("credits")
        if isinstance(credits, dict):
            balance = credits.get("balance")
            if balance not in (None, "", "0"):
                extra.append(f"Credits: {balance}")

    if not windows and isinstance(data.get("rateLimitsByLimitId"), dict):
        codex_block = data["rateLimitsByLimitId"].get("codex") or {}
        if isinstance(codex_block, dict):
            for key, label in (("primary", "5 小时"), ("secondary", "7 天")):
                w = _pick_window(codex_block.get(key))
                if w:
                    w.label = label
                    windows.append(w)
            credits = codex_block.get("credits")
            if isinstance(credits, dict) and credits.get("balance"):
                extra.append(f"Credits: {credits.get('balance')}")

    if not windows:
        return ProviderQuota(
            ok=False,
            title="Codex",
            error="未能解析额度字段，Codex API 可能已变更",
        )

    from datetime import datetime

    return ProviderQuota(
        ok=True,
        title="Codex",
        plan=plan,
        windows=windows[:3],
        extra_lines=extra,
        updated_at=datetime.now().strftime("%H:%M:%S"),
    )
