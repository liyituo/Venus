"""Git-backed version storage for explicitly shared team project files.

The module owns a private repository per team project under VENUS_DATA_DIR. It
never imports a workspace recursively: callers must name every shared root at
initialization and every commit must name the exact file paths to stage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from data_paths import data_dir

_LOCK = threading.RLock()
_GIT_TIMEOUT = 12
_MAX_PATHS = 100
_MAX_REPO_FILES = 5000
_MAX_FILE_BYTES = 1_000_000
_MAX_CHANGE_BYTES = 5_000_000
_MAX_DIFF_CHARS = 120_000
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,80}$")
_JOB_ID_RE = re.compile(r"^job_[a-f0-9]{12}$")
_CHANGE_ID_RE = re.compile(r"^chg_[a-f0-9]{12}$")
_ALLOWED_SUFFIXES = {
    ".py", ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".xml", ".sql", ".js", ".ts", ".tsx",
    ".jsx", ".css", ".html", ".htm", ".sh", ".ps1", ".bat", ".cmd",
    ".gitignore", ".dockerfile", ".example",
}
_BLOCKED_PARTS = {
    ".git", ".venus", ".pcagent", ".env", "venv", ".venv",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", ".cache", ".ssh", ".aws", ".kube", ".azure",
    ".local", "secrets", "credentials", "credential", "private", "memory",
    "sessions", "audit", "logs", "tmp", "temp", "backups",
}
_BLOCKED_NAMES = {
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
    "users.json", "audit.jsonl", "sessions.json", "chat_config.json",
    "token", "tokens", "credentials.json", "secrets.json",
}
_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[._-])(api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"passwords?|secrets?|credentials?|private[_-]?key)(?:[._-]|$)", re.I)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{25,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|"
        r"client[_-]?secret)\s*[\"']?\s*[:=]\s*[\"']?"
        r"([A-Za-z0-9/_+=.-]{24,})"),
)


class VersionError(Exception):
    """Expected collaboration/versioning error with an HTTP status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _valid_project_id(project_id: str) -> str:
    pid = str(project_id or "").strip()
    if not _PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
        raise VersionError(400, "project_id 格式无效")
    return pid


