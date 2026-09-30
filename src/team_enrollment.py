"""One-Hub/one-team enrollment, approvals, and per-device credentials.

The module intentionally stores only hashes of invitation, claim, and device
secrets. A device credential is generated when an approved applicant claims
their device; the plaintext is returned in that one response only.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import team_collab
from data_paths import data_dir

_LOCK = threading.RLock()
_TEAM_FILE = "team.json"
_ACCESS_FILE = "team_access.json"
_LOGIN_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,254}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
_INSTALLATION_CODE_RE = re.compile(
    r"^VI-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-"
    r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-"
    r"[0-9A-HJKMNP-TV-Z]{6}$")
_JOIN_ATTEMPTS: dict[str, list[float]] = {}


class EnrollmentError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _path(name: str) -> Path:
    return data_dir() / name


def _read_json(name: str, default: dict) -> dict:
    path = _path(name)
    if not path.is_file():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentError(500, f"团队数据不可读取：{name}") from exc
    if not isinstance(value, dict):
        raise EnrollmentError(500, f"团队数据格式错误：{name}")
    return value


def _write_json(name: str, value: dict) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_access() -> dict:
    value = _read_json(_ACCESS_FILE, {"invites": [], "applications": [], "devices": []})
    for key in ("invites", "applications", "devices"):
        if not isinstance(value.get(key), list):
            value[key] = []
    return value


def _save_access(value: dict) -> None:
    _write_json(_ACCESS_FILE, value)


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


_DISPLAY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _new_display_code(access: dict) -> str:
    existing = {str(row.get("display_code") or "").upper()
                for row in access.get("devices", [])}
    for _ in range(100):
        raw = "".join(secrets.choice(_DISPLAY_ALPHABET) for _ in range(8))
        code = f"VN-{raw[:4]}-{raw[4:]}"
        if code not in existing:
            return code
    raise EnrollmentError(503, "无法分配唯一终端显示码")


def normalize_login(login: str) -> str:
    value = str(login or "").strip().casefold()
    if not _LOGIN_RE.fullmatch(value):
        return ""
    return value


def _safe_id(value: str, label: str) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result):
        raise EnrollmentError(404, f"{label}不存在")
    return result


def _bootstrap_secret_key() -> str:
    return "team_bootstrap_token"


def ensure_bootstrap_credential() -> bool:
    """Create a local-only startup credential in the OS secure store once."""
    if get_team():
        return False
    try:
        from secure_store import load, store
        if not load(_bootstrap_secret_key()):
            store(_bootstrap_secret_key(), secrets.token_urlsafe(48))
        return True
    except Exception as exc:
        raise EnrollmentError(500, f"无法安全保存 Hub 初始化凭证：{exc}") from exc


def bootstrap_credential_local() -> str:
    """Read the one-time credential for the local Hub GUI only."""
    try:
        from secure_store import load
        return load(_bootstrap_secret_key()) or ""
    except Exception:
        return ""


def get_team() -> dict | None:
    with _LOCK:
        team = _read_json(_TEAM_FILE, {})
        if not team.get("team_id") or team.get("status") != "active":
            return None
        return team


def public_team() -> dict:
    team = get_team()
    if not team:
        raise EnrollmentError(404, "Hub 尚未初始化团队")
    return {"team_id": team["team_id"], "team_name": team["name"],
            "member_limit": 10, "enrollment": "tailscale_serve"}


def bootstrap_team(*, credential: str, team_name: str, admin_login: str,
                   admin_name: str, device_name: str = "Hub 管理设备") -> dict:
    name = str(team_name or "").strip()
    login = normalize_login(admin_login)
    display = str(admin_name or "").strip()[:80]
    device = str(device_name or "").strip()[:80]
    if not name or len(name) > 100:
        raise EnrollmentError(422, "团队名称须为 1–100 个字符")
    if not login:
        raise EnrollmentError(422, "请输入有效的 Tailscale 登录名")
    if not display or not device:
        raise EnrollmentError(422, "请填写管理员显示名和设备名称")
    try:
        from secure_store import delete, load
    except Exception as exc:
        raise EnrollmentError(500, "Hub 安全存储不可用") from exc
    with _LOCK:
        if get_team():
            raise EnrollmentError(409, "该 Hub 已初始化团队")
        expected = load(_bootstrap_secret_key()) or ""
        if not credential or not expected or not hmac.compare_digest(credential, expected):
            raise EnrollmentError(401, "Hub 本机初始化凭证无效或已使用")
        # Preserve old identities and their audit history, but require an
        # explicit new invitation before any legacy user can access this Hub.
        old_users = team_collab.load_users()
        for row in old_users:
            row["status"] = "legacy_untrusted"
            row["enrollment_managed"] = True
            row.setdefault("devices", [])
        team_collab.save_users(old_users)
        user_id = _new_id("u")
        device_id = _new_id("d")
        token = secrets.token_urlsafe(48)
        admin = {"id": user_id, "name": display, "role": "admin",
                 "color": "#C9573D", "status": "active",
                 "tailscale_login": login, "enrollment_managed": True,
                 "created_at": _now(), "devices": [device_id]}
        team_collab.save_users([*old_users, admin])
        team = {"team_id": _new_id("team"), "name": name,
                "status": "initializing", "created_at": _now(),
                "admin_user_id": user_id, "schema_version": 1}
        _write_json(_TEAM_FILE, team)
        access = _load_access()
        access["devices"].append({"id": device_id, "user_id": user_id,
                                  "name": device, "status": "active",
                                  "display_code": _new_display_code(access),
                                  "token_hash": team_collab.hash_token(token),
                                  "created_at": _now(), "last_used_at": _now(),
                                  "source_application_id": "bootstrap"})
        _save_access(access)
        cfg = team_collab.load_collab_config()
        cfg["team_mode"] = True
        team_collab.save_collab_config(cfg)
        team["status"] = "active"
        _write_json(_TEAM_FILE, team)
        delete(_bootstrap_secret_key())
        team_collab.audit(user_id, "team.initialize", {
            "team_id": team["team_id"], "migrated_legacy_users": len(old_users),
            "admin_login": login, "device_id": device_id})
        return {"team": public_team(), "user": team_collab.public_user(admin),
                "device_id": device_id, "device_token": token}


def is_initialized() -> bool:
    return get_team() is not None


def active_user_ids() -> set[str]:
    active_members = {str(row.get("id") or "") for row in team_collab.load_users()
                      if row.get("status", "active") == "active" and row.get("id")}
    with _LOCK:
        ready = {str(row.get("user_id") or "") for row in _load_access()["devices"]
                 if row.get("status") == "active" and row.get("token_hash")}
    return active_members & ready


def active_device_ids() -> set[str]:
    """Return device ids backed by an active, enrollment-managed Hub user.

    Unlike ``list_user_devices`` this helper is read-only and does not perform
    lazy display-code backfills, so migrations and authorization checks can
    safely use it from dry-run paths.
    """
    return {device_id for rows in active_device_ids_by_user().values()
            for device_id in rows}


def active_device_ids_by_user() -> dict[str, set[str]]:
    """Read-only mapping of active enrollment-backed devices to their users."""
    active_members = {str(row.get("id") or "") for row in team_collab.load_users()
                      if row.get("status", "active") == "active"
                      and row.get("enrollment_managed") and row.get("id")}
    with _LOCK:
        result: dict[str, set[str]] = {}
        for row in _load_access().get("devices", []):
            uid = str(row.get("user_id") or "")
            device_id = str(row.get("id") or "")
            if (uid in active_members and device_id
                    and row.get("status") == "active" and row.get("token_hash")):
                result.setdefault(uid, set()).add(device_id)
        return result


def _find_user_by_login(login: str, users: list[dict] | None = None) -> dict | None:
    target = normalize_login(login)
    rows = users if users is not None else team_collab.load_users()
    return next((row for row in rows
                 if normalize_login(str(row.get("tailscale_login") or "")) == target
                 and row.get("enrollment_managed")), None)


def _invite_by_code(access: dict, code: str) -> dict | None:
    if not code or len(code) > 256:
        return None
    digest = team_collab.hash_token(code)
    for row in access["invites"]:
        if hmac.compare_digest(str(row.get("secret_hash") or ""), digest):
            return row
    return None


def _application_holds_device_name(access: dict, row: dict) -> bool:
    if row.get("status") == "pending":
        return True
    if row.get("status") != "approved":
        return False
    device_id = str(row.get("device_id") or "")
    device = next((item for item in access["devices"]
                   if item.get("id") == device_id), None)
    return bool(device and device.get("status") in ("pending_claim", "active"))


def _normalize_installation_code(value: str | None) -> str:
    """Validate the client's public VI- Crockford Base32 display code."""
    code = str(value or "").strip().upper()
    if not code:
        return ""
    if not _INSTALLATION_CODE_RE.fullmatch(code):
        raise EnrollmentError(422, "本机安装标识格式无效")
    return code


