import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 設定ファイルの探索優先度: bitbank_credentials.json -> bingx_credentials.json
_PARENT_DIR = Path(__file__).resolve().parent
BITBANK_CONFIG_PATH = _PARENT_DIR / "bitbank_credentials.json"
BINGX_CONFIG_PATH = _PARENT_DIR / "bingx_credentials.json"
CONFIG_PATH = BITBANK_CONFIG_PATH if BITBANK_CONFIG_PATH.exists() else BINGX_CONFIG_PATH

# ==================== Bitbank API 公式エンドポイント ====================
BITBANK_PUBLIC_URL = "https://public.bitbank.cc"
BITBANK_REST_URL = "https://api.bitbank.cc"
BITBANK_WSS_URL = "wss://stream.bitbank.cc"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    target_path = path or (BITBANK_CONFIG_PATH if BITBANK_CONFIG_PATH.exists() else BINGX_CONFIG_PATH)
    if not target_path.exists():
        return {
            "apis": {
                "bitbank": ["", ""]
            },
            "webhooks": {}
        }
    try:
        with target_path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as exc:
        print(f"[config_loader] Error reading {target_path}: {exc}")
        return {"apis": {}, "webhooks": {}}


def _load_config(path: Optional[Path] = None) -> Dict[str, Any]:
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
    """
    pybotters互換のAPIキー辞書を返します。
    例: {"bitbank": ["API_KEY", "SECRET_KEY"]}
    """
    apis = _platform_value("apis", platform, config)
    if not isinstance(apis, dict):
        return {}
    
    # bitbankキーが直接またはプラットフォーム直下にあるかチェック
    if "bitbank" in apis:
        return apis
    
    # 形式変換: {"bitbank": {"api_key": "...", "secret_key": "..."}} の場合 -> ["key", "secret"]
    formatted = {}
    for k, v in apis.items():
        if isinstance(v, dict) and "api_key" in v and "secret_key" in v:
            formatted[k] = [v["api_key"], v["secret_key"]]
        else:
            formatted[k] = v
    return formatted


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

    "test4": "test4_test",
    "test": "test4_test",
    "test4_test": "test4_test",
    "test4_backtest": "test4_test",
    "backtest": "test4_test",
    "win32": "test4_test",
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

    # 未指定の場合:
    # Windows (win32) でのテスト実行時は test4_test を最優先
    # VPS (Linux等) での本番実行時は real1_bitbank を最優先
    if sys.platform == "win32":
        return str(webhooks.get("test4_test") or webhooks.get("test4_backtest") or webhooks.get("real1_bitbank") or "")
    else:
        return str(webhooks.get("real1_bitbank") or webhooks.get("test4_test") or webhooks.get("test4_backtest") or "")
