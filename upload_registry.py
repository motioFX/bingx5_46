"""
Discord アップロード履歴管理モジュール (upload_registry.py)

全銘柄データ（ZIP/CSV）等のファイルについて、ハッシュ(MD5)およびファイルサイズを
Data/uploaded_files_registry.json に記録・照合することで、
「既に1回アップロードが終わっている同一データ」の重複送信を完全に防ぎ、
「新しくデータの変更があったもの」だけをDiscordへアップロードします。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

REGISTRY_FILE = Path(__file__).resolve().parent / "Data" / "uploaded_files_registry.json"


def compute_file_hash(file_path: Path) -> str:
    """ファイルのMD5ハッシュを計算（大容量ファイルでもストリーミング処理で安全）"""
    hasher = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(131072):  # 128KB chunks
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""


def load_registry() -> Dict[str, Dict[str, Any]]:
    """レジストリファイルを安全に読み込む"""
    if not REGISTRY_FILE.exists():
        return {}
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    """レジストリファイルを安全に保存する"""
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[upload_registry] Warning: Failed to save registry: {e}")


def should_upload_file(file_path: Path) -> bool:
    """
    ファイルがDiscordへアップロードすべきか（新規または変更ありか）を判定する。
    - すでにレジストリに存在し、サイズおよびMD5ハッシュが完全に一致する場合は False（送信不要）。
    - 新規ファイル、またはデータ更新でサイズ/ハッシュが変化している場合は True（送信必要）。
    """
    if not file_path.exists():
        return False

    registry = load_registry()
    key = file_path.name

    if key not in registry:
        return True  # 初回アップロード

    entry = registry[key]
    recorded_size = entry.get("size")
    current_size = file_path.stat().st_size

    # サイズが異なる場合はデータ変更あり
    if recorded_size != current_size:
        return True

    # サイズが同一でも念のためハッシュを検証
    recorded_hash = entry.get("hash", "")
    current_hash = compute_file_hash(file_path)

    if recorded_hash and current_hash and recorded_hash == current_hash:
        return False  # 同一データがすでにアップロード済み

    return True


def record_file_uploaded(file_path: Path, rows: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
    """アップロード完了をレジストリに記録する"""
    if not file_path.exists():
        return

    registry = load_registry()
    current_size = file_path.stat().st_size
    current_hash = compute_file_hash(file_path)

    entry = {
        "size": current_size,
        "hash": current_hash,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if rows is not None:
        entry["rows"] = rows
    if metadata:
        entry.update(metadata)

    registry[file_path.name] = entry
    save_registry(registry)
    print(f"[upload_registry] Recorded upload: {file_path.name} (size: {current_size:,} bytes, hash: {current_hash[:8]}...)")


def register_existing_files(directory: Path, pattern: str = "*.zip") -> int:
    """
    指定ディレクトリ内の既存ファイルを「既送信済み」として一括登録する（初回移行用ヘルパー）
    """
    if not directory.exists():
        return 0
    registry = load_registry()
    count = 0
    for p in directory.glob(pattern):
        if p.is_file() and p.name not in registry:
            current_size = p.stat().st_size
            current_hash = compute_file_hash(p)
            registry[p.name] = {
                "size": current_size,
                "hash": current_hash,
                "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (pre-registered)",
            }
            count += 1
    if count > 0:
        save_registry(registry)
        print(f"[upload_registry] Pre-registered {count} existing files as already uploaded.")
    return count