def _find_installation_code_conflict(access: dict, code: str,
                                     *, exclude_application_id: str = "") -> bool:
    if not code:
        return False
    for row in access.get("applications", []):
        if row.get("id") == exclude_application_id:
            continue
        if (_normalize_installation_code(row.get("installation_code")) == code
                and row.get("status") in ("pending", "approved")
                and (row.get("status") == "pending"
                     or _application_holds_device_name(access, row))):
            return True
    for device in access.get("devices", []):
        if (device.get("status") in ("pending_claim", "active")
                and _normalize_installation_code(device.get("installation_code")) == code):
            return True
    return False


def preview_invite(*, code: str, tailscale_login: str) -> dict:
    team = public_team()
    actual = normalize_login(tailscale_login)
    if not actual:
        raise EnrollmentError(401, "请求缺少可信的 Tailscale 用户身份；请通过 Tailscale Serve HTTPS 地址访问")
    _check_join_rate_limit(actual)
    with _LOCK:
        access = _load_access()
        invite = _invite_by_code(access, code)
        if not invite:
            raise EnrollmentError(404, "邀请码无效")
        if invite.get("status") != "active" or float(invite.get("expires_at") or 0) <= _now():
            raise EnrollmentError(410, "邀请码已撤销或过期")
        if int(invite.get("uses") or 0) >= int(invite.get("max_uses") or 1):
            raise EnrollmentError(410, "邀请码已使用")
        if normalize_login(str(invite.get("expected_login") or "")) != actual:
            raise EnrollmentError(403, "此邀请码指定给其他 Tailscale 登录名")
    return {**team, "expected_login": invite["expected_login"],
            "role": invite.get("role", "member")}


