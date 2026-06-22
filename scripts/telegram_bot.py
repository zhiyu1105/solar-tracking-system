#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram monitor bot for the solar dashboard.

Most commands are read-only. Mutating maintenance operations are restricted to
the configured operations topic and require a short-lived confirmation code.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.dev"
DEFAULT_REPORT_FILE = REPO_ROOT / "logs" / "latest_report.txt"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
MAX_TELEGRAM_MESSAGE = 3900
TOPIC_CHOICES = ("default", "weekly", "csv", "token", "docker", "alert", "ops", "general")
OP_CONFIRM_TTL_SECONDS = 60
OPERATION_COMMANDS = {
    "/reload",
    "/collect",
    "/update_token",
    "/restart_backend",
    "/run_weekly",
}
OPS_TOPIC_LABEL = "05-\u624b\u52d5\u64cd\u4f5c\u5be9\u6838"


SECRET_LINE_RE = re.compile(
    r"(?im)^(\s*[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|KEY)[A-Z0-9_]*\s*=\s*).+$"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]+")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


@dataclass(frozen=True)
class BotConfig:
    token: str
    chat_id: str
    admin_user_ids: set[int]
    alert_enabled: bool
    message_thread_id: str
    weekly_thread_id: str
    csv_thread_id: str
    token_thread_id: str
    docker_thread_id: str
    alert_thread_id: str
    ops_thread_id: str
    env_file: Path


@dataclass
class PendingOperation:
    name: str
    code: str
    requested_by: int
    chat_id: str
    thread_id: str
    created_at: float


PENDING_OPERATION: PendingOperation | None = None
OPERATION_RUNNING = False


