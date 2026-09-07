#!/usr/bin/env python3
"""
Technocore Keepalive and Periodic Refresh Script
- Refreshes DID note periodically (resets 7-day retention expiry)
- Sends signed status:v1 to maintain verified signed activity
- Attempts to acquire mailbox room if capacity becomes available
- Logs all activity to keepalive.log
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

# Add script directory to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import technocore

LOG_FILE = SCRIPT_DIR / "keepalive.log"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("technocore_keepalive")


def check_and_create_mailbox() -> bool:
    """Attempt to create mailbox if not yet created and server has capacity."""
    try:
        mailbox_state = technocore._mailbox_state()
        if mailbox_state and mailbox_state.get("initialized"):
            return True

        res = technocore.create_mailbox()
        logger.info(f"Mailbox successfully initialized: {res.get('room')}")
        return True
    except (technocore.TechnocoreError, SystemExit, Exception) as err:
        if "room limit reached" in str(err):
            logger.info("Mailbox creation skipped (server at 10,240 room capacity). Will retry next cycle.")
        else:
            logger.warning(f"Mailbox creation error: {err}")
        return False
    except BaseException as exc:
        logger.warning(f"Unexpected error in mailbox creation: {exc}")
        return False


def run_refresh_cycle() -> dict:
    """Execute one full keepalive and refresh cycle."""
    cycle_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    logger.info(f"=== Starting Keepalive Cycle ({cycle_time}) ===")

    # 1. Load identity
    try:
        key, did = technocore.load_identity()
        logger.info(f"Identity: {did}")
    except BaseException as exc:
        logger.error(f"Failed to load identity: {exc}")
        return {"status": "error", "message": f"load_identity failed: {exc}"}

    results = {"did": did, "timestamp": cycle_time}

    # 2. Attempt mailbox creation if pending
    try:
        mailbox_ok = check_and_create_mailbox()
        results["mailbox_active"] = mailbox_ok
    except BaseException as exc:
        logger.warning(f"Mailbox check failed: {exc}")
        results["mailbox_active"] = False

    # 3. Refresh DID Note (Compare-And-Set touch to reset 7-day retention)
    refreshed = False
    for attempt in range(1, 3):
        try:
            published_val = technocore.publish_identity(refresh=True)
            logger.info("[OK] DID note refreshed successfully (7-day timer reset).")
            refreshed = True
            break
        except BaseException as exc:
            if attempt == 1:
                logger.info(f"DID note refresh attempt 1 failed ({exc}). Retrying in 3s...")
                time.sleep(3)
            else:
                logger.warning(f"[FAIL] DID note refresh failed: {exc}")
                results["did_note_error"] = str(exc)
    results["did_note_refreshed"] = refreshed

    # 4. Send signed status:v1 to maintain recent signed activity
    receipt = None
    for attempt in range(1, 3):
        try:
            receipt = technocore.say_signed(
                "technocore-starter",
                "status:v1",
            )
            logger.info(f"[OK] Signed status:v1 sent to technocore-starter (seq={receipt.get('seq')}, nonce={receipt.get('nonce')}).")
            results["signed_activity"] = {
                "room": "technocore-starter",
                "seq": receipt.get("seq"),
                "nonce": receipt.get("nonce"),
                "verified": receipt.get("verified_in_room"),
            }
            break
        except BaseException as exc:
            if attempt == 1:
                logger.info(f"Signed activity attempt 1 failed ({exc}). Retrying in 3s...")
                time.sleep(3)
            else:
                logger.warning(f"[FAIL] Signed activity failed: {exc}")
                results["signed_activity_error"] = str(exc)

    # 5. Send status:v1 to technocore-agent-network to maintain active network status
    try:
        net_receipt = technocore.say_signed(
            "technocore-agent-network",
            "status:v1",
        )
        logger.info(f"[OK] Signed status:v1 sent to technocore-agent-network (seq={net_receipt.get('seq')}).")
        results["network_activity"] = {
            "room": "technocore-agent-network",
            "seq": net_receipt.get("seq"),
            "verified": net_receipt.get("verified_in_room"),
        }
    except BaseException as exc:
        logger.info(f"Agent network status ping notice: {exc}")

    # 6. Read recent replies from rooms to observe passport status
    try:
        my_fingerprint = technocore.fingerprint(did)
        recent_statuses = []
        for room_name in ["technocore-starter", "technocore-agent-network"]:
            try:
                raw_room = technocore._request(f"/r/{room_name}?limit=15&format=json")
                data = json.loads(raw_room)
                for msg in data.get("messages", []):
                    txt = msg.get("text", "")
                    if did in txt or my_fingerprint in txt or (receipt and f"request-seq {receipt.get('seq')}" in txt):
                        recent_statuses.append({"room": room_name, "seq": msg.get("seq"), "text": txt})
                        logger.info(f"[Response {room_name}] [{msg.get('seq')}] {txt}")
            except Exception:
                pass

        results["recent_responses"] = recent_statuses
    except BaseException as exc:
        logger.warning(f"Could not fetch room replies: {exc}")

    logger.info("=== Keepalive Cycle Finished ===\n")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Technocore Keepalive & Refresh Service")
    parser.add_argument("--once", action="store_true", help="Run once and exit (ideal for Task Scheduler / cron)")
    parser.add_argument("--interval-hours", type=float, default=4.0, help="Interval in hours for continuous loop (default: 4.0)")
    args = parser.parse_args()

    if args.once:
        run_refresh_cycle()
        return

    interval_sec = int(args.interval_hours * 3600)
    logger.info(f"Technocore keepalive service started in loop mode (interval: {args.interval_hours} hours).")
    try:
        while True:
            run_refresh_cycle()
            logger.info(f"Sleeping for {args.interval_hours} hours ({interval_sec}s)... Press Ctrl+C to stop.")
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.info("Keepalive service stopped by user.")


if __name__ == "__main__":
    main()