def create_invite(*, actor: dict, expected_login: str, role: str = "member") -> dict:
    login = normalize_login(expected_login)
    if not login:
        raise EnrollmentError(422, "请输入有效的 Tailscale 登录名")
    if role not in ("admin", "member"):
        raise EnrollmentError(422, "角色只能是 admin 或 member")
    if not is_initialized():
        raise EnrollmentError(409, "Hub 尚未初始化团队")
    code = secrets.token_urlsafe(32)
    now = _now()
    record = {"id": _new_id("inv"), "expected_login": login,
              "role": role, "secret_hash": team_collab.hash_token(code),
              "status": "active", "created_at": now,
              "expires_at": now + 24 * 60 * 60, "max_uses": 1,
              "uses": 0, "created_by": str(actor.get("id") or "")}
    with _LOCK:
        access = _load_access()
        access["invites"].append(record)
        _save_access(access)
    team_collab.audit(str(actor.get("id") or "owner"), "team.invite.create", {
        "invite_id": record["id"], "expected_login": login, "role": role,
        "expires_at": record["expires_at"]})
    return {"invite": _public_invite(record), "invite_code": code,
            "team": public_team()}


def _public_invite(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("id", "expected_login", "role", "status", "created_at",
             "expires_at", "uses", "max_uses", "created_by")}


def list_invites(*, include_expired: bool = True) -> list[dict]:
    with _LOCK:
        rows = _load_access()["invites"]
        if not include_expired:
            rows = [r for r in rows if r.get("status") == "active"
                    and float(r.get("expires_at") or 0) > _now()
                    and int(r.get("uses") or 0) == 0]
        return [_public_invite(row) for row in reversed(rows)]


def revoke_invite(invite_id: str, *, actor: dict) -> dict:
    target = _safe_id(invite_id, "邀请码")
    with _LOCK:
        access = _load_access()
        row = next((r for r in access["invites"] if r.get("id") == target), None)
        if row is None:
            raise EnrollmentError(404, "邀请码不存在")
        if row.get("status") == "revoked":
            return {"invite": _public_invite(row), "already_processed": True}
        row["status"] = "revoked"
        row["revoked_at"] = _now()
        row["revoked_by"] = str(actor.get("id") or "")
        _save_access(access)
    team_collab.audit(str(actor.get("id") or "owner"), "team.invite.revoke",
                      {"invite_id": target})
    return {"invite": _public_invite(row)}


