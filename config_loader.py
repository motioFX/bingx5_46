import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path(__file__).resolve().parent / "bingx_credentials.json"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    target_path = path or CONFIG_PATH
    if not target_path.exists():
        # Fallback empty config structure to prevent hard crashes before credentials are configured
        return {
            "apis": {
                "bingx": {"api_key": "", "secret_key": ""},
                "bingx_demo": {"api_key": "", "secret_key": ""}
            },
            "webhooks": {}
        }
    try:
        with target_path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as exc:
        print(f"[config_loader] Error reading {target_path}: {exc}")
        return {"apis": {}, "webhooks": {}}


def _load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    return load_config(path)


def _platform_value(section: str, platform: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Any:
    platform_key = platform or sys.platform
    cfg = config if config is not None else load_config()
    section_data = cfg.get(section, {})
    if isinstance(section_data, dict):
        platform_names = {"win32", "linux", "darwin", "default"}
        if any(k in platform_names for k in section_data):
            return section_data.get(platform_key) or section_data.get("default")
    return section_data


def load_api_keys(platform: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    apis = _platform_value("apis", platform, config)
    return apis if isinstance(apis, dict) else {}


# Webhook チャンネル名正規化エイリアスマップ
WEBHOOK_ALIASES = {
    "real1": "real1_bitbank",
    "bitbank": "real1_bitbank",
    "real1_bitbank": "real1_bitbank",

    "real2": "real2_hype",
    "hype": "real2_hype",
    "hyperliquid": "real2_hype",
    "real2_hype": "real2_hype",

    "real3": "real3_bngx",
    "bngx": "real3_bngx",
    "bingx": "real3_bngx",
    "real3_bngx": "real3_bngx",

    "test4": "test4_backtest",
    "test": "test4_backtest",
    "backtest": "test4_backtest",
    "win32": "test4_backtest",
    "test4_backtest": "test4_backtest",
}


def get_all_webhooks(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    cfg = config if config is not None else load_config()
    wh = cfg.get("webhooks", {})
    return wh if isinstance(wh, dict) else {}


def get_webhook_url(name: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> str:
    cfg = config if config is not None else load_config()
    webhooks = cfg.get("webhooks", {})
    if not isinstance(webhooks, dict):
        return str(webhooks) if webhooks else ""

    if name:
        canonical_key = WEBHOOK_ALIASES.get(name.lower(), name)
        if canonical_key in webhooks and webhooks[canonical_key]:
            return str(webhooks[canonical_key])
        if name in webhooks and webhooks[name]:
            return str(webhooks[name])
        return ""

    # 未指定の場合: BingXチャンネル (#real3_bngx) を最優先
    return str(webhooks.get("real3_bngx") or webhooks.get("test4_backtest") or "")