class ConfigError(RuntimeError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            print(f"WARN ignoring invalid TELEGRAM_ADMIN_USER_IDS item: {part}", file=sys.stderr)
    return result


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(env_file: Path) -> BotConfig:
    file_env = load_env_file(env_file)
    env = {**file_env, **os.environ}
    return BotConfig(
        token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
        admin_user_ids=parse_admin_ids(env.get("TELEGRAM_ADMIN_USER_IDS", "")),
        alert_enabled=truthy(env.get("TELEGRAM_ALERT_ENABLED", "0")),
        message_thread_id=env.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip(),
        weekly_thread_id=env.get("TELEGRAM_WEEKLY_THREAD_ID", "").strip(),
        csv_thread_id=env.get("TELEGRAM_CSV_THREAD_ID", "").strip(),
        token_thread_id=env.get("TELEGRAM_TOKEN_THREAD_ID", "").strip(),
        docker_thread_id=env.get("TELEGRAM_DOCKER_THREAD_ID", "").strip(),
        alert_thread_id=env.get("TELEGRAM_ALERT_THREAD_ID", "").strip(),
        ops_thread_id=env.get("TELEGRAM_OPS_THREAD_ID", "").strip(),
        env_file=env_file,
    )


def redact(text: str) -> str:
    text = SECRET_LINE_RE.sub(r"\1<redacted>", text)
    text = BEARER_RE.sub("Bearer <redacted>", text)
    text = JWT_RE.sub("<jwt-redacted>", text)
    return text


def normalize_path(path: Path | str, root: Path = REPO_ROOT) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    return p


def read_text(path: Path, missing_message: str) -> str:
    if not path.exists():
        return missing_message
    return path.read_text(encoding="utf-8-sig", errors="replace")


def chunk_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> Iterable[str]:
    text = text.strip() or "(empty)"
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        yield text[:split_at].rstrip()
        text = text[split_at:].lstrip()
    yield text


def telegram_api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def ensure_send_config(config: BotConfig, chat_id: str | None = None) -> tuple[str, str]:
    if not config.alert_enabled:
        raise ConfigError("TELEGRAM_ALERT_ENABLED is not 1; skipping Telegram notification")
    if not config.token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set; skipping Telegram notification")
    target_chat_id = chat_id or config.chat_id
    if not target_chat_id:
        raise ConfigError("TELEGRAM_CHAT_ID is not set; skipping Telegram notification")
    return config.token, target_chat_id


def thread_id_for_topic(config: BotConfig, topic: str | None) -> str:
    normalized = (topic or "default").strip().lower()
    if normalized in {"", "default"}:
        return config.message_thread_id
    if normalized == "general":
        return ""
    if normalized == "weekly":
        return config.weekly_thread_id or config.message_thread_id
    if normalized == "csv":
        return config.csv_thread_id
    if normalized == "token":
        return config.token_thread_id
    if normalized == "docker":
        return config.docker_thread_id
    if normalized == "alert":
        return config.alert_thread_id
    if normalized == "ops":
        return config.ops_thread_id
    return config.message_thread_id


def send_message(
    config: BotConfig,
    text: str,
    *,
    chat_id: str | None = None,
    allow_skip: bool,
    thread_id: str | None = None,
    thread_label: str = "TELEGRAM_MESSAGE_THREAD_ID",
) -> int:
    try:
        token, target_chat_id = ensure_send_config(config, chat_id)
    except ConfigError as exc:
        if allow_skip:
            print(str(exc))
            return 0
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    selected_thread_id = thread_id if thread_id is not None else config.message_thread_id
    exit_code = 0
    for chunk in chunk_text(redact(text)):
        payload: dict[str, str | bool | int] = {
            "chat_id": target_chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if selected_thread_id:
            try:
                payload["message_thread_id"] = int(selected_thread_id)
            except ValueError:
                print(
                    f"WARN invalid {thread_label}={selected_thread_id}; sending without topic",
                    file=sys.stderr,
                )

        try:
            response = requests.post(
                telegram_api_url(token, "sendMessage"),
                data=payload,
                timeout=20,
            )
            if response.status_code != 200:
                print(
                    f"ERROR Telegram sendMessage HTTP {response.status_code}: {response.text[:300]}",
                    file=sys.stderr,
                )
                exit_code = 1
        except requests.RequestException as exc:
            print(f"ERROR Telegram sendMessage failed: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


def send_topic_message(
    config: BotConfig,
    text: str,
    *,
    topic: str,
    chat_id: str | None = None,
    allow_skip: bool,
) -> int:
    thread_id = thread_id_for_topic(config, topic)
    return send_message(
        config,
        text,
        chat_id=chat_id,
        allow_skip=allow_skip,
        thread_id=thread_id,
        thread_label=f"Telegram topic {topic}",
    )


def build_report_message(report_file: Path) -> str:
    report_file = normalize_path(report_file)
    body = read_text(
        report_file,
        f"Solar Dashboard weekly report not found\nPath: {report_file}",
    )
    return "Solar Dashboard weekly report\n\n" + redact(body)


def build_status_message(report_file: Path) -> str:
    report_file = normalize_path(report_file)
    report = read_text(
        report_file,
        f"Latest report not found\nPath: {report_file}",
    )
    selected: list[str] = []
    wanted_prefixes = (
        "Backend health",
        "Token status",
        "Illumination",
        "Data collection",
        "Cache reload",
        "Full log",
    )
    for line in report.splitlines():
        if any(line.startswith(prefix) for prefix in wanted_prefixes):
            selected.append(line)
    if not selected:
        selected = report.splitlines()[:20]
    return "Solar Dashboard status\n\n" + redact("\n".join(selected))


def run_token_check() -> str:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "z3a_check_token.py"],
            cwd=str(REPO_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )
    except Exception as exc:
        return f"Z3A token check failed to run: {exc}"

    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    header = f"Z3A token check exit={result.returncode}"
    return header + "\n\n" + redact(output.strip())


def jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return 0
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def token_days_left(token: str, now: float | None = None) -> float | None:
    exp = jwt_exp(token)
    if not exp:
        return None
    return (exp - (now if now is not None else time.time())) / 86400


def token_expiry_line(label: str, days: float | None) -> str:
    if days is None:
        return f"{label}: missing or invalid"
    if days < 0:
        return f"{label}: expired {-days:.1f} days ago"
    return f"{label}: {days:.1f} days left"


def refresh_alert_level(days: float | None) -> str | None:
    if days is None or days < 0:
        return "critical"
    if days < 7:
        return "critical"
    if days < 14:
        return "urgent"
    if days < 30:
        return "warning"
    return None


def build_token_alert_message(
    env_file: Path,
    *,
    simulate_access_days: float | None = None,
    simulate_refresh_days: float | None = None,
) -> tuple[bool, str]:
    env = load_env_file(env_file)
    access_days = (
        simulate_access_days
        if simulate_access_days is not None
        else token_days_left(env.get("Z3A_TOKEN", ""))
    )
    refresh_days = (
        simulate_refresh_days
        if simulate_refresh_days is not None
        else token_days_left(env.get("Z3A_REFRESH_TOKEN", ""))
    )

    alerts: list[str] = []
    if access_days is None or access_days < 0:
        alerts.append("critical: access token is missing, invalid, or expired")
    elif access_days < 7:
        alerts.append(f"warning: access token below 7 days ({access_days:.1f} days left)")

    refresh_level = refresh_alert_level(refresh_days)
    if refresh_level:
        if refresh_days is None:
            alerts.append("critical: refresh token/token2 is missing or invalid")
        elif refresh_days < 0:
            alerts.append(f"critical: refresh token/token2 expired {-refresh_days:.1f} days ago")
        else:
            alerts.append(
                f"{refresh_level}: refresh token/token2 below threshold "
                f"({refresh_days:.1f} days left)"
            )

    summary = "\n".join(
        [
            "Z3A token expiry alert check",
            "",
            token_expiry_line("Access token", access_days),
            token_expiry_line("Refresh token/token2", refresh_days),
        ]
    )
    if not alerts:
        return False, summary + "\n\nNo alert threshold crossed."

    return True, summary + "\n\nAlert:\n" + "\n".join(f"- {item}" for item in alerts)


def read_tail_nonempty_lines(path: Path, max_lines: int = 200) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= max_lines:
            block_size = min(8192, position)
            position -= block_size
            fh.seek(position)
            data = fh.read(block_size) + data
    return [
        line.decode("utf-8-sig", errors="replace")
        for line in data.splitlines()
        if line.strip()
    ][-max_lines:]


def latest_complete_timestamp(header: list[str], tail_lines: list[str]) -> tuple[str, bool]:
    saw_incomplete_timestamp = False
    for line in reversed(tail_lines):
        try:
            row = next(csv.reader([line]))
        except Exception:
            continue
        data = dict(zip(header, row))
        timestamp = (data.get("timestamp") or "").strip()
        if TIMESTAMP_RE.match(timestamp):
            return timestamp, saw_incomplete_timestamp
        if timestamp:
            saw_incomplete_timestamp = True

        date_value = (data.get("date") or "").strip()
        time_value = (data.get("time") or "").strip()
        if DATE_RE.match(date_value) and TIME_RE.match(time_value):
            return f"{date_value} {time_value}", saw_incomplete_timestamp
    return "unknown", saw_incomplete_timestamp


def build_csv_status(env_file: Path) -> str:
    env = load_env_file(env_file)
    raw_path = env.get("Z3A_CSV_PATH", "")
    if not raw_path:
        return f"CSV status\n\nZ3A_CSV_PATH is not set in {env_file}"

    csv_path = normalize_path(raw_path)
    if not csv_path.exists():
        return f"CSV status\n\nMissing CSV: {csv_path}"

    size_mb = csv_path.stat().st_size / (1024 * 1024)
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    latest = "unknown"
    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            header_line = fh.readline()
        tail_lines = read_tail_nonempty_lines(csv_path)
        header = next(csv.reader([header_line]))
        latest, skipped_incomplete = latest_complete_timestamp(header, tail_lines)
    except Exception as exc:
        latest = f"parse failed: {exc}"
        skipped_incomplete = False

    note = "\nNote: skipped incomplete trailing CSV row" if skipped_incomplete else ""
    return (
        "CSV status\n\n"
        f"Path: {csv_path}\n"
        f"Size: {size_mb:.1f} MB\n"
        f"Modified: {mtime}\n"
        f"Latest timestamp: {latest}"
        f"{note}"
    )


def csv_latest_date(env_file: Path) -> tuple[date | None, str]:
    env = load_env_file(env_file)
    raw_path = env.get("Z3A_CSV_PATH", "")
    if not raw_path:
        return None, f"Z3A_CSV_PATH is not set in {env_file}"

    csv_path = normalize_path(raw_path)
    if not csv_path.exists():
        return None, f"Missing CSV: {csv_path}"

    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            header_line = fh.readline()
        tail_lines = read_tail_nonempty_lines(csv_path)
        header = next(csv.reader([header_line]))
        latest, _ = latest_complete_timestamp(header, tail_lines)
        latest_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        return None, f"Unable to parse latest CSV timestamp from {csv_path}: {exc}"

    return latest_dt.date(), f"Latest CSV timestamp: {latest_dt:%Y-%m-%d %H:%M:%S}"


def latest_weekly_log(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    candidates = sorted(
        log_dir.glob("solar_weekly_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def build_log_tail(lines: int) -> str:
    log_file = latest_weekly_log()
    if not log_file:
        return f"Weekly log not found\nPath: {DEFAULT_LOG_DIR}\\solar_weekly_*.log"
    text = read_text(log_file, f"Weekly log not found\nPath: {log_file}")
    tail = "\n".join(text.splitlines()[-lines:])
    return f"Latest weekly log tail\nPath: {log_file}\n\n" + redact(tail)


def run_command(
    args: list[str],
    *,
    timeout: int,
    cwd: Path = REPO_ROOT,
) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        output = "\n".join(part for part in [result.stdout, result.stderr] if part)
        return result.returncode, redact(output.strip())
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in [exc.stdout or "", exc.stderr or ""] if part)
        return 124, redact(f"Command timed out after {timeout}s\n{output}".strip())
    except Exception as exc:
        return 1, f"Command failed to run: {exc}"


def tail_lines(text: str, lines: int = 30) -> str:
    split = text.splitlines()
    return "\n".join(split[-lines:]) if split else ""


def write_ops_log(operation: str, status: str, body: str) -> None:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = DEFAULT_LOG_DIR / f"telegram_ops_{datetime.now().strftime('%Y-%m-%d')}.log"
    record = (
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {operation} {status}\n"
        f"{redact(body).rstrip()}\n\n"
    )
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(record)


def backend_reload() -> tuple[int, str]:
    try:
        response = requests.post(
            "http://localhost:8000/api/fixed-panels/reload/",
            timeout=30,
        )
        body = response.text[:1000]
        if response.status_code != 200:
            return 1, f"Reload HTTP {response.status_code}\n{body}"
        try:
            data = response.json()
        except ValueError:
            return 0, f"Reload HTTP 200\n{body}"
        if data.get("success") is False:
            return 1, "Reload returned success=false\n" + json.dumps(data, ensure_ascii=False)
        rows = data.get("df_rows", "?")
        date_range = data.get("date_range") or {}
        start = date_range.get("start", "?")
        end = date_range.get("end", "?")
        return 0, f"Reload OK: {rows} rows, range {start} ~ {end}"
    except requests.RequestException as exc:
        return 1, f"Reload request failed: {exc}"


def build_docker_status() -> str:
    exit_code, docker_output = run_command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=solar_",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        timeout=20,
    )
    lines = ["Docker/Dashboard status", ""]
    if exit_code == 0 and docker_output:
        lines.append(docker_output)
    else:
        lines.append(f"docker ps failed exit={exit_code}")
        if docker_output:
            lines.append(docker_output)

    try:
        response = requests.get(
            "http://localhost:8000/api/fixed-panels/status/",
            timeout=10,
        )
        lines.append("")
        lines.append(f"Backend API: HTTP {response.status_code}")
    except requests.RequestException as exc:
        lines.append("")
        lines.append(f"Backend API: no response ({exc})")

    return "\n".join(lines)


def ops_help_text(config: BotConfig) -> str:
    topic_note = config.ops_thread_id or "(not configured)"
    return (
        "Operations commands\n\n"
        f"Allowed topic id: {topic_note}\n"
        "/reload - reload backend cache\n"
        "/collect - collect from latest CSV date through today and reload cache\n"
        "/update_token - update Z3A tokens from app cache, recreate backend, check status\n"
        "/restart_backend - restart backend container\n"
        "/run_weekly - start SolarWeeklyMaintenance task\n"
        "/confirm <code> - confirm pending operation\n"
        "/cancel - cancel pending operation\n\n"
        "Every operation requires a 60-second confirmation code."
    )


def is_ops_topic(config: BotConfig, message: dict) -> bool:
    thread_id = str(message.get("message_thread_id") or "")
    return bool(config.ops_thread_id) and thread_id == str(config.ops_thread_id)


def pending_expired() -> bool:
    return bool(PENDING_OPERATION and time.time() - PENDING_OPERATION.created_at > OP_CONFIRM_TTL_SECONDS)


def clear_expired_pending() -> None:
    global PENDING_OPERATION
    if pending_expired():
        PENDING_OPERATION = None


def command_args(text: str) -> list[str]:
    parts = text.strip().split()
    return parts[1:] if len(parts) > 1 else []


def operation_label(name: str) -> str:
    return {
        "/reload": "backend cache reload",
        "/collect": "Z3A collect latest CSV date through today + reload",
        "/update_token": "Z3A app-cache token update + backend recreate",
        "/restart_backend": "backend container restart",
        "/run_weekly": "SolarWeeklyMaintenance task",
    }.get(name, name)


def execute_operation(config: BotConfig, operation: PendingOperation) -> tuple[int, str]:
    if operation.name == "/reload":
        return backend_reload()

    if operation.name == "/collect":
        latest_date, latest_note = csv_latest_date(config.env_file)
        if latest_date is None:
            return 1, f"{latest_note}\nFix CSV status first, then run /collect again."

        today = date.today()
        if latest_date > today:
            return 1, f"{latest_note}\nLatest CSV date is newer than today ({today:%Y-%m-%d}); not collecting."

        start_date = latest_date.strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
        days = (today - latest_date).days + 1
        timeout = min(max(days * 420, 1800), 7200)
        code, output = run_command(
            [
                sys.executable,
                "-X",
                "utf8",
                "z3a_collect.py",
                "--pipeline",
                "--start",
                start_date,
                "--end",
                end_date,
            ],
            timeout=timeout,
        )
        summary = (
            f"{latest_note}\n"
            f"Collect range: {start_date} -> {end_date} ({days} day(s))\n"
            f"z3a_collect exit={code}\n{tail_lines(output, 35)}"
        )
        if code == 0:
            reload_code, reload_output = backend_reload()
            summary += f"\n\nReload after collect exit={reload_code}\n{reload_output}"
            code = reload_code
        return code, summary

    if operation.name == "/update_token":
        code, output = run_command(
            [
                sys.executable,
                "-X",
                "utf8",
                "scripts/update_z3a_token_from_app_cache.py",
                "--apply",
                "--recreate-backend",
                "--check-status",
            ],
            timeout=900,
        )
        return code, f"update_z3a_token_from_app_cache exit={code}\n{tail_lines(output, 35)}"

    if operation.name == "/restart_backend":
        code, output = run_command(
            ["docker", "compose", "--env-file", ".env.dev", "-f", "docker-compose-dev.yml", "restart", "backend"],
            timeout=300,
        )
        if code != 0:
            fallback_code, fallback_output = run_command(
                ["docker-compose", "-f", "docker-compose-dev.yml", "restart", "backend"],
                timeout=300,
            )
            return fallback_code, (
                f"docker compose exit={code}\n{tail_lines(output, 20)}\n\n"
                f"docker-compose fallback exit={fallback_code}\n{tail_lines(fallback_output, 20)}"
            )
        return code, output or "backend restart completed"

    if operation.name == "/run_weekly":
        return run_command(
            ["powershell.exe", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName SolarWeeklyMaintenance"],
            timeout=60,
        )

    return 1, f"Unknown operation: {operation.name}"


def help_text() -> str:
    return (
        "Solar Dashboard bot commands\n\n"
        "/status - latest compact status\n"
        "/weekly - latest full weekly report\n"
        "/allstatus - send status fanout to configured topics\n"
        "/token - Z3A token status, redacted\n"
        "/csv - CSV path and latest timestamp\n"
        "/docker - Docker and backend health\n"
        "/ops - operations help\n"
        "/reload, /collect, /update_token, /restart_backend, /run_weekly - operations, confirm required\n"
        "/log - latest weekly log tail, redacted\n"
        "/whoami - show your Telegram user id\n"
        "/help - show this help\n\n"
        "Operations are admin-only and require confirmation in the operations topic."
    )


def is_allowed_chat(config: BotConfig, chat_id: int | str) -> bool:
    return not config.chat_id or str(chat_id) == str(config.chat_id)


def is_allowed_user(config: BotConfig, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return bool(config.admin_user_ids) and user_id in config.admin_user_ids


def command_name(text: str) -> str:
    if not text.strip():
        return ""
    first = text.strip().split()[0]
    return first.split("@", 1)[0].lower()


def build_all_status_messages(
    config: BotConfig,
    report_file: Path,
    *,
    include_full_report: bool,
) -> list[tuple[str, str]]:
    weekly_message = (
        build_report_message(report_file)
        if include_full_report
        else build_status_message(report_file)
    )
    return [
        ("weekly", weekly_message),
        ("csv", build_csv_status(config.env_file)),
        ("token", run_token_check()),
        ("docker", build_docker_status()),
    ]


def reply_to_command(config: BotConfig, message: dict, args: argparse.Namespace) -> None:
    global PENDING_OPERATION, OPERATION_RUNNING

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = str(message.get("text") or "")
    cmd = command_name(text)

    if not cmd.startswith("/"):
        return

    if chat_id is None or not is_allowed_chat(config, chat_id):
        print(f"Ignoring command from chat_id={chat_id}; expected TELEGRAM_CHAT_ID={config.chat_id}")
        return

    if cmd == "/whoami":
        reply = f"Telegram user id: {user_id}\nChat id: {chat_id}"
    elif not config.admin_user_ids:
        reply = (
            "TELEGRAM_ADMIN_USER_IDS is not set, so commands are disabled.\n"
            f"Your Telegram user id: {user_id}\n"
            f"Chat id: {chat_id}"
        )
    elif not is_allowed_user(config, user_id):
        reply = f"Unauthorized Telegram user id: {user_id}"
    elif cmd == "/help" or cmd == "/start":
        reply = help_text()
    elif cmd == "/ops":
        reply = ops_help_text(config)
    elif cmd in OPERATION_COMMANDS:
        clear_expired_pending()
        if not is_ops_topic(config, message):
            reply = f"Operation commands are only allowed in topic: {OPS_TOPIC_LABEL}."
        elif OPERATION_RUNNING:
            reply = "Another operation is currently running. Try again later."
        elif PENDING_OPERATION:
            reply = (
                f"Pending operation already exists: {operation_label(PENDING_OPERATION.name)}\n"
                f"Confirm with /confirm {PENDING_OPERATION.code} or cancel with /cancel."
            )
        else:
            code = f"{secrets.randbelow(1_000_000):06d}"
            PENDING_OPERATION = PendingOperation(
                name=cmd,
                code=code,
                requested_by=int(user_id or 0),
                chat_id=str(chat_id),
                thread_id=str(message.get("message_thread_id") or ""),
                created_at=time.time(),
            )
            reply = (
                f"Pending operation: {operation_label(cmd)}\n"
                f"Confirm within {OP_CONFIRM_TTL_SECONDS}s with:\n"
                f"/confirm {code}\n\n"
                "Cancel with /cancel."
            )
    elif cmd == "/confirm":
        clear_expired_pending()
        if not is_ops_topic(config, message):
            reply = f"Confirm is only allowed in topic: {OPS_TOPIC_LABEL}."
        elif OPERATION_RUNNING:
            reply = "Another operation is currently running. Try again later."
        elif not PENDING_OPERATION:
            reply = "No pending operation."
        elif not command_args(text) or command_args(text)[0] != PENDING_OPERATION.code:
            label = operation_label(PENDING_OPERATION.name)
            PENDING_OPERATION = None
            reply = (
                "Invalid confirmation code. Pending operation cancelled: "
                f"{label}\nRun the operation command again to create a new code."
            )
        elif int(user_id or 0) != PENDING_OPERATION.requested_by:
            reply = "Only the user who requested the operation can confirm it."
        else:
            operation = PENDING_OPERATION
            PENDING_OPERATION = None
            OPERATION_RUNNING = True
            started = datetime.now()
            try:
                exit_code, output = execute_operation(config, operation)
                elapsed = (datetime.now() - started).total_seconds()
                status = "OK" if exit_code == 0 else f"FAIL exit={exit_code}"
                reply = (
                    f"Operation finished: {operation_label(operation.name)}\n"
                    f"Status: {status}\n"
                    f"Elapsed: {elapsed:.1f}s\n\n"
                    f"{tail_lines(output, 25)}"
                )
                write_ops_log(operation.name, status, reply)
                if exit_code != 0:
                    send_topic_message(
                        config,
                        reply,
                        topic="alert",
                        allow_skip=False,
                    )
            finally:
                OPERATION_RUNNING = False
    elif cmd == "/cancel":
        clear_expired_pending()
        if not is_ops_topic(config, message):
            reply = f"Cancel is only allowed in topic: {OPS_TOPIC_LABEL}."
        elif PENDING_OPERATION:
            label = operation_label(PENDING_OPERATION.name)
            PENDING_OPERATION = None
            reply = f"Cancelled pending operation: {label}"
        else:
            reply = "No pending operation."
    elif cmd == "/status":
        reply = build_status_message(args.report_file)
    elif cmd == "/weekly":
        reply = build_report_message(args.report_file)
    elif cmd == "/allstatus":
        exit_code = 0
        for topic, topic_message in build_all_status_messages(
            config,
            args.report_file,
            include_full_report=False,
        ):
            result = send_topic_message(config, topic_message, topic=topic, allow_skip=False)
            if result != 0:
                exit_code = result
        if exit_code == 0:
            reply = "All status messages sent: weekly, csv, token."
        else:
            reply = f"All status fanout finished with error code {exit_code}. Check bot log."
    elif cmd == "/token":
        reply = run_token_check()
    elif cmd == "/csv":
        reply = build_csv_status(config.env_file)
    elif cmd == "/docker":
        reply = build_docker_status()
    elif cmd == "/log":
        reply = build_log_tail(args.log_lines)
    else:
        reply = "Unknown command. Use /help."

    # Command replies should stay where the command was sent. The configured
    # default topic is only for scheduled/report-style notifications.
    command_thread_id = message.get("message_thread_id")
    thread_id = str(command_thread_id) if command_thread_id is not None else ""
    send_message(config, reply, chat_id=str(chat_id), allow_skip=False, thread_id=thread_id)


def run_poll(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    if not config.token:
        print(f"ERROR TELEGRAM_BOT_TOKEN is not set in {args.env_file}", file=sys.stderr)
        return 2

    offset = args.offset
    print("Telegram polling started. Press Ctrl+C to stop.")
    while True:
        try:
            params: dict[str, int | str] = {
                "timeout": args.timeout,
                "allowed_updates": json.dumps(["message"]),
            }
            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                telegram_api_url(config.token, "getUpdates"),
                params=params,
                timeout=args.timeout + 10,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                print(f"Telegram getUpdates returned not ok: {payload}")
                time.sleep(args.retry_delay)
                continue

            updates = payload.get("result", [])
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message")
                if message:
                    reply_to_command(config, message, args)

            if args.once:
                return 0
        except KeyboardInterrupt:
            print("Telegram polling stopped.")
            return 0
        except Exception as exc:
            print(f"WARN polling error: {exc}", file=sys.stderr)
            if args.once:
                return 1
            time.sleep(args.retry_delay)


def cmd_send_report(args: argparse.Namespace) -> int:
    message = build_report_message(args.report_file)
    if args.dry_run:
        print(message)
        return 0
    return send_topic_message(load_config(args.env_file), message, topic=args.topic, allow_skip=True)


def cmd_status(args: argparse.Namespace) -> int:
    print(build_status_message(args.report_file))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    message = args.message or (
        "Solar Dashboard Telegram test\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def cmd_send_csv_status(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    message = build_csv_status(config.env_file)
    if args.dry_run:
        print(message)
        return 0
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def cmd_send_token_status(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    message = run_token_check()
    if args.dry_run:
        print(message)
        return 0
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def cmd_send_docker_status(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    message = build_docker_status()
    if args.dry_run:
        print(message)
        return 0
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def cmd_send_all_status(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    messages = build_all_status_messages(
        config,
        args.report_file,
        include_full_report=args.include_full_report,
    )

    if args.dry_run:
        for topic, message in messages:
            print(f"===== topic: {topic} =====")
            print(message)
            print()
        return 0

    exit_code = 0
    for topic, message in messages:
        result = send_topic_message(config, message, topic=topic, allow_skip=False)
        if result != 0:
            exit_code = result
    return exit_code


def cmd_send_alert(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    message = args.message.strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    if not message:
        print("ERROR send-alert requires --message or stdin text", file=sys.stderr)
        return 2
    message = "Solar Dashboard alert\n\n" + message
    if args.dry_run:
        print(redact(message))
        return 0
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def cmd_check_token_alert(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    has_alert, message = build_token_alert_message(
        args.env_file,
        simulate_access_days=args.simulate_access_days,
        simulate_refresh_days=args.simulate_refresh_days,
    )
    message = "Solar Dashboard alert\n\n" + message
    if args.dry_run:
        print(redact(message))
        return 0
    if not has_alert:
        print("No token alert threshold crossed.")
        return 0
    return send_topic_message(config, message, topic=args.topic, allow_skip=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Solar dashboard Telegram monitor bot")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Path to .env.dev. Defaults to repo root .env.dev.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    send_report = sub.add_parser("send-report", help="Send logs/latest_report.txt to Telegram")
    send_report.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    send_report.add_argument("--topic", choices=TOPIC_CHOICES, default="weekly")
    send_report.add_argument("--dry-run", action="store_true")
    send_report.set_defaults(func=cmd_send_report)

    status = sub.add_parser("status", help="Print compact local status")
    status.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    status.set_defaults(func=cmd_status)

    test = sub.add_parser("test", help="Send a test Telegram message")
    test.add_argument("--message", default="")
    test.add_argument("--topic", choices=TOPIC_CHOICES, default="weekly")
    test.set_defaults(func=cmd_test)

    csv_status = sub.add_parser("send-csv-status", help="Send CSV freshness status to Telegram")
    csv_status.add_argument("--topic", choices=TOPIC_CHOICES, default="csv")
    csv_status.add_argument("--dry-run", action="store_true")
    csv_status.set_defaults(func=cmd_send_csv_status)

    token_status = sub.add_parser("send-token-status", help="Send Z3A token status to Telegram")
    token_status.add_argument("--topic", choices=TOPIC_CHOICES, default="token")
    token_status.add_argument("--dry-run", action="store_true")
    token_status.set_defaults(func=cmd_send_token_status)

    docker_status = sub.add_parser("send-docker-status", help="Send Docker/backend status to Telegram")
    docker_status.add_argument("--topic", choices=TOPIC_CHOICES, default="docker")
    docker_status.add_argument("--dry-run", action="store_true")
    docker_status.set_defaults(func=cmd_send_docker_status)

    all_status = sub.add_parser(
        "send-all-status",
        help="Send weekly summary, CSV, token, and Docker status to their configured topics",
    )
    all_status.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    all_status.add_argument("--include-full-report", action="store_true")
    all_status.add_argument("--dry-run", action="store_true")
    all_status.set_defaults(func=cmd_send_all_status)

    alert = sub.add_parser("send-alert", help="Send a proactive alert message to Telegram")
    alert.add_argument("--message", default="")
    alert.add_argument("--topic", choices=TOPIC_CHOICES, default="alert")
    alert.add_argument("--dry-run", action="store_true")
    alert.set_defaults(func=cmd_send_alert)

    token_alert = sub.add_parser(
        "check-token-alert",
        help="Send an alert when Z3A token expiry crosses configured thresholds",
    )
    token_alert.add_argument("--topic", choices=TOPIC_CHOICES, default="alert")
    token_alert.add_argument("--dry-run", action="store_true")
    token_alert.add_argument("--simulate-access-days", type=float, default=None)
    token_alert.add_argument("--simulate-refresh-days", type=float, default=None)
    token_alert.set_defaults(func=cmd_check_token_alert)

    poll = sub.add_parser("poll", help="Run read-only Telegram long polling")
    poll.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    poll.add_argument("--timeout", type=int, default=25)
    poll.add_argument("--retry-delay", type=float, default=5.0)
    poll.add_argument("--offset", type=int, default=None)
    poll.add_argument("--once", action="store_true")
    poll.add_argument("--log-lines", type=int, default=60)
    poll.set_defaults(func=run_poll)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