def _check_join_rate_limit(key: str) -> None:
    now = _now()
    with _LOCK:
        attempts = [ts for ts in _JOIN_ATTEMPTS.get(key, []) if now - ts < 900]
        if len(attempts) >= 8:
            raise EnrollmentError(429, "加入请求过于频繁，请 15 分钟后重试")
        attempts.append(now)
        _JOIN_ATTEMPTS[key] = attempts


def submit_application(*, code: str, tailscale_login: str, display_name: str,
                       device_name: str, claim_secret: str,
                       request_id: str, installation_code: str | None = None) -> dict:
    actual = normalize_login(tailscale_login)
    display = str(display_name or "").strip()[:80]
    device = str(device_name or "").strip()[:80]
    claim = str(claim_secret or "")
    request_id = str(request_id or "").strip()
    installation_code = _normalize_installation_code(installation_code)
    if not actual:
        raise EnrollmentError(401, "请求缺少可信的 Tailscale 用户身份")
    if not display or not device:
        raise EnrollmentError(422, "请填写显示名和设备名称")
    if len(claim.encode("utf-8")) < 32:
        raise EnrollmentError(422, "申请临时密钥不足 256 位")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,72}", request_id):
        raise EnrollmentError(422, "加入申请编号无效")
    _check_join_rate_limit(actual)
    now = _now()
    with _LOCK:
        access = _load_access()
        prior = next((row for row in access["applications"]
                      if row.get("request_id") == request_id
                      and row.get("actual_login") == actual), None)
        claim_hash = team_collab.hash_token(claim)
        if prior:
            prior_installation_code = _normalize_installation_code(
                prior.get("installation_code"))
            if (hmac.compare_digest(str(prior.get("claim_secret_hash") or ""), claim_hash)
                    and prior_installation_code == installation_code):
                return {"application": _public_application(prior),
                        "already_processed": True}
            raise EnrollmentError(409, "该加入申请编号已用于其他请求")
        invite = _invite_by_code(access, code)
        if not invite:
            raise EnrollmentError(404, "邀请码无效")
        if invite.get("status") != "active" or float(invite.get("expires_at") or 0) <= now:
            raise EnrollmentError(410, "邀请码已撤销或过期")
        if int(invite.get("uses") or 0) >= int(invite.get("max_uses") or 1):
            raise EnrollmentError(410, "邀请码已使用")
        if normalize_login(str(invite.get("expected_login") or "")) != actual:
            raise EnrollmentError(403, "此邀请码指定给其他 Tailscale 登录名")
        if _find_installation_code_conflict(access, installation_code):
            raise EnrollmentError(409, "该本机安装标识已绑定此 Hub 的其他待处理或活跃设备，请人工核对")
        duplicate = next((row for row in access["applications"]
                          if row.get("actual_login") == actual
                          and row.get("device_name") == device
                          and _application_holds_device_name(access, row)), None)
        if duplicate:
            if hmac.compare_digest(str(duplicate.get("claim_secret_hash") or ""), claim_hash):
                return {"application": _public_application(duplicate),
                        "already_processed": True}
            raise EnrollmentError(409, "该设备已有待处理的加入申请")
        existing = _find_user_by_login(actual)
        row = {"id": f"app_{request_id}", "request_id": request_id,
               "invite_id": invite["id"], "expected_login": invite["expected_login"],
               "actual_login": actual, "display_name": display,
               "installation_code": installation_code,
               "device_name": device, "user_id": str(existing.get("id") or "") if existing else "",
               "role": invite.get("role", "member"), "status": "pending",
               "created_at": now, "claim_secret_hash": claim_hash,
               "claim_used": False}
        invite["uses"] = int(invite.get("uses") or 0) + 1
        invite["used_at"] = now
        access["applications"].append(row)
        _save_access(access)
    team_collab.audit("system", "team.application.submit", {
        "application_id": row["id"], "expected_login": row["expected_login"],
        "actual_login": actual, "device_name": device})
    return {"application": _public_application(row)}