def _valid_job_id(job_id: str) -> str:
    jid = str(job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(jid):
        raise VersionError(400, "job_id 格式无效")
    return jid


def _valid_change_id(change_id: str) -> str:
    cid = str(change_id or "").strip()
    if not _CHANGE_ID_RE.fullmatch(cid):
        raise VersionError(400, "change_id 格式无效")
    return cid


def _managed_root(project_id: str) -> Path:
    pid = _valid_project_id(project_id)
    data_root = data_dir().resolve()
    projects_root = data_root / "team_projects"
    root = projects_root / pid
    if projects_root.is_symlink() or root.is_symlink():
        raise VersionError(403, "团队项目数据目录不能是符号链接")
    resolved = root.resolve()
    base = projects_root.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise VersionError(400, "项目路径越界") from exc
    return root


def _repo(project_id: str) -> Path:
    return _managed_root(project_id) / "repo"


def _changes_dir(project_id: str) -> Path:
    path = _managed_root(project_id) / "changes"
    if path.is_symlink():
        raise VersionError(403, "团队变更数据目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(project_id: str) -> Path:
    return _managed_root(project_id) / "manifest.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or tmp.is_symlink():
        raise VersionError(403, "团队元数据文件不能是符号链接")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _git(cwd: Path, *args: str, timeout: float = _GIT_TIMEOUT,
         max_output: int = 200_000, allow_truncate: bool = False) -> str:
    """Run Git with argv, fixed cwd, timeout and bounded returned output."""
    try:
        resolved_cwd = cwd.resolve(strict=True)
    except OSError as exc:
        raise VersionError(404, f"Git 工作目录不存在：{exc}") from exc
    # Git honors environment variables such as GIT_DIR/GIT_WORK_TREE and loads
    # system/global config by default. A team file can contain .gitattributes
    # filter declarations, while host config may map those filters or hooks to
    # commands. Keep every operation bound to this repository and ignore host
    # configuration so shared content cannot trigger host-level Git extensions.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0"})
    try:
        proc = subprocess.Popen(
            ["git", "--no-pager", "-c", f"core.excludesFile={os.devnull}", *args],
            cwd=resolved_cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
    except FileNotFoundError as exc:
        raise VersionError(503, "系统未安装 Git") from exc
    except OSError as exc:
        raise VersionError(503, f"Git 无法启动：{exc}") from exc
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    truncated: set[str] = set()

    def _drain(name: str, stream) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            buffer = outputs[name]
            remaining = max(0, max_output - len(buffer))
            if len(chunk) > remaining:
                truncated.add(name)
            if remaining:
                buffer.extend(chunk[:remaining])

    readers = [threading.Thread(target=_drain, args=(name, stream), daemon=True)
               for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        for reader in readers:
            reader.join(timeout=1)
        raise VersionError(504, "Git 操作超时") from exc
    for reader in readers:
        reader.join(timeout=1)
    out = bytes(outputs["stdout"]).decode("utf-8", errors="replace")
    err = bytes(outputs["stderr"]).decode("utf-8", errors="replace")
    if proc.returncode:
        raise VersionError(409, (err or out or "Git 操作失败").strip()[:1200])
    if truncated and not allow_truncate:
        raise VersionError(413, "Git 输出超过处理上限；请缩小共享路径或变更范围")
    return out.strip()


def _repo_ready(project_id: str) -> bool:
    repo = _repo(project_id)
    git_dir = repo / ".git"
    return (repo.is_dir() and not repo.is_symlink()
            and git_dir.is_dir() and not git_dir.is_symlink())


def _manifest(project_id: str) -> dict:
    path = _manifest_path(project_id)
    if path.is_symlink():
        raise VersionError(403, "团队共享范围清单不能是符号链接")
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def _reject_secret_content(rel: str, content: bytes) -> None:
    text = content.decode("utf-8", errors="ignore")
    for pattern in _SECRET_VALUE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1) if match.lastindex else match.group(0)
        if str(value).casefold().startswith((
                "your_", "your-", "replace", "change_me", "changeme",
                "example", "placeholder", "<", "xxx")):
            continue
        raise VersionError(403, f"文件疑似包含凭据，不能进入团队仓库：{rel}")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD", max_output=300)


def _safe_relative(raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if (not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
            or "\x00" in value):
        raise VersionError(400, f"共享路径必须是相对路径：{raw}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise VersionError(400, f"共享路径含非法分段：{raw}")
    lower_parts = [part.casefold() for part in parts]
    if any(part in _BLOCKED_PARTS for part in lower_parts):
        raise VersionError(403, f"禁止共享受保护路径：{raw}")
    leaf = lower_parts[-1]
    if leaf in _BLOCKED_NAMES or leaf.endswith((".pem", ".key", ".p12", ".pfx")):
        raise VersionError(403, f"禁止共享凭据或本机数据文件：{raw}")
    if _SENSITIVE_NAME_RE.search(leaf):
        raise VersionError(403, f"禁止共享疑似凭据文件：{raw}")
    if leaf == ".env" or (leaf.startswith(".env.") and leaf != ".env.example"):
        raise VersionError(403, f"禁止共享环境密钥文件：{raw}")
    suffix = Path(leaf).suffix.casefold()
    if suffix and suffix not in _ALLOWED_SUFFIXES:
        raise VersionError(403, f"文件类型不在团队共享白名单中：{raw}")
    return "/".join(parts)


def _assert_no_symlink(root: Path, rel: str, *, allow_missing_leaf: bool = True) -> Path:
    """Resolve each existing component and reject symlinks, including in-root ones."""
    candidate = root
    parts = rel.split("/")
    for i, part in enumerate(parts):
        candidate = candidate / part
        try:
            if candidate.is_symlink():
                raise VersionError(403, f"不允许通过符号链接访问团队文件：{rel}")
            if not candidate.exists() and not allow_missing_leaf:
                raise VersionError(404, f"文件不存在：{rel}")
        except OSError as exc:
            raise VersionError(400, f"无法验证路径：{rel}") from exc
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        raise VersionError(403, f"路径越界：{rel}") from exc
    return candidate


def _allowed_path(manifest: dict, rel: str) -> bool:
    for root in manifest.get("shared_paths") or []:
        selected = str(root).rstrip("/")
        if rel == selected or rel.startswith(selected + "/"):
            return True
    return False


def _copy_initial_paths(source_root: Path, repo: Path,
                        selected_paths: list[str]) -> tuple[list[str], int]:
    if not isinstance(selected_paths, list) or not selected_paths:
        raise VersionError(422, "请明确列出至少一个共享文件或共享目录")
    if len(selected_paths) > _MAX_PATHS:
        raise VersionError(422, f"共享路径不能超过 {_MAX_PATHS} 项")
    roots = list(dict.fromkeys(_safe_relative(p) for p in selected_paths))
    copied: list[str] = []
    total = 0

    def copy_file(rel: str, src: Path) -> None:
        nonlocal total
        safe = _safe_relative(rel)
        if safe in copied:
            return
        if src.is_symlink() or not src.is_file():
            return
        if len(copied) >= _MAX_REPO_FILES:
            raise VersionError(413, f"初始化文件数量超过上限（{_MAX_REPO_FILES}）")
        size = src.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise VersionError(413, f"共享文件过大（上限 1 MB）：{rel}")
        total += size
        if total > _MAX_CHANGE_BYTES:
            raise VersionError(413, "初始化共享文件总量超过 5 MB")
        _reject_secret_content(safe, src.read_bytes())
        target = _assert_no_symlink(repo, safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append(safe)

    for rel in roots:
        src = _assert_no_symlink(source_root, rel, allow_missing_leaf=False)
        if src.is_file():
            copy_file(rel, src)
            continue
        if not src.is_dir():
            raise VersionError(422, f"共享路径不是文件或目录：{rel}")
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = [d for d in sorted(dirnames)
                           if not (Path(dirpath) / d).is_symlink()
                           and d.casefold() not in _BLOCKED_PARTS]
            for filename in sorted(filenames):
                candidate = Path(dirpath) / filename
                if candidate.is_symlink():
                    continue
                rel_file = candidate.relative_to(source_root).as_posix()
                try:
                    _safe_relative(rel_file)
                except VersionError:
                    continue
                copy_file(rel_file, candidate)
    if not copied:
        raise VersionError(422, "列出的路径中没有可共享的安全文件")
    return sorted(set(copied)), total


def initialize_project(project_id: str, source_workspace: Path,
                       shared_paths: list[str], actor: dict) -> dict:
    """Create an empty private Git repo and import only listed safe files."""
    pid = _valid_project_id(project_id)
    import project_store
    if not project_store.is_team_project(pid):
        raise VersionError(404, "团队项目不存在")
    with _LOCK:
        root = _managed_root(pid)
        repo = _repo(pid)
        if repo.is_symlink():
            raise VersionError(403, "团队 Git 仓库目录不能是符号链接")
        if _repo_ready(pid):
            return project_status(pid)
        if repo.exists():
            raise VersionError(409, "团队仓库路径已存在但不是由 Venus 管理的 Git 仓库；拒绝覆盖")
        root.mkdir(parents=True, exist_ok=True)
        repo.mkdir(parents=True, exist_ok=True)
        try:
            source = source_workspace.resolve(strict=True)
            _git(repo, "init", "--initial-branch=main", max_output=1000)
            copied, total = _copy_initial_paths(source, repo, shared_paths)
            manifest = {"project_id": pid, "shared_paths": list(dict.fromkeys(
                _safe_relative(p) for p in shared_paths)), "created_at": time.time()}
            _write_json(_manifest_path(pid), manifest)
            name = str(actor.get("name") or actor.get("id") or "team member")
            uid = str(actor.get("id") or "owner")
            _git(repo, "--literal-pathspecs", "add", "-A", "--", *copied)
            _git(repo, "-c", f"user.name={name}", "-c", f"user.email={uid}@venus.invalid",
                 "commit", "--allow-empty", "-m", "Initialize shared team project")
        except VersionError:
            # This repo did not exist before this locked initialization call.
            shutil.rmtree(repo, ignore_errors=True)
            _manifest_path(pid).unlink(missing_ok=True)
            raise
        except Exception as exc:
            if repo.exists():
                shutil.rmtree(repo, ignore_errors=True)
            raise VersionError(500, f"初始化版本仓库失败：{exc}") from exc
        return {**project_status(pid), "imported_files": copied, "imported_bytes": total}


def project_status(project_id: str) -> dict:
    pid = _valid_project_id(project_id)
    if not _repo_ready(pid):
        return {"project_id": pid, "initialized": False, "head": "", "branch": "",
                "shared_paths": []}
    repo = _repo(pid)
    try:
        branch = _git(repo, "branch", "--show-current", max_output=200)
        head = _head(repo)
    except VersionError as exc:
        raise VersionError(500, f"团队版本仓库无法读取：{exc.detail}") from exc
    manifest = _manifest(pid)
    return {"project_id": pid, "initialized": True, "head": head,
            "branch": branch, "shared_paths": manifest.get("shared_paths") or []}


def _workspace_path(project_id: str, job_id: str) -> Path:
    jid = _valid_job_id(job_id)
    root = _managed_root(project_id) / "worktrees"
    path = root / jid
    if root.is_symlink() or path.is_symlink():
        raise VersionError(403, "团队任务工作目录不能经过符号链接")
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise VersionError(400, "团队任务工作目录越界") from exc
    return path


def _write_change(project_id: str, change: dict) -> None:
    _write_json(_changes_dir(project_id) / f"{change['id']}.json", change)


def create_task_workspace(project_id: str, job_id: str, actor: dict, *,
                          required_approvals: int = 1,
                          policy_version: int = 1,
                          eligible_reviewer_ids: set[str] | None = None) -> dict:
    pid = _valid_project_id(project_id)
    jid = _valid_job_id(job_id)
    if not _repo_ready(pid):
        raise VersionError(409, "团队项目尚未初始化版本仓库；请先明确设置共享路径")
    with _LOCK:
        repo = _repo(pid)
        base_sha = _head(repo)
        branch = f"venus/{jid}"
        workspace = _workspace_path(pid, jid)
        registrations = _managed_root(pid) / "workspaces"
        marker = registrations / f"{jid}.json"
        if registrations.is_symlink() or marker.is_symlink():
            raise VersionError(403, "团队工作区登记文件不能是符号链接")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            registration = _read_json(marker, {})
            if registration.get("job_id") == jid and registration.get("project_id") == pid:
                change = _read_json(_changes_dir(pid) / f"{registration.get('change_id')}.json", {})
                return {"workspace": str(workspace.resolve()), "base_sha": base_sha,
                        "change": change}
            raise VersionError(409, "任务工作目录已存在但不属于此任务，拒绝覆盖")
        _git(repo, "worktree", "add", "-b", branch, str(workspace), base_sha)
        cid = f"chg_{uuid.uuid4().hex[:12]}"
        user_id = str(actor.get("id") or "owner")
        change = {
            "id": cid, "project_id": pid, "job_id": jid, "author_id": user_id,
            "author_name": str(actor.get("name") or user_id), "base_sha": base_sha,
            "current_sha": base_sha, "modified_files": [], "purpose": "",
            "required_approvals": max(1, int(required_approvals or 1)),
            "policy_version": max(1, int(policy_version or 1)),
            "eligible_reviewer_ids": sorted({str(value) for value in
                (eligible_reviewer_ids or set()) if str(value)}),
            "approval_count": 0,
            "status": "draft", "created_at": time.time(), "updated_at": time.time(),
            "reviews": [], "commits": [], "merged_sha": "", "merge": None,
        }
        _write_change(pid, change)
        _write_json(marker, {"project_id": pid, "job_id": jid, "change_id": cid,
                            "workspace": str(workspace.resolve()), "created_at": time.time()})
        return {"workspace": str(workspace.resolve()), "base_sha": base_sha,
                "change": change}


def workspace_for_job(project_id: str, job_id: str) -> Path:
    pid = _valid_project_id(project_id)
    jid = _valid_job_id(job_id)
    workspace = _workspace_path(pid, jid)
    registrations = _managed_root(pid) / "workspaces"
    marker = registrations / f"{jid}.json"
    if registrations.is_symlink() or marker.is_symlink():
        raise VersionError(403, "团队工作区登记文件不能是符号链接")
    registration = _read_json(marker, {})
    if (registration.get("project_id") != pid or registration.get("job_id") != jid
            or Path(str(registration.get("workspace") or "")).resolve(strict=False)
            != workspace.resolve(strict=False) or not workspace.is_dir()
            or not (workspace / ".git").exists()):
        raise VersionError(404, "团队任务工作目录不存在或校验失败")
    try:
        workspace.resolve().relative_to((_managed_root(pid) / "worktrees").resolve())
    except ValueError as exc:
        raise VersionError(403, "团队任务工作目录路径校验失败") from exc
    return workspace


def workspace_status(project_id: str, job_id: str) -> dict:
    pid = _valid_project_id(project_id)
    workspace = workspace_for_job(pid, job_id)
    manifest = _manifest(pid)
    tracked = _git(workspace, "diff", "HEAD", "--name-only", "--no-renames", "--",
                   max_output=20_000).splitlines()
    untracked = _git(workspace, "ls-files", "--others", "--exclude-standard", "--",
                      max_output=20_000).splitlines()
    names = []
    for rel in sorted(set(tracked + untracked)):
        try:
            safe = _safe_relative(rel)
        except VersionError:
            continue
        if _allowed_path(manifest, safe):
            names.append(safe)
    return {"project_id": pid, "job_id": job_id, "files": names,
            "count": len(names)}


def _expand_paths(workspace: Path, manifest: dict, paths: list[str]) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise VersionError(422, "必须明确列出要提交的文件路径")
    if len(paths) > _MAX_PATHS:
        raise VersionError(422, f"提交路径不能超过 {_MAX_PATHS} 项")
    expanded: set[str] = set()
    total = 0
    for raw in paths:
        rel = _safe_relative(raw)
        if not _allowed_path(manifest, rel):
            raise VersionError(403, f"文件不在项目已指定的共享路径中：{rel}")
        candidate = _assert_no_symlink(workspace, rel)

        def iter_files() -> Any:
            if not candidate.is_dir():
                yield candidate
                return
            for dirpath, dirnames, filenames in os.walk(candidate, followlinks=False):
                dirnames[:] = [d for d in dirnames
                               if not (Path(dirpath) / d).is_symlink()]
                for filename in filenames:
                    yield Path(dirpath) / filename

        for item in iter_files():
            if item.is_symlink():
                raise VersionError(403, f"不能提交符号链接：{item}")
            item_rel = item.relative_to(workspace).as_posix()
            try:
                safe = _safe_relative(item_rel)
            except VersionError:
                continue
            if not _allowed_path(manifest, safe):
                continue
            if safe in expanded:
                continue
            if len(expanded) >= _MAX_PATHS:
                raise VersionError(413, f"单次变更文件数量超过上限（{_MAX_PATHS}）")
            if item.exists():
                if not item.is_file():
                    continue
                size = item.stat().st_size
                if size > _MAX_FILE_BYTES:
                    raise VersionError(413, f"文件过大（上限 1 MB）：{safe}")
                total += size
                _reject_secret_content(safe, item.read_bytes())
            expanded.add(safe)
    if total > _MAX_CHANGE_BYTES:
        raise VersionError(413, "单次变更文件总量超过 5 MB")
    if not expanded:
        raise VersionError(422, "没有可提交的共享文件")
    return sorted(expanded)


def get_change(project_id: str, change_id: str) -> dict:
    pid = _valid_project_id(project_id)
    cid = _valid_change_id(change_id)
    path = _changes_dir(pid) / f"{cid}.json"
    if path.is_symlink():
        raise VersionError(403, "变更单文件不能是符号链接")
    change = _read_json(path, None)
    if not isinstance(change, dict) or change.get("project_id") != pid:
        raise VersionError(404, "变更单不存在")
    return change


def list_changes(project_id: str, *, limit: int = 100) -> list[dict]:
    pid = _valid_project_id(project_id)
    if not _repo_ready(pid):
        return []
    limit = max(1, min(int(limit or 100), 200))
    rows = []
    for path in _changes_dir(pid).glob("chg_*.json"):
        if path.is_symlink():
            continue
        item = _read_json(path, None)
        if isinstance(item, dict):
            rows.append(item)
    rows.sort(key=lambda c: float(c.get("updated_at") or c.get("created_at") or 0),
              reverse=True)
    return rows[:limit]


def _change_workspace(change: dict) -> Path:
    return workspace_for_job(str(change.get("project_id") or ""),
                             str(change.get("job_id") or ""))


def _change_files(project_id: str, workspace: Path, base_sha: str,
                  current_sha: str) -> list[str]:
    if base_sha == current_sha:
        return []
    raw = _git(workspace, "diff", "--name-only", "--no-renames",
               f"{base_sha}..{current_sha}", "--", max_output=20_000).splitlines()
    manifest = _manifest(project_id)
    return sorted(rel for rel in raw if _allowed_path(manifest, rel))


def diff_change(project_id: str, change_id: str) -> dict:
    change = get_change(project_id, change_id)
    workspace = _change_workspace(change)
    files = _change_files(project_id, workspace,
                          str(change.get("base_sha") or ""),
                          str(change.get("current_sha") or ""))
    if not files:
        return {"change_id": change["id"], "files": [], "diff": ""}
    diff = _git(workspace, "diff", "--no-ext-diff", "--no-color", "--no-renames",
                "--unified=3", f"{change['base_sha']}..{change['current_sha']}",
                "--", *files, max_output=_MAX_DIFF_CHARS, allow_truncate=True)
    return {"change_id": change["id"], "files": files,
            "diff": diff[:_MAX_DIFF_CHARS],
            "truncated": len(diff) >= _MAX_DIFF_CHARS}


def commit_change(project_id: str, change_id: str, actor: dict, *,
                  paths: list[str], purpose: str, request_id: str = "") -> dict:
    pid = _valid_project_id(project_id)
    cid = _valid_change_id(change_id)
    purpose = str(purpose or "").strip()[:500]
    if not purpose:
        raise VersionError(422, "请填写本次变更目的")
    with _LOCK:
        change = get_change(pid, cid)
        request_id = str(request_id or "").strip()[:100]
        if request_id:
            previous = next((row for row in change.get("commits", [])
                             if row.get("request_id") == request_id), None)
            if previous:
                return {**change, "already_processed": True}
        if change.get("status") == "merged":
            raise VersionError(409, "已合并的变更单不能继续追加提交；请从最新版本创建新任务")
        workspace = _change_workspace(change)
        selected = _expand_paths(workspace, _manifest(pid), paths)
        # Literal pathspecs plus explicit paths prevent wildcard/pathspec expansion.
        # Clear any index entries created by tools/shell before staging only this request's paths.
        _git(workspace, "reset", "--mixed", "HEAD")
        _git(workspace, "--literal-pathspecs", "add", "-A", "--", *selected)
        staged = _git(workspace, "diff", "--cached", "--name-only", "--no-renames",
                      "--", max_output=20_000).splitlines()
        staged = [p for p in staged if p in selected]
        if not staged:
            raise VersionError(409, "所选路径没有相对当前提交的文件改动")
        total = 0
        for rel in staged:
            target = workspace / rel
            if target.exists() and target.is_file():
                total += target.stat().st_size
        if total > _MAX_CHANGE_BYTES:
            raise VersionError(413, "单次提交文件总量超过 5 MB")
        old_sha = str(change.get("current_sha") or "")
        name = str(actor.get("name") or actor.get("id") or "team member")
        uid = str(actor.get("id") or "owner")
        _git(workspace, "-c", f"user.name={name}", "-c", f"user.email={uid}@venus.invalid",
             "commit", "-m", purpose)
        current = _head(workspace)
        committed = _git(workspace, "diff-tree", "--no-commit-id", "--name-only", "-r",
                         "--no-renames", current, "--", max_output=20_000).splitlines()
        committed = sorted(set(p for p in committed if p in selected))
        for review in change.get("reviews") or []:
            if review.get("decision") == "approve" and not review.get("invalidated"):
                review["invalidated"] = True
                review["invalidated_at"] = time.time()
        change.setdefault("commits", []).append({
            "sha": current, "parent_sha": old_sha, "author_id": uid,
            "author_name": name, "paths": committed, "purpose": purpose,
            "created_at": time.time(), "request_id": request_id,
        })
        next_status = "stale" if change.get("status") == "stale" else "pending_review"
        change.update({"current_sha": current, "modified_files": _change_files(
            pid, workspace, str(change.get("base_sha") or ""), current),
            "purpose": purpose, "status": next_status, "approval_count": 0,
            "updated_at": time.time()})
        _write_change(pid, change)
        return change


def submit_change(project_id: str, change_id: str) -> dict:
    with _LOCK:
        change = get_change(project_id, change_id)
        if change.get("status") == "pending_review":
            return {**change, "already_processed": True}
        if change.get("status") not in ("draft", "rejected"):
            raise VersionError(409, f"当前状态不能提交审阅：{change.get('status')}")
        if not change.get("current_sha") or change.get("current_sha") == change.get("base_sha"):
            raise VersionError(409, "变更单还没有提交文件改动")
        if change.get("status") == "rejected":
            for review in change.get("reviews") or []:
                if not review.get("invalidated"):
                    review["invalidated"] = True
                    review["invalidated_at"] = time.time()
        change.update({"status": "pending_review", "approval_count": 0,
                       "updated_at": time.time()})
        _write_change(project_id, change)
        return change


def review_change(project_id: str, change_id: str, actor: dict, *,
                  decision: str, comment: str = "", reviewed_sha: str = "",
                  required_approvals: int | None = None,
                  active_reviewer_ids: set[str] | None = None) -> dict:
    pid = _valid_project_id(project_id)
    decision = str(decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise VersionError(422, "decision 必须是 approve 或 reject")
    with _LOCK:
        change = get_change(pid, change_id)
        if active_reviewer_ids is not None:
            _reconcile_active_reviews(change, active_reviewer_ids)
            _write_change(pid, change)
        uid = str(actor.get("id") or "owner")
        if ("eligible_reviewer_ids" in change
                and uid not in set(change.get("eligible_reviewer_ids") or [])):
            raise VersionError(403, "你不在此变更请求创建时的审阅资格范围内")
        if uid == str(change.get("author_id") or ""):
            raise VersionError(403, "变更作者不能审阅或批准自己的变更")
        if change.get("status") in ("merged", "rejected"):
            latest = next((r for r in reversed(change.get("reviews") or [])
                           if r.get("reviewer_id") == uid and r.get("reviewed_sha")
                           == change.get("current_sha") and r.get("decision") == decision
                           and not r.get("invalidated")), None)
            if latest:
                return {**change, "already_processed": True}
            raise VersionError(409, f"当前状态不能审阅：{change.get('status')}")
        if change.get("status") not in ("pending_review", "approved"):
            raise VersionError(409, f"变更单尚未提交审阅：{change.get('status')}")
        current = str(change.get("current_sha") or "")
        if not current or (reviewed_sha and reviewed_sha != current):
            raise VersionError(409, "审阅版本已变化，请刷新差异后重新审阅")
        prior = next((r for r in reversed(change.get("reviews") or [])
                      if r.get("reviewer_id") == uid and r.get("reviewed_sha") == current
                      and not r.get("invalidated")), None)
        if prior:
            if prior.get("decision") == decision:
                return {**change, "already_processed": True}
            raise VersionError(409, "你已审阅过该提交 SHA；请等待作者提交新版本后再审阅")
        if change.get("status") == "approved" and decision == "approve":
            return {**change, "already_processed": True}
        review = {"reviewer_id": uid,
                  "reviewer_name": str(actor.get("name") or uid),
                  "decision": decision, "comment": str(comment or "").strip()[:1000],
                  "reviewed_sha": current, "created_at": time.time(),
                  "invalidated": False}
        reviews = change.setdefault("reviews", [])
        if decision == "reject":
            # A rejection vetoes the current review round. Preserve the audit
            # trail, while making previous approvals in this round unusable.
            for old_review in reviews:
                if (old_review.get("decision") == "approve"
                        and old_review.get("reviewed_sha") == current
                        and not old_review.get("invalidated")):
                    old_review["invalidated"] = True
                    old_review["invalidated_at"] = time.time()
        reviews.append(review)
        required = max(1, int(change.get("required_approvals")
                              or required_approvals or 1))
        change["required_approvals"] = required
        approval_ids = {str(row.get("reviewer_id") or "") for row in reviews
                        if row.get("decision") == "approve"
                        and row.get("reviewed_sha") == current
                        and not row.get("invalidated")
                        and (active_reviewer_ids is None
                             or str(row.get("reviewer_id") or "") in active_reviewer_ids)
                        and str(row.get("reviewer_id") or "") != str(change.get("author_id") or "")}
        change["approval_count"] = len(approval_ids)
        next_status = ("rejected" if decision == "reject" else
                       "approved" if len(approval_ids) >= required else "pending_review")
        change.update({"status": next_status, "updated_at": time.time()})
        _write_change(pid, change)
        return change


def _reconcile_active_reviews(change: dict, active_reviewer_ids: set[str]) -> bool:
    """Invalidate approvals from inactive accounts while keeping the trail."""
    if change.get("status") not in ("approved", "pending_review"):
        return False
    now = time.time()
    current = str(change.get("current_sha") or "")
    changed = False
    for review in change.get("reviews") or []:
        if (review.get("decision") == "approve"
                and review.get("reviewed_sha") == current
                and not review.get("invalidated")
                and str(review.get("reviewer_id") or "") not in active_reviewer_ids):
            review.update({"invalidated": True, "invalidated_at": now,
                           "invalidated_reason": "reviewer_inactive"})
            changed = True
    approvals = {str(review.get("reviewer_id") or "")
                 for review in change.get("reviews") or []
                 if review.get("decision") == "approve"
                 and review.get("reviewed_sha") == current
                 and not review.get("invalidated")
                 and str(review.get("reviewer_id") or "") in active_reviewer_ids
                 and str(review.get("reviewer_id") or "") != str(change.get("author_id") or "")}
    count = len(approvals)
    if int(change.get("approval_count") or 0) != count:
        change["approval_count"] = count
        changed = True
    required = max(1, int(change.get("required_approvals") or 1))
    next_status = "approved" if count >= required else "pending_review"
    if change.get("status") in ("approved", "pending_review") and change.get("status") != next_status:
        change["status"] = next_status
        change["updated_at"] = now
        changed = True
    return changed


def reconcile_active_reviews(project_id: str, change_id: str,
                             active_reviewer_ids: set[str]) -> dict:
    """Persist reviewer deactivations before returning a change to the UI."""
    pid = _valid_project_id(project_id)
    with _LOCK:
        change = get_change(pid, change_id)
        if _reconcile_active_reviews(change, active_reviewer_ids):
            _write_change(pid, change)
        return change


def invalidate_reviews(project_id: str, change_id: str, *,
                      reason: str = "policy_changed",
                      policy_version: int | None = None,
                      required_approvals: int | None = None,
                      eligible_reviewer_ids: set[str] | None = None) -> dict:
    """Invalidate a review round while retaining all recorded votes."""
    pid = _valid_project_id(project_id)
    with _LOCK:
        change = get_change(pid, change_id)
        now = time.time()
        current_sha = str(change.get("current_sha") or "")
        for review in change.get("reviews") or []:
            if (not review.get("invalidated")
                    and str(review.get("reviewed_sha") or "") == current_sha):
                review.update({"invalidated": True, "invalidated_at": now,
                               "invalidated_reason": str(reason or "policy_changed")[:100]})
        if change.get("status") in ("approved", "pending_review"):
            change["status"] = "pending_review"
            change["approval_count"] = 0
        if policy_version is not None:
            change["policy_version"] = max(1, int(policy_version))
        if required_approvals is not None:
            change["required_approvals"] = max(1, min(50, int(required_approvals)))
        if eligible_reviewer_ids is not None:
            change["eligible_reviewer_ids"] = sorted({str(value) for value
                                                       in eligible_reviewer_ids
                                                       if str(value)})
        change["updated_at"] = now
        _write_change(pid, change)
        return change


def merge_change(project_id: str, change_id: str, actor: dict, *,
                 active_reviewer_ids: set[str] | None = None) -> dict:
    pid = _valid_project_id(project_id)
    with _LOCK:
        change = get_change(pid, change_id)
        if active_reviewer_ids is not None and _reconcile_active_reviews(
                change, active_reviewer_ids):
            _write_change(pid, change)
        if change.get("status") == "merged":
            return {**change, "already_processed": True}
        required = max(1, int(change.get("required_approvals") or 1))
        if active_reviewer_ids is not None:
            eligible_count = len(active_reviewer_ids -
                                 {str(change.get("author_id") or "")})
            if eligible_count < required:
                raise VersionError(403,
                                   f"当前只有 {eligible_count} 名活跃审阅成员，策略要求 {required} 名；请恢复活跃成员后再操作")
        if change.get("status") != "approved":
            raise VersionError(409, "变更尚未获其他成员批准，不能合并")
        repo = _repo(pid)
        target_branch = _git(repo, "branch", "--show-current", max_output=200)
        if target_branch != "main":
            raise VersionError(409, "项目目标分支不是 main，请先恢复项目主分支")
        target_head = _head(repo)
        if target_head != change.get("base_sha"):
            for review in change.get("reviews") or []:
                if not review.get("invalidated"):
                    review["invalidated"] = True
                    review["invalidated_at"] = time.time()
            change.update({"status": "stale", "updated_at": time.time(),
                           "stale_head": target_head})
            _write_change(pid, change)
            raise VersionError(409, "目标分支已前进，当前变更基线已过期；请显式刷新基线并重新审阅")
        workspace = _change_workspace(change)
        if _head(workspace) != change.get("current_sha"):
            raise VersionError(409, "变更分支提交 SHA 已变化，请刷新差异并重新审阅")
        approving_reviewers = {str(r.get("reviewer_id") or "")
                               for r in change.get("reviews") or []
                               if r.get("decision") == "approve"
                               and r.get("reviewed_sha") == change.get("current_sha")
                               and not r.get("invalidated")
                               and (active_reviewer_ids is None
                                    or str(r.get("reviewer_id") or "") in active_reviewer_ids)
                               and str(r.get("reviewer_id") or "")
                               != str(change.get("author_id") or "")}
        if len(approving_reviewers) < required:
            raise VersionError(403, f"当前 SHA 需要 {required} 名其他成员批准")
        _git(repo, "merge", "--ff-only", "--no-edit", f"refs/heads/venus/{change['job_id']}")
        merged_sha = _head(repo)
        change.update({"status": "merged", "merged_sha": merged_sha,
                       "updated_at": time.time(),
                       "merge": {"actor_id": str(actor.get("id") or "owner"),
                                 "actor_name": str(actor.get("name") or actor.get("id") or "owner"),
                                 "sha": merged_sha, "created_at": time.time()}})
        _write_change(pid, change)
        return change


def rebase_change(project_id: str, change_id: str, actor: dict) -> dict:
    """Explicitly refresh a stale branch; successful rebase invalidates reviews."""
    pid = _valid_project_id(project_id)
    with _LOCK:
        change = get_change(pid, change_id)
        if change.get("status") != "stale":
            raise VersionError(409, "只有版本过期的变更可以刷新基线")
        repo = _repo(pid)
        new_base = _head(repo)
        workspace = _change_workspace(change)
        old_base = str(change.get("base_sha") or "")
        if old_base == new_base:
            raise VersionError(409, "目标版本尚未变化，请刷新后重试")
        uid = str(actor.get("id") or "owner")
        name = str(actor.get("name") or uid)
        try:
            _git(workspace, "-c", f"user.name={name}", "-c",
                 f"user.email={uid}@venus.invalid", "rebase", "--onto", new_base, old_base)
        except VersionError as exc:
            try:
                _git(workspace, "rebase", "--abort")
            except VersionError:
                pass
            raise VersionError(409, f"基线刷新有文件衝突，尚未修改目标分支：{exc.detail}") from exc
        current = _head(workspace)
        for review in change.get("reviews") or []:
            if not review.get("invalidated"):
                review["invalidated"] = True
                review["invalidated_at"] = time.time()
        change.update({"base_sha": new_base, "current_sha": current,
                       "modified_files": _change_files(pid, workspace, new_base, current),
                       "status": "pending_review", "approval_count": 0,
                       "updated_at": time.time(),
                       "rebased_by": {"id": str(actor.get("id") or "owner"),
                                      "name": str(actor.get("name") or actor.get("id") or "owner"),
                                      "at": time.time()}})
        change.setdefault("history", []).append({"action": "rebase", "old_base": old_base,
                                                  "new_base": new_base,
                                                  "current_sha": current,
                                                  "actor_id": uid,
                                                  "created_at": time.time()})
        _write_change(pid, change)
        return change


def revert_change(project_id: str, change_id: str, actor: dict,
                  job_id: str, *, request_id: str = "",
                  required_approvals: int = 1, policy_version: int = 1,
                  eligible_reviewer_ids: set[str] | None = None) -> dict:
    """Build a new pending-review revert change for an already merged change."""
    pid = _valid_project_id(project_id)
    jid = _valid_job_id(job_id)
    with _LOCK:
        original = get_change(pid, change_id)
        if original.get("status") != "merged":
            raise VersionError(409, "只有已合并的变更可以回退")
        prior_revert = str(original.get("revert_change_id") or "")
        if prior_revert:
            try:
                prior = get_change(pid, prior_revert)
            except VersionError:
                prior = None
            if prior:
                if prior.get("status") == "stale" and prior.get("error"):
                    raise VersionError(
                        409, "该回退变更已记录文件冲突；请从变更列表打开它并刷新基线")
                return prior
        repo = _repo(pid)
        base = _head(repo)
        workspace = _workspace_path(pid, jid)
        if workspace.exists():
            raise VersionError(409, "回退任务工作目录已存在")
        branch = f"venus/{jid}"
        registrations = _managed_root(pid) / "workspaces"
        marker = registrations / f"{jid}.json"
        if registrations.is_symlink() or marker.is_symlink():
            raise VersionError(403, "团队工作区登记文件不能是符号链接")
        _git(repo, "worktree", "add", "-b", branch, str(workspace), base)
        cid = f"chg_{uuid.uuid4().hex[:12]}"
        uid = str(actor.get("id") or "owner")
        change = {"id": cid, "project_id": pid, "job_id": jid, "author_id": uid,
                  "author_name": str(actor.get("name") or uid), "base_sha": base,
                  "current_sha": base, "modified_files": [],
                  "required_approvals": max(1, int(required_approvals or 1)),
                  "policy_version": max(1, int(policy_version or 1)),
                  "eligible_reviewer_ids": sorted({str(value) for value in
                      (eligible_reviewer_ids or set()) if str(value)}),
                  "approval_count": 0,
                  "purpose": f"回退变更 {original['id']}", "status": "draft",
                  "created_at": time.time(), "updated_at": time.time(), "reviews": [],
                  "commits": [], "reverts_change_id": original["id"],
                  "merged_sha": "", "merge": None}
        _write_change(pid, change)
        _write_json(marker,
                    {"project_id": pid, "job_id": jid, "change_id": cid,
                     "workspace": str(workspace.resolve()), "created_at": time.time()})
        original.update({"revert_change_id": cid,
                         "revert_request_id": request_id or "",
                         "updated_at": time.time()})
        _write_change(pid, original)
        try:
            commits = _git(workspace, "rev-list", "--reverse",
                           f"{original['base_sha']}..{original['merged_sha']}",
                           max_output=20_000).splitlines()
            for commit_sha in reversed(commits):
                _git(workspace, "-c", f"user.name={change['author_name']}",
                     "-c", f"user.email={uid}@venus.invalid", "revert", "--no-edit", commit_sha)
            current = _head(workspace)
        except VersionError as exc:
            try:
                if _git(workspace, "rev-parse", "--verify", "REVERT_HEAD", max_output=500):
                    _git(workspace, "revert", "--abort")
            except VersionError:
                pass
            change["status"] = "stale"
            change["error"] = "回退文件与目标版本有冲突"
            _write_change(pid, change)
            raise VersionError(409, f"回退内容与当前文件有冲突：{exc.detail}") from exc
        change.update({"current_sha": current,
                       "modified_files": _change_files(pid, workspace, base, current),
                       "status": "pending_review", "updated_at": time.time(),
                       "commits": [{"sha": current, "parent_sha": base, "author_id": uid,
                                    "author_name": change["author_name"],
                                    "paths": _change_files(pid, workspace, base, current),
                                    "purpose": change["purpose"], "created_at": time.time()}]})
        _write_change(pid, change)
        return change
