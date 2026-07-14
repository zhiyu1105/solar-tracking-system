#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check local Z3A token status without printing token values."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from pathlib import Path
import requests

requests.packages.urllib3.disable_warnings()

def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return int(json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("exp", 0))
    except Exception:
        return 0

def days_left(token: str, now: float) -> float | None:
    exp = jwt_exp(token)
    if not exp:
        return None
    return (exp - now) / 86400


def token_can_query_devices(base_url: str, token: str, use_bearer_prefix: bool) -> bool:
    if not token:
        return False
    auth = f"Bearer {token}" if use_bearer_prefix else token
    try:
        response = requests.get(
            base_url.rstrip("/") + "/bind/query",
            headers={"auth": auth},
            verify=False,
            timeout=10,
        )
        raw = response.json() if response.text else {}
        return response.status_code == 200 and raw.get("code") == 0
    except Exception:
        return False


def print_token_block(label: str, token: str, now: float) -> None:
    print(f"\n{label}:")
    if not token:
        print("  狀態：未設定")
        return
    exp = jwt_exp(token)
    if not exp:
        print("  狀態：不是可解析的 JWT")
        return
    dt = datetime.fromtimestamp(exp)
    days = (exp - now) / 86400
    print(f"  到期時間：{dt:%Y-%m-%d %H:%M:%S}")
    if days < 0:
        print(f"  狀態：✗ 已過期 {-days:.1f} 天前")
    elif days < 1:
        print(f"  狀態：⚠ 即將到期 — 剩 {days * 24:.1f} 小時")
    elif days < 7:
        print(f"  狀態：⚠ 接近到期 — 剩 {days:.1f} 天")
    else:
        print(f"  狀態：✓ 有效 — 剩 {days:.1f} 天")


def main() -> int:
    env = load_env(Path(__file__).parent / ".env.dev")
    now = time.time()
    access_token = env.get("Z3A_TOKEN", "")
    refresh_token = env.get("Z3A_REFRESH_TOKEN", "")
    base_url = env.get("Z3A_BASE_URL", "https://server.qiyunwulian.com:12341")
    use_bearer_prefix = env.get("Z3A_USE_BEARER_PREFIX", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    access_days = days_left(access_token, now)
    refresh_days = days_left(refresh_token, now)

    if access_days is not None and access_days > 0 and token_can_query_devices(
        base_url, access_token, use_bearer_prefix
    ):
        print(f"COLLECTION_AUTH_STATUS: OK access token usable — 剩 {access_days:.1f} 天")
    elif refresh_days is not None and refresh_days > 0 and token_can_query_devices(
        base_url, refresh_token, use_bearer_prefix
    ):
        print(f"COLLECTION_AUTH_STATUS: OK refresh token/token2 data fallback usable — 剩 {refresh_days:.1f} 天")
    else:
        print("COLLECTION_AUTH_STATUS: FAIL no usable access token or token2 data fallback")

    print()
    print("══════════════════════════════════════════════════════")
    print("  Z3A Token 狀態檢查")
    print("══════════════════════════════════════════════════════")
    print_token_block("Access Token (Z3A_TOKEN)", access_token, now)
    print_token_block("Refresh Token (Z3A_REFRESH_TOKEN)", refresh_token, now)

    print()
    print("══════════════════════════════════════════════════════")
    print("Token 更新主流程")
    print("如果 Access Token 即將到期，優先使用七云物聯 App cache 更新：")
    print("  1. 先確認七云物聯 Windows App 仍可登入，或重新登入 App。")
    print("  2. 在 Telegram 的 05-手動操作審核 topic 輸入 /update_token。")
    print("  3. 依 bot 回覆輸入 /confirm <code>。")
    print("  4. bot 會讀取 App cache、更新 .env.dev、重建 backend 並檢查狀態。")
    print("  5. 本流程不會輸出 token 值。Fiddler 僅保留為 App cache 失效時的備援。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