def _public_application(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("id", "expected_login", "actual_login", "display_name",
             "installation_code", "device_name", "user_id", "role", "status", "created_at",
             "updated_at", "reviewed_by", "reviewed_at", "review_note",
             "claim_used", "device_id")} | {
                 "installation_code_binding": "bound" if row.get("installation_code") else "legacy"}


def list_applications() -> list[dict]:
    with _LOCK:
        return [_public_application(row) for row in
                reversed(_load_access()["applications"])]


def application_status(application_id: str, *, tailscale_login: str) -> dict:
    target = _safe_id(application_id, "加入申请")
    login = normalize_login(tailscale_login)
    if not login:
        raise EnrollmentError(401, "请求缺少可信的 Tailscale 用户身份")
    with _LOCK:
        access = _load_access()
        row = next((r for r in access["applications"] if r.get("id") == target), None)
        if row is None or normalize_login(str(row.get("actual_login") or "")) != login:
            raise EnrollmentError(404, "加入申请不存在")
        device = next((d for d in access["devices"]
                       if d.get("id") == row.get("device_id")), None)
        user = next((u for u in team_collab.load_users()
                     if u.get("id") == row.get("user_id")), None)
        claim_available = bool(
            row.get("status") == "approved" and not row.get("claim_used")
            and device and device.get("status") == "pending_claim"
            and user and user.get("status", "active") == "active")
        return {"application": _public_application(row),
                "team": public_team(),
                "claim_available": claim_available}


def review_application(application_id: str, *, actor: dict, decision: str,
                        note: str = "") -> dict:
    target = _safe_id(application_id, "加入申请")
    if decision not in ("approve", "reject"):
        raise EnrollmentError(422, "decision 必须是 approve 或 reject")
    with _LOCK:
        access = _load_access()
        row = next((r for r in access["applications"] if r.get("id") == target), None)
        if row is None:
            raise EnrollmentError(404, "加入申请不存在")
        expected_status = "approved" if decision == "approve" else "rejected"
        if row.get("status") == expected_status:
            return {"application": _public_application(row), "already_processed": True}
        if row.get("status") != "pending":
            raise EnrollmentError(409, f"申请当前状态为 {row.get('status')}，不能重复审核")
        users = team_collab.load_users()
        login = normalize_login(str(row.get("actual_login") or ""))
        user = _find_user_by_login(login, users)
        if decision == "approve":
            installation_code = _normalize_installation_code(row.get("installation_code"))
            if _find_installation_code_conflict(
                    access, installation_code, exclude_application_id=target):
                raise EnrollmentError(409, "该本机安装标识已绑定此 Hub 的其他待处理或活跃设备，请人工核对")
            active_count = sum(1 for item in users if item.get("status", "active") == "active")
            if user is None and active_count >= 10:
                raise EnrollmentError(409, "团队已达到 10 名活跃成员上限")
            if user is None:
                user = {"id": _new_id("u"), "name": row.get("display_name") or login,
                        "role": row.get("role", "member"), "color": "#2F8A5B",
                        "status": "active", "tailscale_login": login,
                        "enrollment_managed": True, "created_at": _now(),
                        "devices": []}
                users.append(user)
            else:
                was_active = user.get("status", "active") == "active"
                user["name"] = row.get("display_name") or user.get("name") or login
                # Re-invitation explicitly reactivates the existing identity.
                # A newly approved invite controls the role when reactivating
                # an inactive member; adding another device to an active
                # member must not silently promote or demote that member.
                if not was_active:
                    user["role"] = row.get("role", "member")
                user["status"] = "active"
                user["tailscale_login"] = login
                user["enrollment_managed"] = True
            device_id = _new_id("d")
            user.setdefault("devices", []).append(device_id)
            team_collab.save_users(users)
            access["devices"].append({"id": device_id, "user_id": user["id"],
                                      "name": row["device_name"],
                                      "installation_code": installation_code,
                                      "display_code": _new_display_code(access),
                                      "status": "pending_claim", "token_hash": "",
                                      "created_at": _now(), "last_used_at": None,
                                      "source_application_id": target})
            row["user_id"] = user["id"]
            row["device_id"] = device_id
        row["status"] = expected_status
        row["updated_at"] = _now()
        row["reviewed_by"] = str(actor.get("id") or "")
        row["reviewed_at"] = _now()
        row["review_note"] = str(note or "").strip()[:500]
        _save_access(access)
    team_collab.audit(str(actor.get("id") or "owner"),
                      "team.application." + expected_status, {
                          "application_id": target, "user_id": row.get("user_id"),
                          "device_id": row.get("device_id"),
                          "decision": decision})
    return {"application": _public_application(row)}


