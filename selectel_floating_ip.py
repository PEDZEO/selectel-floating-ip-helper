#!/usr/bin/env python3
"""Selectel floating IP helper."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import socket
import secrets
import sys
import time
import http.client
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_BASE = "https://api.selectel.ru/vpc/resell/v2"
SCRIPT_DIR = Path(__file__).resolve().parent
TRANSIENT_HTTP_STATUS_CODES = {408, 500, 502, 503, 504}
ENV_PATH = SCRIPT_DIR / ".env"
LOG_DIR = SCRIPT_DIR / "logs"


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, details: str = "") -> None:
        self.status_code = status_code
        self.details = details
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, *, required: bool = True, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def env_int(name: str, default: int) -> int:
    value = env(name, required=False, default=str(default))
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise SystemExit(f"{name} must be an integer") from error


def env_float(name: str, default: float) -> float:
    value = env(name, required=False, default=str(default))
    try:
        return float(str(value))
    except (TypeError, ValueError) as error:
        raise SystemExit(f"{name} must be a number") from error


def sleep_with_jitter(min_seconds: float, max_seconds: float) -> None:
    if max_seconds <= 0:
        return
    if min_seconds < 0:
        min_seconds = 0
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    duration = random.uniform(min_seconds, max_seconds)
    time.sleep(duration)
    return duration


def init_log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d')}.log"


def append_log_line(log_path: Path | None, message: str) -> None:
    if not log_path:
        return
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {message}\n")


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def confirm_continue_on_existing_match(address: str, floatingip_id: str) -> bool:
    if not sys.stdin.isatty():
        print(
            f"\u041d\u0430\u0439\u0434\u0435\u043d \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0439 "
            f"\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 IP {address} "
            f"(id={floatingip_id}). "
            "\u041e\u0431\u043d\u0430\u0440\u0443\u0436\u0435\u043d "
            "\u043d\u0435\u0438\u043d\u0442\u0435\u0440\u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0439 "
            "\u0440\u0435\u0436\u0438\u043c, \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 "
            "\u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439.",
            file=sys.stderr,
        )
        return False

    prompt = (
        f"\u041d\u0430\u0439\u0434\u0435\u043d \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0439 "
        f"\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 IP {address} "
        f"(id={floatingip_id}). "
        "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c "
        "\u043f\u043e\u0438\u0441\u043a \u0434\u0440\u0443\u0433\u043e\u0433\u043e IP? [y/N]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "\u0434", "\u0434\u0430"}


def attempts_label(max_attempts: int) -> str:
    return "unlimited" if max_attempts <= 0 else str(max_attempts)


def telegram_enabled() -> bool:
    bot_token = str(env("TELEGRAM_BOT_TOKEN", required=False, default="") or "").strip()
    chat_id = str(env("TELEGRAM_CHAT_ID", required=False, default="") or "").strip()
    return bool(bot_token and chat_id)


def telegram_api_request(method: str, payload: dict | None = None) -> dict:
    bot_token = str(env("TELEGRAM_BOT_TOKEN", required=False, default="") or "").strip()
    chat_id = str(env("TELEGRAM_CHAT_ID", required=False, default="") or "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("Telegram is not configured")

    request_timeout = env_float("SELECTEL_HTTP_TIMEOUT_SECONDS", 30.0)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/{method}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=request_timeout) as response:
        body = response.read().decode("utf-8", errors="replace").strip()
        result = json.loads(body) if body else {}
    if not result.get("ok", False):
        description = result.get("description") or f"telegram {method} failed"
        raise RuntimeError(description)
    return result


def send_telegram_message(message: str, *, reply_markup: dict | None = None) -> dict | None:
    bot_token = str(env("TELEGRAM_BOT_TOKEN", required=False, default="") or "").strip()
    chat_id = str(env("TELEGRAM_CHAT_ID", required=False, default="") or "").strip()
    if not bot_token or not chat_id:
        return None

    payload: dict[str, object] = {"chat_id": chat_id, "text": message}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return telegram_api_request("sendMessage", payload)
    except Exception as error:
        print(f"Telegram notify failed: {error}", file=sys.stderr)
        return None


def notify_success(message: str) -> None:
    print(message)
    send_telegram_message(message)


def env_flag(name: str, default: bool = False) -> bool:
    value = str(env(name, required=False, default="1" if default else "0") or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def telegram_confirmation_enabled() -> bool:
    return telegram_enabled() and env_flag("SELECTEL_TELEGRAM_CONFIRM_MATCH", default=False)


def normalize_chat_id(raw_chat_id: object) -> str:
    return str(raw_chat_id or "").strip()


def safe_telegram_call(method: str, payload: dict | None = None) -> dict | None:
    try:
        return telegram_api_request(method, payload)
    except Exception as error:
        print(f"Telegram {method} failed: {error}", file=sys.stderr)
        return None


def get_telegram_updates(offset: int | None = None, timeout: int = 0) -> list[dict]:
    payload: dict[str, object] = {"timeout": timeout, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    result = telegram_api_request("getUpdates", payload)
    updates = result.get("result", [])
    return updates if isinstance(updates, list) else []


def next_telegram_update_offset() -> int | None:
    try:
        updates = get_telegram_updates(timeout=0)
    except Exception as error:
        print(f"Telegram update probe failed: {error}", file=sys.stderr)
        return None
    if not updates:
        return None
    return max(int(update.get("update_id", 0)) for update in updates) + 1


def answer_telegram_callback(callback_query_id: str, text: str) -> None:
    safe_telegram_call(
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text, "show_alert": False},
    )


def edit_telegram_message(chat_id: str, message_id: int, text: str) -> None:
    safe_telegram_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        },
    )


def wait_for_telegram_match_confirmation(message: str) -> str | None:
    if not telegram_confirmation_enabled():
        return None

    decision_token = secrets.token_hex(6)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Сохранить и искать дальше", "callback_data": f"keep_continue:{decision_token}"},
                {"text": "Сохранить и остановить", "callback_data": f"keep_stop:{decision_token}"},
            ],
            [
                {"text": "Удалить и искать дальше", "callback_data": f"delete_continue:{decision_token}"},
            ],
        ]
    }
    sent = send_telegram_message(message, reply_markup=keyboard)
    if not sent:
        return "telegram_unavailable"

    sent_message = sent.get("result", {}) if isinstance(sent, dict) else {}
    message_id = int(sent_message.get("message_id", 0) or 0)
    expected_chat_id = normalize_chat_id(env("TELEGRAM_CHAT_ID", required=False, default=""))
    timeout_seconds = env_int("SELECTEL_TELEGRAM_CONFIRM_TIMEOUT_SECONDS", 600)
    default_action = str(
        env(
            "SELECTEL_TELEGRAM_CONFIRM_DEFAULT_ACTION",
            required=False,
            default="keep_stop",
        )
        or "keep_stop"
    ).strip().lower()
    valid_actions = {"keep_continue", "keep_stop", "delete_continue"}
    if default_action not in valid_actions:
        raise SystemExit(
            "SELECTEL_TELEGRAM_CONFIRM_DEFAULT_ACTION must be one of: "
            "keep_continue, keep_stop, delete_continue"
        )

    offset = next_telegram_update_offset()
    deadline = time.time() + max(1, timeout_seconds)

    while time.time() < deadline:
        poll_timeout = min(30, max(1, int(deadline - time.time())))
        try:
            updates = get_telegram_updates(offset=offset, timeout=poll_timeout)
        except Exception as error:
            print(f"Telegram confirmation polling failed: {error}", file=sys.stderr)
            time.sleep(3)
            continue

        for update in updates:
            update_id = int(update.get("update_id", 0) or 0)
            offset = update_id + 1
            callback_query = update.get("callback_query")
            if not isinstance(callback_query, dict):
                continue

            callback_data = str(callback_query.get("data") or "")
            callback_id = str(callback_query.get("id") or "")
            callback_message = callback_query.get("message") or {}
            callback_chat_id = normalize_chat_id((callback_message.get("chat") or {}).get("id"))

            if not callback_data.endswith(f":{decision_token}"):
                continue
            if callback_chat_id != expected_chat_id:
                answer_telegram_callback(callback_id, "Это решение не для этого чата.")
                continue

            action = callback_data.split(":", 1)[0]
            if action not in valid_actions:
                answer_telegram_callback(callback_id, "Неизвестное действие.")
                continue

            answer_telegram_callback(callback_id, f"Принято: {action}")
            if message_id:
                action_labels = {
                    "keep_continue": "Сохранить и искать дальше",
                    "keep_stop": "Сохранить и остановить",
                    "delete_continue": "Удалить и искать дальше",
                }
                edit_telegram_message(
                    expected_chat_id,
                    message_id,
                    f"{message}\n\nРешение: {action_labels[action]}",
                )
            return action

    if message_id:
        timeout_labels = {
            "keep_continue": "Сохранить и искать дальше",
            "keep_stop": "Сохранить и остановить",
            "delete_continue": "Удалить и искать дальше",
        }
        edit_telegram_message(
            expected_chat_id,
            message_id,
            f"{message}\n\nТаймаут ожидания. Применено действие по умолчанию: {timeout_labels[default_action]}",
        )
    return default_action


def resolve_match_action(message: str) -> str:
    decision = wait_for_telegram_match_confirmation(message)
    if decision:
        print(f"Telegram decision: {decision}")
        return decision
    return "keep_stop"


def write_pending_match(args: argparse.Namespace, payload: dict) -> None:
    pending_path = SCRIPT_DIR / "pending_match.json"
    pending_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(
        args,
        {"pending_match": True, "path": str(pending_path), **payload},
        compact_line=(
            f"pending match saved path={pending_path} "
            f"ip={payload.get('ip')} id={payload.get('id')} reason={payload.get('reason')}"
        ),
    )


def cleanup_created_ip(token: str, floatingip_id: str | None, address: str | None = None) -> None:
    if not floatingip_id:
        return
    try:
        delete_floating_ip(token, floatingip_id)
    except ApiError as error:
        if error.status_code == 404:
            return
        target = f"{address} " if address else ""
        print(f"WARN: cleanup delete failed for {target}id={floatingip_id}: HTTP {error.status_code}", file=sys.stderr)
    except Exception as error:
        target = f"{address} " if address else ""
        print(f"WARN: cleanup delete failed for {target}id={floatingip_id}: {error}", file=sys.stderr)


def api_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = {
        "X-Token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    max_retries = env_int("SELECTEL_API_RETRIES", 8)
    backoff_base = env_float("SELECTEL_BACKOFF_BASE_SECONDS", 5.0)
    backoff_cap = env_float("SELECTEL_BACKOFF_CAP_SECONDS", 90.0)
    request_timeout = env_float("SELECTEL_HTTP_TIMEOUT_SECONDS", 30.0)

    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                body = response.read().decode("utf-8")
                if not body.strip():
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            if error.code == 429 and attempt < max_retries:
                retry_after = error.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_seconds = float(retry_after)
                    except ValueError:
                        wait_seconds = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                else:
                    wait_seconds = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                sleep_with_jitter(wait_seconds, wait_seconds + 3.0)
                continue
            if error.code in TRANSIENT_HTTP_STATUS_CODES and attempt < max_retries:
                wait_seconds = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                sleep_with_jitter(wait_seconds, wait_seconds + 3.0)
                continue
            raise ApiError(f"{method} {url} failed", status_code=error.code, details=details) from error
        except urllib.error.URLError as error:
            if attempt < max_retries:
                wait_seconds = min(backoff_cap, backoff_base * max(1, attempt))
                sleep_with_jitter(wait_seconds, wait_seconds + 2.0)
                continue
            raise ApiError(f"network error: {error}") from error
        except (TimeoutError, socket.timeout) as error:
            if attempt < max_retries:
                wait_seconds = min(backoff_cap, backoff_base * max(1, attempt))
                sleep_with_jitter(wait_seconds, wait_seconds + 2.0)
                continue
            raise ApiError(f"request timeout after {request_timeout:.1f}s: {method} {url}") from error
        except (
            ConnectionError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            EOFError,
            http.client.HTTPException,
        ) as error:
            if attempt < max_retries:
                wait_seconds = min(backoff_cap, backoff_base * max(1, attempt))
                sleep_with_jitter(wait_seconds, wait_seconds + 2.0)
                continue
            raise ApiError(f"request error: {error}") from error

    raise SystemExit(f"Request failed after retries: {method} {url}")


def candidate_ip_dirs() -> list[Path]:
    return [
        SCRIPT_DIR / "ip",
        Path(r"F:\yandex-cloud-ip\ip"),
        Path(r"F:\reg-cloudvps-ip\ip"),
    ]


def default_ip_list_dir() -> Path:
    explicit = env("SELECTEL_IP_LIST_DIR", required=False)
    if explicit:
        return Path(str(explicit))
    for path in candidate_ip_dirs():
        if path.exists():
            return path
    return candidate_ip_dirs()[0]


def load_local_matchers(directory_path: Path) -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    ip_set: set[str] = set()
    networks: list[ipaddress._BaseNetwork] = []
    if not directory_path.exists() or not directory_path.is_dir():
        return ip_set, networks
    for file_path in sorted(directory_path.glob("*.txt")):
        for raw_line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if "/" in line:
                    networks.append(ipaddress.ip_network(line, strict=False))
                else:
                    ip_set.add(str(ipaddress.ip_address(line)))
            except ValueError:
                continue
    return ip_set, networks


def address_matches_local_lists(
    address_value: str,
    ip_set: set[str],
    networks: list[ipaddress._BaseNetwork],
) -> bool:
    if not address_value:
        return False
    try:
        target = ipaddress.ip_address(address_value)
    except ValueError:
        return False
    if str(target) in ip_set:
        return True
    return any(target in network for network in networks)


def list_projects(token: str) -> list[dict]:
    return api_request("GET", "/projects", token).get("projects", [])


def list_floating_ips(token: str, *, detailed: bool = True) -> list[dict]:
    suffix = "?detailed=true" if detailed else ""
    return api_request("GET", f"/floatingips{suffix}", token).get("floatingips", [])


def create_floating_ips(token: str, project_id: str, region: str, quantity: int = 1) -> list[dict]:
    payload = {"floatingips": [{"quantity": quantity, "region": region}]}
    result = api_request("POST", f"/floatingips/projects/{project_id}", token, payload=payload)
    items = result.get("floatingips", [])
    if not items:
        raise SystemExit("Create request returned no floating IPs")
    return items


def delete_floating_ip(token: str, floatingip_id: str) -> None:
    try:
        api_request("DELETE", f"/floatingips/{floatingip_id}", token)
    except SystemExit:
        pass
    except ApiError as error:
        if error.status_code == 404:
            pass
        else:
            raise


def print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def output_mode(args: argparse.Namespace) -> str:
    if getattr(args, "json_output", False):
        return "json"
    return str(env("SELECTEL_OUTPUT_MODE", required=False, default="compact") or "compact").strip().lower()


def emit(args: argparse.Namespace, payload: dict, compact_line: str | None = None) -> None:
    if output_mode(args) == "json" or not compact_line:
        print_json(payload)
        log_path = getattr(args, "log_path", None)
        append_log_line(log_path, json.dumps(payload, ensure_ascii=False))
        return
    print(compact_line)
    append_log_line(getattr(args, "log_path", None), compact_line)


def filter_ips(ips: list[dict], args: argparse.Namespace) -> list[dict]:
    items = ips
    if getattr(args, "project_id", None):
        items = [item for item in items if item.get("project_id") == args.project_id]
    if getattr(args, "ip", None):
        items = [item for item in items if item.get("floating_ip_address") == args.ip]
    if getattr(args, "prefix", None):
        items = [item for item in items if str(item.get("floating_ip_address", "")).startswith(args.prefix)]
    if getattr(args, "status", None):
        items = [item for item in items if item.get("status") == args.status]
    if getattr(args, "local_list", False):
        directory_path = Path(args.ip_list_dir)
        ip_set, networks = load_local_matchers(directory_path)
        items = [
            item
            for item in items
            if address_matches_local_lists(str(item.get("floating_ip_address", "")), ip_set, networks)
        ]
    return items


def find_existing_matching_ip(
    token: str,
    project_id: str,
    ip_set: set[str],
    networks: list[ipaddress._BaseNetwork],
) -> dict | None:
    for item in list_floating_ips(token):
        if item.get("project_id") != project_id:
            continue
        if address_matches_local_lists(str(item.get("floating_ip_address", "")), ip_set, networks):
            return item
    return None


def is_quota_exceeded_error(error: ApiError) -> bool:
    if error.status_code != 409:
        return False
    try:
        payload = json.loads(error.details)
    except json.JSONDecodeError:
        return "quota_exceeded" in error.details
    return payload.get("error") == "quota_exceeded"


def is_project_locked_error(error: ApiError) -> bool:
    if error.status_code != 400:
        return False
    try:
        payload = json.loads(error.details)
    except json.JSONDecodeError:
        return "project_is_locked" in error.details
    return payload.get("error") == "project_is_locked"


def is_empty_request_error(error: ApiError) -> bool:
    return error.status_code is None


def is_rate_limit_error(error: ApiError) -> bool:
    if error.status_code != 429:
        return False
    try:
        payload = json.loads(error.details)
    except json.JSONDecodeError:
        return "too_many_requests" in error.details or "rate" in error.details.lower()
    return payload.get("error") in {"too_many_requests", "rate_limit_exceeded"}


def is_transient_http_error(error: ApiError) -> bool:
    return error.status_code in TRANSIENT_HTTP_STATUS_CODES


def is_resource_not_found_error(error: ApiError) -> bool:
    if error.status_code != 404:
        return False
    try:
        payload = json.loads(error.details)
    except json.JSONDecodeError:
        return "resource_not_found" in error.details or "resource_quota_not_found" in error.details
    return payload.get("error") in {"resource_not_found", "resource_quota_not_found"}


def cleanup_nonmatching_project_ips(
    token: str,
    project_id: str,
    ip_set: set[str],
    networks: list[ipaddress._BaseNetwork],
    ips: list[dict] | None = None,
) -> list[dict]:
    deleted: list[dict] = []
    for item in (ips if ips is not None else list_floating_ips(token)):
        if item.get("project_id") != project_id:
            continue
        address = str(item.get("floating_ip_address", ""))
        if address_matches_local_lists(address, ip_set, networks):
            continue
        floatingip_id = str(item.get("id") or "")
        if not floatingip_id:
            continue
        delete_floating_ip(token, floatingip_id)
        deleted.append(
            {
                "id": floatingip_id,
                "ip": address,
                "status": item.get("status"),
                "region": item.get("region"),
            }
        )
    return deleted


def project_floating_ips(ips: list[dict], project_id: str) -> list[dict]:
    return [item for item in ips if item.get("project_id") == project_id]


def planned_batch_size(token: str, project_id: str) -> tuple[int, list[dict]]:
    batch_limit = env_int("SELECTEL_CREATE_BATCH_SIZE", 12)
    quota_limit = env_int("SELECTEL_FLOATING_IP_QUOTA", 12)
    if batch_limit <= 0:
        batch_limit = 1
    cached_ips: list[dict] = []
    for attempt in range(1, 4):
        try:
            cached_ips = list_floating_ips(token)
            break
        except ApiError as error:
            if error.status_code in {500, 502, 503, 504} and attempt < 3:
                wait = min(30.0, 2.0 * (2 ** (attempt - 1)))
                print(f"planned_batch_size: HTTP {error.status_code}, retry {attempt}/3 in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
    current_used = len(project_floating_ips(cached_ips, project_id))
    available = max(0, quota_limit - current_used)
    size = max(1, min(batch_limit, available if available > 0 else batch_limit))
    return size, cached_ips


def batch_size_backoff(batch_size: int) -> int:
    if batch_size <= 1:
        return 1
    return max(1, batch_size // 2)


def cmd_auth_check(token: str, _: argparse.Namespace) -> int:
    projects = list_projects(token)
    print_json({"ok": True, "projects": projects})
    return 0


def cmd_list(token: str, args: argparse.Namespace) -> int:
    items = list_floating_ips(token)
    items = filter_ips(items, args)
    print_json({"floatingips": items})
    return 0


def cmd_find(token: str, args: argparse.Namespace) -> int:
    items = list_floating_ips(token)
    items = filter_ips(items, args)
    emit(
        args,
        {"matches": items, "count": len(items), "ip_list_dir": args.ip_list_dir if args.local_list else None},
        compact_line=f"matches={len(items)} ip_list_dir={args.ip_list_dir}" if args.local_list else f"matches={len(items)}",
    )
    return 0 if items else 1


def cmd_create(token: str, args: argparse.Namespace) -> int:
    args.log_path = init_log_path()
    project_id = args.project_id or str(env("SELECTEL_PROJECT_ID"))
    region = args.region or str(env("SELECTEL_REGION"))
    list_dir = Path(args.ip_list_dir)
    ip_set, networks = load_local_matchers(list_dir)
    emit(
        args,
        {
            "started": True,
            "project_id": project_id,
            "region": region,
            "max_attempts": args.max_attempts,
            "ip_list_dir": str(list_dir),
            "entries_loaded": len(ip_set) + len(networks),
        },
        compact_line=(
            f"start project={project_id} region={region} "
            f"max_attempts={attempts_label(args.max_attempts)} "
            f"ip_list_dir={list_dir} entries={len(ip_set) + len(networks)}"
        ),
    )
    if not ip_set and not networks:
        raise SystemExit(f"IP list directory is empty or missing: {list_dir}")

    if args.dry_run:
        batch_size, _ = planned_batch_size(token, project_id)
        emit(
            args,
            {
                "dry_run": True,
                "project_id": project_id,
                "region": region,
                "max_attempts": args.max_attempts,
                "batch_size": batch_size,
                "ip_list_dir": str(list_dir),
                "request": {"floatingips": [{"quantity": batch_size, "region": region}]},
            },
            compact_line=(
                f"dry-run project={project_id} region={region} "
                f"max_attempts={attempts_label(args.max_attempts)} batch_size={batch_size} ip_list_dir={list_dir}"
            ),
        )
        return 0

    existing_match = find_existing_matching_ip(token, project_id, ip_set, networks)
    if existing_match:
        existing_address = str(existing_match.get("floating_ip_address", ""))
        existing_id = str(existing_match.get("id", ""))
        emit(
            args,
            {"matched_existing": True, "ip_list_dir": str(list_dir), "ip": existing_match},
            compact_line=f"existing match ip={existing_address} id={existing_id}",
        )
        existing_message = (
            "\u041d\u0430\u0439\u0434\u0435\u043d \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0439 "
            "\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 floating IP.\n"
            f"IP: {existing_address}\n"
            f"ID: {existing_id}\n"
            f"Region: {existing_match.get('region') or '-'}\n"
            f"Project: {project_id}\n"
            f"Source list: {list_dir}"
        )
        if telegram_confirmation_enabled():
            decision = resolve_match_action(existing_message)
            if decision == "keep_continue":
                notify_success(existing_message + "\nDecision: keep and continue.")
                print("Continuing search after Telegram approval.")
            elif decision == "delete_continue":
                cleanup_created_ip(token, existing_id, existing_address)
                notify_success(existing_message + "\nDecision: delete and continue.")
            else:
                notify_success(existing_message + "\nDecision: keep and stop.")
                return 0
        elif not confirm_continue_on_existing_match(existing_address, existing_id):
            notify_success(existing_message)
            return 0
        else:
            print("Continuing search despite existing matching IP.")

    attempt = 0
    cached_ips: list[dict] = []
    while args.max_attempts <= 0 or attempt < args.max_attempts:
        attempt += 1
        created_items: list[dict] = []
        match_kept = False
        try:
            batch_size, cached_ips = planned_batch_size(token, project_id)
            while True:
                try:
                    created_items = create_floating_ips(token, project_id, region, quantity=batch_size)
                    break
                except ApiError as error:
                    if is_rate_limit_error(error):
                        backoff_sec = random.uniform(
                            env_float("SELECTEL_RATE_LIMIT_BACKOFF_MIN_SECONDS", 300.0),
                            env_float("SELECTEL_RATE_LIMIT_BACKOFF_MAX_SECONDS", 600.0),
                        )
                        emit(
                            args,
                            {
                                "rate_limited": True,
                                "status_code": error.status_code,
                                "attempt": attempt,
                                "sleep_seconds": round(backoff_sec, 1),
                                "details": error.details.strip() or "<empty>",
                            },
                            compact_line=(
                                f"attempt {attempt} -> rate limited "
                                f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                            ),
                        )
                        time.sleep(backoff_sec)
                        continue
                    if is_transient_http_error(error):
                        backoff_sec = random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        )
                        emit(
                            args,
                            {
                                "transient_error": True,
                                "status_code": error.status_code,
                                "attempt": attempt,
                                "sleep_seconds": round(backoff_sec, 1),
                            },
                            compact_line=f"attempt {attempt} -> HTTP {error.status_code} ({error.details.strip() or 'transient error'}), retry after {backoff_sec:.0f}s",
                        )
                        time.sleep(backoff_sec)
                        continue
                    if is_empty_request_error(error):
                        backoff_sec = random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        )
                        emit(
                            args,
                            {
                                "request_error": True,
                                "attempt": attempt,
                                "sleep_seconds": round(backoff_sec, 1),
                                "details": error.details.strip() or "<empty>",
                            },
                            compact_line=(
                                f"attempt {attempt} -> request error "
                                f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                            ),
                        )
                        time.sleep(backoff_sec)
                        continue
                    if is_resource_not_found_error(error):
                        backoff_sec = random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        )
                        emit(
                            args,
                            {
                                "resource_not_found": True,
                                "status_code": error.status_code,
                                "attempt": attempt,
                                "sleep_seconds": round(backoff_sec, 1),
                                "details": error.details.strip() or "<empty>",
                            },
                            compact_line=(
                                f"attempt {attempt} -> resource not found "
                                f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                            ),
                        )
                        time.sleep(backoff_sec)
                        continue
                    if is_project_locked_error(error):
                        backoff_sec = random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        )
                        emit(
                            args,
                            {
                                "project_locked": True,
                                "status_code": error.status_code,
                                "attempt": attempt,
                                "sleep_seconds": round(backoff_sec, 1),
                            },
                            compact_line=(
                                f"attempt {attempt} -> project locked "
                                f"({error.details.strip() or 'project_is_locked'}), retry after {backoff_sec:.0f}s"
                            ),
                        )
                        time.sleep(backoff_sec)
                        continue
                    if not is_quota_exceeded_error(error):
                        raise
                    # Refresh IP list before cleanup — it may be stale
                    _, cached_ips = planned_batch_size(token, project_id)
                    deleted_items = cleanup_nonmatching_project_ips(token, project_id, ip_set, networks, cached_ips)
                    if deleted_items:
                        emit(
                            args,
                            {
                                "quota_recovered": True,
                                "attempt": attempt,
                                "deleted_count": len(deleted_items),
                                "deleted": deleted_items,
                            },
                            compact_line=(
                                f"attempt {attempt} -> quota recovered by deleting "
                                f"{len(deleted_items)} non-matching floating IP(s)"
                            ),
                        )
                    else:
                        emit(
                            args,
                            {"quota_hit": True, "attempt": attempt},
                            compact_line=f"attempt {attempt} -> quota still full, waiting...",
                        )
                        time.sleep(random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        ))
                        continue
                    next_batch_size = batch_size_backoff(batch_size)
                    if next_batch_size == batch_size:
                        emit(
                            args,
                            {"quota_hit_stuck": True, "attempt": attempt, "batch_size": batch_size},
                            compact_line=f"attempt {attempt} -> quota stuck at batch {batch_size}, waiting...",
                        )
                        time.sleep(random.uniform(
                            env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                            env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                        ))
                        continue
                    emit(
                        args,
                        {
                            "batch_reduced": True,
                            "attempt": attempt,
                            "from_batch_size": batch_size,
                            "to_batch_size": next_batch_size,
                        },
                        compact_line=(
                            f"attempt {attempt} -> quota hit, reducing batch "
                            f"{batch_size} -> {next_batch_size}"
                        ),
                    )
                    batch_size = next_batch_size
            matching_items = [
                item
                for item in created_items
                if address_matches_local_lists(str(item.get("floating_ip_address", "")), ip_set, networks)
            ]
            if matching_items:
                created = matching_items[0]
                created_id = str(created.get("id") or "")
                address = str(created.get("floating_ip_address", ""))
                if not created_id:
                    raise SystemExit(f"Create response missing id: {created}")
                match_kept = True
                for extra_item in created_items:
                    extra_id = str(extra_item.get("id") or "")
                    if not extra_id or extra_id == created_id:
                        continue
                    cleanup_created_ip(token, extra_id, str(extra_item.get("floating_ip_address", "")))
                emit(
                    args,
                    {
                        "matched": True,
                        "attempt": attempt,
                        "batch_size": len(created_items),
                        "ip_list_dir": str(list_dir),
                        "ip": created,
                    },
                    compact_line=(
                        f"[{attempt}/{attempts_label(args.max_attempts)}] "
                        f"batch={len(created_items)} match ip={address} id={created.get('id')} kept"
                    ),
                )
                matched_message = (
                    "\u041d\u0430\u0439\u0434\u0435\u043d \u043d\u043e\u0432\u044b\u0439 "
                    "\u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0439 floating IP.\n"
                    f"IP: {address}\n"
                    f"ID: {created.get('id')}\n"
                    f"Region: {region}\n"
                    f"Project: {project_id}\n"
                    f"Attempt: {attempt}/{attempts_label(args.max_attempts)}\n"
                    f"Batch size: {len(created_items)}\n"
                    f"Source list: {list_dir}"
                )
                decision = resolve_match_action(matched_message)
                if decision == "keep_continue":
                    notify_success(matched_message + "\nDecision: keep and continue.")
                    continue
                if decision == "delete_continue":
                    match_kept = False
                    cleanup_created_ip(token, created_id, address)
                    created_items = []
                    notify_success(matched_message + "\nDecision: delete and continue.")
                    continue
                if decision == "telegram_unavailable":
                    write_pending_match(
                        args,
                        {
                            "reason": "telegram_unavailable",
                            "ip": address,
                            "id": created_id,
                            "region": region,
                            "project_id": project_id,
                            "attempt": attempt,
                            "batch_size": len(created_items),
                            "source_list": str(list_dir),
                            "created": created,
                            "created_at": utc_now(),
                        },
                    )
                    print(matched_message + "\nDecision: keep and stop because Telegram is unavailable.")
                    return 0
                notify_success(matched_message + "\nDecision: keep and stop.")
                return 0

            for item in created_items:
                cleanup_created_ip(token, str(item.get("id") or ""), str(item.get("floating_ip_address", "")))
            deleted_items = [
                {
                    "id": str(item.get("id") or ""),
                    "ip": str(item.get("floating_ip_address", "")),
                }
                for item in created_items
                if item.get("id")
            ]
            created_items = []
            post_create_sleep = sleep_with_jitter(
                env_float("SELECTEL_POST_CREATE_MIN_DELAY_SECONDS", 8.0),
                env_float("SELECTEL_POST_CREATE_MAX_DELAY_SECONDS", 15.0),
            )
            next_sleep = 0.0
            if (args.max_attempts <= 0 or attempt < args.max_attempts) and args.delay_seconds > 0:
                next_sleep = random.uniform(
                    args.delay_seconds,
                    args.delay_seconds + env_float("SELECTEL_DELAY_JITTER_SECONDS", 3.0),
                )
            emit(
                args,
                {
                    "matched": False,
                    "attempt": attempt,
                    "batch_size": len(deleted_items),
                    "deleted": deleted_items,
                    "post_create_sleep_seconds": round(post_create_sleep, 1),
                    "next_sleep_seconds": round(next_sleep, 1),
                },
                compact_line=(
                    f"attempt {attempt} -> batch={len(deleted_items)} deleted -> sleeping {next_sleep:.1f}s"
                    if next_sleep > 0
                    else f"attempt {attempt} -> batch={len(deleted_items)} deleted"
                ),
            )
            if next_sleep > 0:
                time.sleep(next_sleep)
        except ApiError as error:
            if is_rate_limit_error(error):
                backoff_sec = random.uniform(
                    env_float("SELECTEL_RATE_LIMIT_BACKOFF_MIN_SECONDS", 300.0),
                    env_float("SELECTEL_RATE_LIMIT_BACKOFF_MAX_SECONDS", 600.0),
                )
                emit(
                    args,
                    {
                        "rate_limited": True,
                        "status_code": error.status_code,
                        "attempt": attempt,
                        "sleep_seconds": round(backoff_sec, 1),
                        "details": error.details.strip() or "<empty>",
                    },
                    compact_line=(
                        f"attempt {attempt} -> rate limited "
                        f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                    ),
                )
                time.sleep(backoff_sec)
                continue
            if is_transient_http_error(error):
                backoff_sec = random.uniform(
                    env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                    env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                )
                emit(
                    args,
                    {
                        "transient_error": True,
                        "status_code": error.status_code,
                        "attempt": attempt,
                        "sleep_seconds": round(backoff_sec, 1),
                        "details": error.details.strip() or "<empty>",
                    },
                    compact_line=(
                        f"attempt {attempt} -> HTTP {error.status_code} "
                        f"({error.details.strip() or 'transient error'}), retry after {backoff_sec:.0f}s"
                    ),
                )
                time.sleep(backoff_sec)
                continue
            if is_empty_request_error(error):
                backoff_sec = random.uniform(
                    env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                    env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                )
                emit(
                    args,
                    {
                        "request_error": True,
                        "attempt": attempt,
                        "sleep_seconds": round(backoff_sec, 1),
                        "details": error.details.strip() or "<empty>",
                    },
                    compact_line=(
                        f"attempt {attempt} -> request error "
                        f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                    ),
                )
                time.sleep(backoff_sec)
                continue
            if is_resource_not_found_error(error):
                backoff_sec = random.uniform(
                    env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                    env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                )
                emit(
                    args,
                    {
                        "resource_not_found": True,
                        "status_code": error.status_code,
                        "attempt": attempt,
                        "sleep_seconds": round(backoff_sec, 1),
                        "details": error.details.strip() or "<empty>",
                    },
                    compact_line=(
                        f"attempt {attempt} -> resource not found "
                        f"({error.details.strip() or '<empty>'}), retry after {backoff_sec:.0f}s"
                    ),
                )
                time.sleep(backoff_sec)
                continue
            if is_project_locked_error(error):
                backoff_sec = random.uniform(
                    env_float("SELECTEL_BACKOFF_BASE_SECONDS", 10.0),
                    env_float("SELECTEL_BACKOFF_CAP_SECONDS", 120.0),
                )
                emit(
                    args,
                    {
                        "project_locked": True,
                        "status_code": error.status_code,
                        "attempt": attempt,
                        "sleep_seconds": round(backoff_sec, 1),
                    },
                    compact_line=(
                        f"attempt {attempt} -> project locked "
                        f"({error.details.strip() or 'project_is_locked'}), retry after {backoff_sec:.0f}s"
                    ),
                )
                time.sleep(backoff_sec)
                continue
            raise
        except KeyboardInterrupt:
            for item in created_items:
                cleanup_created_ip(token, str(item.get("id") or ""), str(item.get("floating_ip_address", "")))
            raise
        except Exception:
            if not match_kept:
                for item in created_items:
                    cleanup_created_ip(token, str(item.get("id") or ""), str(item.get("floating_ip_address", "")))
            raise

    raise SystemExit(f"No matching IP found after {attempt} attempts.")

def cmd_delete(token: str, args: argparse.Namespace) -> int:
    floatingip_id = args.id
    target_ip = args.ip
    if not floatingip_id:
        if not target_ip:
            raise SystemExit("Specify --id or --ip")
        items = list_floating_ips(token)
        matches = [item for item in items if item.get("floating_ip_address") == target_ip]
        if not matches:
            raise SystemExit(f"Floating IP not found by address: {target_ip}")
        floatingip_id = str(matches[0]["id"])
    if args.dry_run:
        emit(
            args,
            {"dry_run": True, "id": floatingip_id, "ip": target_ip},
            compact_line=f"dry-run delete id={floatingip_id} ip={target_ip or '-'}",
        )
        return 0
    delete_floating_ip(token, str(floatingip_id))
    emit(
        args,
        {"deleted": True, "id": floatingip_id, "ip": target_ip},
        compact_line=f"deleted id={floatingip_id} ip={target_ip or '-'}",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-check", help="Validate X-Token and list projects")

    list_parser = subparsers.add_parser("list", help="List floating IPs")
    list_parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON")
    list_parser.add_argument("--project-id", help="Filter by project id")
    list_parser.add_argument("--status", help="Filter by status")
    list_parser.add_argument("--ip", help="Find exact IP")
    list_parser.add_argument("--prefix", help="Find by IP prefix")

    find_parser = subparsers.add_parser("find", help="Find floating IPs against local ip folder")
    find_parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON")
    find_parser.add_argument("--project-id", help="Filter by project id")
    find_parser.add_argument("--status", help="Filter by status")
    find_parser.add_argument("--ip", help="Find exact IP")
    find_parser.add_argument("--prefix", help="Find by IP prefix")
    find_parser.add_argument("--local-list", action="store_true", help="Return only IPs present in the local ip list folder")
    find_parser.add_argument("--ip-list-dir", default=str(default_ip_list_dir()), help="Folder with *.txt IP and CIDR lists")

    create_parser = subparsers.add_parser("create", help="Create floating IPs one by one until one matches the local ip list folder")
    create_parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON")
    create_parser.add_argument("--project-id", help="Target project id")
    create_parser.add_argument("--region", help="Region, for example ru-2")
    create_parser.add_argument("--max-attempts", type=int, default=env_int("SELECTEL_MAX_ATTEMPTS", 100), help="How many create/delete attempts to make")
    create_parser.add_argument("--delay-seconds", type=float, default=env_float("SELECTEL_DELAY_SECONDS", 2.0), help="Delay between failed attempts")
    create_parser.add_argument("--ip-list-dir", default=str(default_ip_list_dir()), help="Folder with *.txt IP and CIDR lists")
    create_parser.add_argument("--dry-run", action="store_true", help="Show the request without creating IPs")

    delete_parser = subparsers.add_parser("delete", help="Delete floating IP by id or ip")
    delete_parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON")
    delete_parser.add_argument("--id", help="Floating IP id")
    delete_parser.add_argument("--ip", help="Floating IP address")
    delete_parser.add_argument("--dry-run", action="store_true", help="Show the request without deleting the IP")

    return parser


def main() -> int:
    configure_stdio()
    load_env_file()
    token = str(env("SELECTEL_X_TOKEN"))
    args = build_parser().parse_args()
    args.log_path = None
    command_map = {
        "auth-check": cmd_auth_check,
        "list": cmd_list,
        "find": cmd_find,
        "create": cmd_create,
        "delete": cmd_delete,
    }
    try:
        return command_map[args.command](token, args)
    except KeyboardInterrupt:
        print("\nОперация отменена пользователем.")
        return 130
    except ApiError as error:
        details = error.details.strip() or "<empty>"
        status = f"HTTP {error.status_code}" if error.status_code is not None else "request error"
        raise SystemExit(f"{status}: {details}") from error


if __name__ == "__main__":
    sys.exit(main())