def claim_device(application_id: str, *, tailscale_login: str,
                 claim_secret: str, installation_code: str | None = None) -> dict:
    target = _safe_id(application_id, "加入申请")
    login = normalize_login(tailscale_login)
    claim = str(claim_secret or "")
    installation_code = _normalize_installation_code(installation_code)
    if not login:
        raise EnrollmentError(401, "请求缺少可信的 Tailscale 用户身份")
    if not claim:
        raise EnrollmentError(401, "领取凭证无效")
    with _LOCK:
        access = _load_access()
        row = next((r for r in access["applications"] if r.get("id") == target), None)
        if row is None or normalize_login(str(row.get("actual_login") or "")) != login:
            raise EnrollmentError(404, "加入申请不存在")
        if row.get("status") == "rejected":
            raise EnrollmentError(403, "加入申请已被拒绝")
        if row.get("status") != "approved":
            raise EnrollmentError(409, "申请尚未批准")
        if row.get("claim_used"):
            raise EnrollmentError(410, "此设备令牌已经领取；如凭证丢失，请撤销设备后重新邀请")
        application_installation_code = _normalize_installation_code(
            row.get("installation_code"))
        if application_installation_code and installation_code != application_installation_code:
            raise EnrollmentError(409, "本机安装标识与原申请不一致")
        if not hmac.compare_digest(str(row.get("claim_secret_hash") or ""),
                                   team_collab.hash_token(claim)):
            raise EnrollmentError(401, "领取凭证无效")
        device = next((d for d in access["devices"] if d.get("id") == row.get("device_id")), None)
        if not device or device.get("status") != "pending_claim":
            raise EnrollmentError(410, "设备领取已失效，请联系管理员重新邀请")
        user = next((u for u in team_collab.load_users()
                     if u.get("id") == row.get("user_id")), None)
        if not user or user.get("status", "active") != "active":
            raise EnrollmentError(410, "团队成员已停用，请联系管理员重新邀请")
        token = secrets.token_urlsafe(48)
        device.update({"status": "active", "token_hash": team_collab.hash_token(token),
                       "last_used_at": _now(), "claimed_at": _now()})
        # Only the original application can establish this binding. A code
        # supplied while claiming a legacy request is deliberately ignored.
        row["installation_code_binding"] = (
            "bound" if application_installation_code else "legacy")
        row["claim_used"] = True
        row["claimed_at"] = _now()
        _save_access(access)
    team_collab.audit(str(row.get("user_id") or ""), "team.device.claim", {
        "application_id": target, "device_id": device["id"],
        "installation_code_binding": row.get("installation_code_binding", "legacy")})
    return {"device_id": device["id"], "device_token": token,
            "team": public_team(), "user_id": row.get("user_id")}


def authenticate_device_result(token: str, tailscale_login: str, *,
                               require_network_identity: bool = True) -> tuple[dict | None, str]:
    raw = str(token or "").strip()
    login = normalize_login(tailscale_login)
    if not raw:
        return None, "缺少团队设备凭证"
    if require_network_identity and not login:
        return None, "缺少可信的 Tailscale 用户身份"
    users = team_collab.load_users()
    with _LOCK:
        access = _load_access()
        for device in access["devices"]:
            if not team_collab.verify_token(raw, str(device.get("token_hash") or "")):
                continue
            if device.get("status") != "active":
                return None, "此设备凭证已撤销"
            user = next((u for u in users if u.get("id") == device.get("user_id")), None)
            if not user or user.get("status", "active") != "active":
                return None, "团队成员已停用"
            if require_network_identity and normalize_login(str(user.get("tailscale_login") or "")) != login:
                return None, "当前 Tailscale 身份与此设备凭证不匹配"
            now = _now()
            if now - float(device.get("last_used_at") or 0) >= 60:
                device["last_used_at"] = now
                _save_access(access)
            return ({"id": user["id"], "name": user.get("name") or user["id"],
                     "role": user.get("role") or "member",
                     "color": user.get("color") or "#888888",
                     "status": user.get("status", "active"),
                     "tailscale_login": user.get("tailscale_login"),
                     "device_id": device["id"], "device_name": device.get("name"),
                     "device_display_code": device.get("display_code") or ""}, "")
    return None, "设备凭证无效"


def authenticate_device(token: str, tailscale_login: str) -> dict | None:
    return authenticate_device_result(token, tailscale_login)[0]


def direct_invite_login(code: str) -> str:
    """A direct invitation is a bearer secret assigned by the administrator.

    The stored member label is not an externally verified email/network identity.
    The existing approval and one-use claim checks still apply.
    """
    with _LOCK:
        invite = _invite_by_code(_load_access(), code)
        if not invite:
            raise EnrollmentError(401, "邀请码无效")
        return str(invite.get("expected_login") or "")


def direct_application_login(application_id: str, claim_secret: str) -> str:
    """Status and claim require the applicant's secret, never just their label."""
    target = _safe_id(application_id, "加入申请")
    with _LOCK:
        row = next((r for r in _load_access()["applications"] if r.get("id") == target), None)
        if (not row or not claim_secret or not hmac.compare_digest(
                str(row.get("claim_secret_hash") or ""), team_collab.hash_token(claim_secret))):
            raise EnrollmentError(401, "申请凭证无效")
        return str(row.get("actual_login") or "")


def current_identity(user_id: str, device_id: str) -> dict:
    user = next((u for u in team_collab.load_users() if u.get("id") == user_id), None)
    if not user or user.get("status", "active") != "active":
        raise EnrollmentError(401, "团队成员已停用")
    with _LOCK:
        access = _load_access()
        if _ensure_display_codes(access):
            _save_access(access)
        device = next((d for d in access["devices"]
                       if d.get("id") == device_id and d.get("user_id") == user_id), None)
    if not device or device.get("status") != "active":
        raise EnrollmentError(401, "此设备凭证已撤销")
    team = public_team()
    return {"team": team, "user": team_collab.public_user(user),
            "device": _public_device(device)}


def list_user_devices(user_id: str) -> list[dict]:
    with _LOCK:
        access = _load_access()
        if _ensure_display_codes(access):
            _save_access(access)
        return [_public_device(row) for row in access["devices"]
                if row.get("user_id") == user_id]


def _public_device(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("id", "user_id", "name", "status", "created_at", "last_used_at",
             "claimed_at", "source_application_id", "display_code")}


def _ensure_display_codes(access: dict) -> bool:
    changed = False
    known: set[str] = set()
    for row in access.get("devices", []):
        code = str(row.get("display_code") or "").upper()
        if not code or code in known:
            row["display_code"] = _new_display_code(access)
            code = row["display_code"]
            changed = True
        known.add(code)
    return changed


def resolve_display_code(display_code: str) -> dict:
    """Resolve a display-only terminal label without exposing credentials."""
    normalized = re.sub(r"[^A-Z0-9]", "", str(display_code or "").upper())
    if not normalized or len(normalized) != 10 or not normalized.startswith("VN"):
        raise EnrollmentError(404, "终端显示码不存在")
    with _LOCK:
        access = _load_access()
        if _ensure_display_codes(access):
            _save_access(access)
        row = next((item for item in access["devices"]
                    if re.sub(r"[^A-Z0-9]", "", str(item.get("display_code") or "").upper())
                    == normalized), None)
        if not row or row.get("status") != "active":
            raise EnrollmentError(404, "终端显示码不存在或终端已停用")
        user = next((u for u in team_collab.load_users()
                     if u.get("id") == row.get("user_id")), None)
        if not user or user.get("status", "active") != "active":
            raise EnrollmentError(404, "终端显示码不存在或终端已停用")
        return {"user": team_collab.public_user(user),
                "device": {"device_id": str(row.get("id") or ""),
                           "display_code": str(row.get("display_code") or ""),
                           "name": str(row.get("name") or ""),
                           "status": str(row.get("status") or "")}}


def get_device(device_id: str) -> dict | None:
    target = _safe_id(device_id, "终端")
    with _LOCK:
        access = _load_access()
        if _ensure_display_codes(access):
            _save_access(access)
        row = next((item for item in access["devices"] if item.get("id") == target), None)
        return _public_device(row) if row else None


def list_members() -> list[dict]:
    users = team_collab.load_users()
    with _LOCK:
        access = _load_access()
        if _ensure_display_codes(access):
            _save_access(access)
        devices = access["devices"]
    out = []
    for user in users:
        if not user.get("enrollment_managed"):
            continue
        out.append({**team_collab.public_user(user),
                    "status": user.get("status", "active"),
                    "tailscale_login": user.get("tailscale_login") or "",
                    "created_at": user.get("created_at"),
                    "devices": [_public_device(device) for device in devices
                                if device.get("user_id") == user.get("id")]})
    return out


def revoke_device(device_id: str, *, actor: dict, own_device_id: str = "") -> dict:
    target = _safe_id(device_id, "设备")
    with _LOCK:
        access = _load_access()
        device = next((d for d in access["devices"] if d.get("id") == target), None)
        if device is None:
            raise EnrollmentError(404, "设备不存在")
        actor_id = str(actor.get("id") or "")
        if not team_collab.is_admin(actor) and not (device.get("user_id") == actor_id
                                                     and target == own_device_id):
            raise EnrollmentError(403, "只能撤销自己的当前设备")
        user = next((u for u in team_collab.load_users()
                     if u.get("id") == device.get("user_id")), None)
        if user and user.get("role") == "admin" and user.get("status", "active") == "active":
            admin_ids = {str(u.get("id")) for u in team_collab.load_users()
                         if u.get("role") == "admin" and u.get("status", "active") == "active"}
            live_admin_devices = [d for d in access["devices"]
                                  if d.get("user_id") in admin_ids and d.get("status") == "active"]
            if len(admin_ids) == 1 and len(live_admin_devices) <= 1 and device.get("status") == "active":
                raise EnrollmentError(409, "不能撤销团队最后一位管理员的最后一台有效设备")
        if device.get("status") == "revoked":
            return {"device": _public_device(device), "already_processed": True}
        device.update({"status": "revoked", "revoked_at": _now(),
                       "revoked_by": actor_id})
        _save_access(access)
    team_collab.audit(str(actor.get("id") or "owner"), "team.device.revoke", {
        "device_id": target, "user_id": device.get("user_id")})
    return {"device": _public_device(device)}


def deactivate_member(user_id: str, *, actor: dict) -> dict:
    target = _safe_id(user_id, "成员")
    with _LOCK:
        users = team_collab.load_users()
        user = next((u for u in users if u.get("id") == target
                     and u.get("enrollment_managed")), None)
        if user is None:
            raise EnrollmentError(404, "成员不存在")
        if user.get("status") == "inactive":
            return {"member": team_collab.public_user(user), "already_processed": True}
        if user.get("role") == "admin":
            active_admins = [u for u in users if u.get("role") == "admin"
                             and u.get("status", "active") == "active"]
            if len(active_admins) <= 1:
                raise EnrollmentError(409, "不能停用团队最后一位管理员")
        user["status"] = "inactive"
        user["deactivated_at"] = _now()
        user["deactivated_by"] = str(actor.get("id") or "")
        # Fail closed even if the process exits between the two atomic writes:
        # authentication checks member status before consulting device state.
        team_collab.save_users(users)
        access = _load_access()
        for device in access["devices"]:
            if (device.get("user_id") == target
                    and device.get("status") not in ("revoked", "inactive")):
                device.update({"status": "revoked", "revoked_at": _now(),
                               "revoked_by": str(actor.get("id") or "")})
        _save_access(access)
    team_collab.audit(str(actor.get("id") or "owner"), "team.member.deactivate",
                      {"user_id": target})
    return {"member": {**team_collab.public_user(user), "status": "inactive"}}


def read_audit_for(user: dict, *, limit: int = 100) -> list[dict]:
    rows = team_collab.read_audit(limit=max(1, min(int(limit or 100) * 5, 2500)))
    if team_collab.is_admin(user):
        return rows[-max(1, min(int(limit or 100), 500)):]
    uid = str(user.get("id") or "")
    own = [row for row in rows if str(row.get("user") or "") == uid
           or str((row.get("detail") or {}).get("user_id") or "") == uid]
    return own[-max(1, min(int(limit or 100), 500)):]
