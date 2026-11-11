"""
main.py

Employee Attendance & Activity Monitoring system (updated):
- Shows live camera stream window when started
- Python script to download reports for all registered employees
"""

import os
import io
import time
import json
import math
import atexit
import base64
import queue
import threading
import sqlite3
from datetime import timedelta
import datetime
import traceback
import signal
from typing import Any, List, Tuple, Dict, Optional

from flask import Flask, request, jsonify, send_file, send_from_directory, abort
import numpy as np
import cv2
from flask_cors import CORS

# Face embedding library (no dlib)
from facenet_pytorch import MTCNN, InceptionResnetV1
import torch

# MediaPipe for face mesh
import mediapipe as mp

# YOLOv8 (ultralytics)
from ultralytics import YOLO

# Scheduler
from apscheduler.schedulers.background import BackgroundScheduler

# Utilities
import pandas as pd
from PIL import Image

# -------------------------
# Config / thresholds
# -------------------------
DB_PATH = "attendance_activity.db"
REPORTS_DIR = "reports"
LOGS_DIR = "logs"
IMG_DIR = "employee_images"
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

def ensure_report_directory(employee_id: Optional[int], logs = False) -> str:
    """Ensure a per-employee report directory exists and return its path."""
    if employee_id is None:
        if logs:
            target_dir = LOGS_DIR
        else:
            target_dir = REPORTS_DIR
    else:
        if logs:
            target_dir = os.path.join(LOGS_DIR, f"Employee_{employee_id}")
        else:
            target_dir = os.path.join(REPORTS_DIR, f"Employee_{employee_id}")
    os.makedirs(target_dir, exist_ok=True)
    return target_dir


def default_eating_state() -> Dict[str, object]:
    return {
        "active": False,
        "start": 0.0,
        "last_detected": 0.0,
        "last_items": [],
        "last_event": None
    }

# Cosine-distance threshold and margin for face matching; lower values mean stricter matching.
EMBEDDING_DISTANCE_THRESHOLD = 0.30
EMBEDDING_SECOND_BEST_MARGIN = 0.08
FACE_DETECTION_CONFIDENCE = 0.95
EAR_THRESHOLD = 0.20
EYE_CLOSED_SECONDS = 1.5
HEAD_DOWN_PITCH_THRESHOLD = 25
HEAD_DOWN_SECONDS = 1.5
EATING_CLASSES = {name.lower() for name in ["cup", "coffee cup", "bottle", "fork", "spoon", "bowl", "sandwich", "apple", "banana", "wine glass", "pizza", "donut", "cake"]}
PROXIMITY_PIXEL_THRESHOLD = 0.25
FACE_LOST_SECONDS = 5.0
CAMERA_INDEX = 0

FACE_FOOD_EXPANSION = 2.6
FOOD_IOU_THRESHOLD = 0.005
FOOD_CENTER_THRESHOLD = 0.85
EATING_FRAMES_REQUIRED = 3
EATING_FRAMES_COOLDOWN = 8
EATING_HOLD_SECONDS = 0.35
EATING_GRACE_SECONDS = 1.0
MOUTH_OPEN_THRESHOLD = 0.035
MOUTH_MOVEMENT_DELTA = 0.008
MOUTH_FRAMES_REQUIRED = 4
MOUTH_FRAMES_COOLDOWN = 6
MOUTH_ACTIVITY_GRACE_SECONDS = 0.6

EXPECTED_ACTIVITY_LABELS = [
    "working",
    "eyes_closed",
    "head_down",
    "eating",
    "drinking"
]

# -------------------------
# Session Management
# -------------------------
SESSION_MIN_DURATION_SECONDS = 1.0

session_states: Dict[int, Dict[str, Any]] = {}
session_lock = threading.Lock()


def _session_apply_elapsed(info: Dict[str, Any], now: float) -> None:
    if not info.get("running"):
        info["last_update_ts"] = now
        return
    last_update = float(info.get("last_update_ts", now))
    elapsed = max(0.0, now - last_update)
    if elapsed <= 0.0:
        info["last_update_ts"] = now
        return
    remaining_before = float(info.get("remaining_seconds", 0.0))
    remaining_after = max(0.0, remaining_before - elapsed)
    if remaining_before > 0.0 and remaining_after <= 0.0:
        overshoot = max(0.0, elapsed - remaining_before)
        depletion_time = now - overshoot
        info["depleted_at"] = depletion_time
    info["remaining_seconds"] = remaining_after
    info["last_update_ts"] = now


def _snapshot_session(info: Dict[str, Any]) -> Dict[str, Any]:
    if not info:
        return {}
    remaining = max(0.0, float(info.get("remaining_seconds", 0.0)))
    hard_end = info.get("hard_end_ts")
    created_at = info.get("created_at")
    first_seen = info.get("first_seen_at")
    return {
        "employee_id": int(info.get("employee_id")),
        "duration_seconds": float(info.get("duration_seconds", 0.0)),
        "remaining_seconds": remaining,
        "remaining_hms": format_seconds_to_hms(remaining),
        "hard_end_iso": (datetime.datetime.utcfromtimestamp(hard_end) + (timedelta(hours=5).total_seconds())).isoformat() if hard_end else None,
        "created_at_iso": (datetime.datetime.utcfromtimestamp(created_at) + (timedelta(hours=5).total_seconds())).isoformat() if created_at else None,
        "first_seen_iso": (datetime.datetime.utcfromtimestamp(first_seen) + (timedelta(hours=5).total_seconds())).isoformat() if first_seen else None,
        "running": bool(info.get("running")),
    }


def configure_employee_session(employee_id: int, duration_seconds: float) -> Dict[str, Any]:
    if duration_seconds is None:
        raise ValueError("duration_seconds is required")
    duration_seconds = max(SESSION_MIN_DURATION_SECONDS, float(duration_seconds))
    now = time.time()
    hard_end = now + duration_seconds
    with session_lock:
        info = session_states.get(employee_id, {})
        info.update({
            "employee_id": employee_id,
            "created_at": now,
            "hard_end_ts": hard_end,
            "duration_seconds": duration_seconds,
            "remaining_seconds": duration_seconds,
            "running": False,
            "last_update_ts": now,
            "first_seen_at": None,
            "depleted_at": None,
        })
        session_states[employee_id] = info
        snapshot = _snapshot_session(info)
    return snapshot


def resume_employee_session(employee_id: int, *, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with session_lock:
        info = session_states.get(employee_id)
        if not info:
            return
        _session_apply_elapsed(info, now)
        info["running"] = True
        info["last_update_ts"] = now
        if info.get("first_seen_at") is None:
            info["first_seen_at"] = now
        info["hard_end_ts"] = now + float(info.get("remaining_seconds", 0.0))


def pause_employee_session(employee_id: int, *, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with session_lock:
        info = session_states.get(employee_id)
        if not info:
            return
        _session_apply_elapsed(info, now)
        info["running"] = False
        info["last_update_ts"] = now
        info["hard_end_ts"] = now + float(info.get("remaining_seconds", 0.0))


def cancel_employee_session(employee_id: int) -> Optional[Dict[str, Any]]:
    now = time.time()
    with session_lock:
        info = session_states.pop(employee_id, None)
        if info:
            _session_apply_elapsed(info, now)
    if not info:
        return None
    info["running"] = False
    info["last_update_ts"] = now
    return _snapshot_session(info)


def consume_expired_sessions(now: Optional[float] = None) -> List[Tuple[int, float, str]]:
    now = time.time() if now is None else now
    expired: List[Tuple[int, float, str]] = []
    with session_lock:
        for emp_id, info in list(session_states.items()):
            _session_apply_elapsed(info, now)
            hard_end = float(info.get("hard_end_ts", now))
            reason = None
            expiry_ts = None
            duration_depleted = info.get("remaining_seconds", 0.0) <= 0.0 and info.get("first_seen_at") is not None
            if duration_depleted:
                reason = "duration_consumed"
                expiry_ts = info.get("depleted_at") or now
            if now >= hard_end:
                expiry_candidate = hard_end
                if reason is None or expiry_candidate <= (expiry_ts or expiry_candidate):
                    reason = "window_expired"
                    expiry_ts = expiry_candidate
            if reason:
                info["running"] = False
                info["remaining_seconds"] = 0.0
                info["completed_at"] = expiry_ts
                info["completed_reason"] = reason
                expired.append((emp_id, expiry_ts, reason))
                del session_states[emp_id]
            else:
                info["last_update_ts"] = now
    return expired


def get_session_status(employee_id: Optional[int] = None) -> List[Dict[str, Any]]:
    now = time.time()
    with session_lock:
        if employee_id is not None:
            info = session_states.get(employee_id)
            if not info:
                return []
            _session_apply_elapsed(info, now)
            info["last_update_ts"] = now
            return [_snapshot_session(info)]
        results: List[Dict[str, Any]] = []
        for info in session_states.values():
            _session_apply_elapsed(info, now)
            info["last_update_ts"] = now
            results.append(_snapshot_session(info))
    return results


def session_is_configured(employee_id: int) -> bool:
    with session_lock:
        return employee_id in session_states

# -------------------------
# Initialize subsystems
# -------------------------

# Track manual attendance markers set via start/stop endpoints.
manual_attendance_ids = set()
last_report_employee_id: Optional[int] = None
_shutdown_reports_generated = False
_original_sigint_handler = signal.getsignal(signal.SIGINT)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Using device:", device)

mtcnn = MTCNN(keep_all=True, device=device)
resnet = InceptionResnetV1(pretrained='vggface2').eval().to(device)

mp_face_mesh = mp.solutions.face_mesh
mp_drawing = mp.solutions.drawing_utils

yolo_model = YOLO("yolov8n.pt")

scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

# -------------------------
# Database helpers
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT,
        email TEXT,
        embedding BLOB,
        profile_image_path TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS activity_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER,
        activity_type TEXT,
        start_timestamp TEXT,
        end_timestamp TEXT,
        duration_seconds REAL,
        extra_info TEXT
    )
    """)
    #  "log_id": row[0],
    #         "activity_type": row[2],
    #         "start_timestamp": row[3],
    #         "end_timestamp": row[4],
    #         "duration_seconds": row[5],
    #         "extra_info": row[6],
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER,
        check_in TEXT,
        check_out TEXT
    )
    """)
    conn.commit()
    conn.close()

def db_insert_employee(full_name, email, embedding_vec, profile_image_path=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    emb_blob = sqlite3.Binary(embedding_vec.tobytes())
    created_at = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
    cur.execute("INSERT INTO employees (full_name, email, embedding, profile_image_path, created_at) VALUES (?, ?, ?, ?, ?)",
                (full_name, email, emb_blob, profile_image_path, created_at))
    emp_id = cur.lastrowid
    conn.commit()
    conn.close()
    return emp_id

def db_get_all_embeddings():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT employee_id, full_name, embedding FROM employees")
    rows = cur.fetchall()
    conn.close()
    results = []
    for emp_id, name, emb_blob in rows:
        if emb_blob is None:
            continue
        arr = np.frombuffer(emb_blob, dtype=np.float32).copy()
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr /= norm
        results.append((emp_id, name, arr))
    return results

def db_log_activity(employee_id, activity_type, start_ts, end_ts, duration_seconds, extra_info=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO activity_logs (employee_id, activity_type, start_timestamp, end_timestamp, duration_seconds, extra_info) VALUES (?, ?, ?, ?, ?, ?)",
                (employee_id, activity_type, start_ts, end_ts, duration_seconds, json.dumps(extra_info) if extra_info else None))
    conn.commit()
    conn.close()

def db_mark_attendance_checkin(employee_id, check_in_ts):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM attendance WHERE employee_id = ? AND check_out IS NULL ORDER BY id DESC LIMIT 1", (employee_id,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return existing[0]
    cur.execute("INSERT INTO attendance (employee_id, check_in, check_out) VALUES (?, ?, ?)", (employee_id, check_in_ts, None))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def db_mark_attendance_checkout(employee_id, check_out_ts):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM attendance WHERE employee_id = ? AND check_out IS NULL ORDER BY id DESC LIMIT 1", (employee_id,))
    row = cur.fetchone()
    if row:
        aid = row[0]
        cur.execute("UPDATE attendance SET check_out = ? WHERE id = ?", (check_out_ts, aid))
    conn.commit()
    conn.close()


def db_delete_employee(employee_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM employees WHERE employee_id = ?", (employee_id,))
    exists = cur.fetchone() is not None
    if not exists:
        conn.close()
        return False
    cur.execute("DELETE FROM employees WHERE employee_id = ?", (employee_id,))
    cur.execute("DELETE FROM activity_logs WHERE employee_id = ?", (employee_id,))
    cur.execute("DELETE FROM attendance WHERE employee_id = ?", (employee_id,))
    conn.commit()
    conn.close()
    return True


def db_get_all_employee_ids() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT employee_id FROM employees ORDER BY employee_id")
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids


def format_seconds_to_hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return "00:00:00"
    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_duration_compact(seconds: float) -> str:
    total_seconds = max(0, int(round(float(seconds))))
    return str(datetime.timedelta(seconds=total_seconds))


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_report_date(value: Optional[str], end_of_day: bool = False) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        date_obj = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid date format. Use YYYY-MM-DD.") from exc
    if end_of_day:
        return datetime.datetime.combine(date_obj, datetime.time(23, 59, 59))
    return datetime.datetime.combine(date_obj, datetime.time(0, 0, 0))


REPORT_COLUMNS = [
    "Employee_ID",
    "Employee_Name",
    "Time_In",
    "Time_Out",
    "Total_Duration",
    "Performance",
    "Eating_Count",
    "Drinking_Count",
    "Status",
    "CreatedAt"
]

LOG_COLUMNS = [
    "Employee_ID",
    "Activity_Type",
    "Start_Timestamp",
    "End_Timestamp",
    "Duration_Seconds",
    "Food_Event",
    "Food_Items",
    "Reason"
]

DRINKING_ITEMS = {name.lower() for name in ["cup", "coffee cup", "bottle", "wine glass", "water bottle"]}
EATING_ITEMS = {name.lower() for name in ["fork", "spoon", "bowl", "sandwich", "apple", "banana", "pizza", "donut", "cake", "bread", "burger", "hot dog"]}
EAT_DRINK_STATES = {"eating", "drinking"}


def _clamp_interval(
    start: Optional[datetime.datetime],
    end: Optional[datetime.datetime],
    window_start: Optional[datetime.datetime],
    window_end: Optional[datetime.datetime]
) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
    if start is None:
        return None
    effective_end = end or (datetime.datetime.utcnow() + timedelta(hours=5))
    if window_start and effective_end < window_start:
        return None
    if window_end and start > window_end:
        return None
    clamped_start = max(start, window_start) if window_start else start
    clamped_end = min(effective_end, window_end) if window_end else effective_end
    if clamped_end <= clamped_start:
        return None
    return clamped_start, clamped_end


def _seconds_between(start: datetime.datetime, end: datetime.datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _parse_activity_extra(extra_info: Optional[str]) -> Dict[str, Any]:
    result = {"event": None, "items": [], "reason": None}
    if not extra_info:
        return result
    try:
        payload = json.loads(extra_info)
    except json.JSONDecodeError:
        return result
    if isinstance(payload, dict):
        event = payload.get("food_event")
        if isinstance(event, str):
            result["event"] = event.lower()
        items = payload.get("food_items")
        if isinstance(items, list):
            normalised = [str(item).lower() for item in items]
            result["items"] = normalised
        reason = payload.get("reason")
        if isinstance(reason, str):
            result["reason"] = reason
    return result


def _normalise_live_extra(extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(extra, dict):
        return {"event": None, "items": [], "reason": None}
    event = extra.get("food_event")
    if isinstance(event, str):
        event_value = event.lower()
    else:
        event_value = None
    items_field = extra.get("food_items")
    if isinstance(items_field, list):
        items_value = [str(item).lower() for item in items_field]
    else:
        items_value = []
    reason_field = extra.get("reason")
    reason_value = reason_field if isinstance(reason_field, str) else None
    return {"event": event_value, "items": items_value, "reason": reason_value}


def _attach_reason(extra: Optional[Dict[str, Any]], reason: Optional[str]) -> Optional[Dict[str, Any]]:
    payload = {}
    if isinstance(extra, dict):
        payload.update(extra)
    if reason:
        payload["reason"] = reason
    return payload or None


def _performance_from_percentage(pct: float) -> str:
    if pct <= 0:
        return "NIL"
    if pct < 20:
        return "POOR"
    if pct < 40:
        return "SATISFACTORY"
    if pct < 60:
        return "GOOD"
    if pct < 80:
        return "VERY GOOD"
    if pct < 100:
        return "VERY GOOD"
    return "EXCELLENT"


def gather_report_rows(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    global monitor_thread

    start_dt = parse_report_date(start_date, end_of_day=False)
    end_dt = parse_report_date(end_date, end_of_day=True)
    report_created_at = (datetime.datetime.utcnow() + timedelta(hours=5)).date().isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT employee_id, full_name FROM employees")
    name_map = {emp_id: full_name for emp_id, full_name in cur.fetchall()}

    if employee_id is not None:
        target_ids: List[int] = [employee_id]
    else:
        target_ids = sorted(name_map.keys())

    live_state_snapshot: Dict[int, Dict[str, Any]] = {}
    now_utc = datetime.datetime.utcnow() + timedelta(hours=5)
    if monitor_thread is not None and monitor_thread.is_alive():
        with monitor_thread.lock:
            live_state_snapshot = {
                live_emp_id: {
                    "state": info["state"],
                    "start": info["start"],
                    "extra": info.get("extra")
                }
                for live_emp_id, info in monitor_thread.current_states.items()
            }

    extra_ids = (set(manual_attendance_ids) | set(live_state_snapshot.keys())) - set(target_ids)
    if extra_ids:
        target_ids.extend(sorted(extra_ids))

    rows: List[Dict[str, Any]] = []

    for emp_id in target_ids:
        employee_name = name_map.get(emp_id, "Unknown")

        summary = {
            "time_in_dt": None,
            "time_out_dt": None,
            "present_seconds": 0.0,
            "session_seconds": 0.0,
            "eating_count": 0,
            "drinking_count": 0
        }

        cur.execute(
            "SELECT check_in, check_out FROM attendance WHERE employee_id = ?",
            (emp_id,)
        )
        for check_in_iso, check_out_iso in cur.fetchall():
            check_in_dt = parse_iso_datetime(check_in_iso)
            check_out_dt = parse_iso_datetime(check_out_iso)
            interval = _clamp_interval(check_in_dt, check_out_dt, start_dt, end_dt)
            if not interval:
                continue
            interval_start, interval_end = interval
            summary["session_seconds"] += _seconds_between(interval_start, interval_end)
            if summary["time_in_dt"] is None or interval_start < summary["time_in_dt"]:
                summary["time_in_dt"] = interval_start
            if summary["time_out_dt"] is None or interval_end > summary["time_out_dt"]:
                summary["time_out_dt"] = interval_end

        cur.execute(
            """
            SELECT activity_type, start_timestamp, end_timestamp, duration_seconds, extra_info
            FROM activity_logs
            WHERE employee_id = ?
            """,
            (emp_id,)
        )
        for activity_type, start_iso, end_iso, duration_seconds, extra_info in cur.fetchall():
            start_dt_log = parse_iso_datetime(start_iso)
            end_dt_log = parse_iso_datetime(end_iso)
            interval = _clamp_interval(start_dt_log, end_dt_log, start_dt, end_dt)
            if not interval:
                continue
            interval_start, interval_end = interval
            overlap_seconds = _seconds_between(interval_start, interval_end)
            if overlap_seconds <= 0:
                continue
            summary["present_seconds"] += overlap_seconds
            if summary["time_in_dt"] is None or interval_start < summary["time_in_dt"]:
                summary["time_in_dt"] = interval_start
            if summary["time_out_dt"] is None or interval_end > summary["time_out_dt"]:
                summary["time_out_dt"] = interval_end
            if activity_type in EAT_DRINK_STATES:
                extra_payload = _parse_activity_extra(extra_info)
                event = extra_payload["event"]
                items = set(extra_payload["items"])
                effective_event = event or activity_type
                if effective_event == "drinking" or any(item in DRINKING_ITEMS for item in items):
                    summary["drinking_count"] += 1
                elif effective_event == "eating" or any(item in EATING_ITEMS for item in items):
                    summary["eating_count"] += 1
                else:
                    # Default to drinking when cup-like objects dominate, otherwise treat as eating.
                    if any(item in {"cup", "coffee cup", "bottle", "water bottle", "wine glass"} for item in items):
                        summary["drinking_count"] += 1
                    elif items:
                        summary["eating_count"] += 1

        if emp_id in live_state_snapshot:
            state_info = live_state_snapshot[emp_id]
            live_start = datetime.datetime.utcfromtimestamp(state_info["start"]+(timedelta(hours=5).total_seconds()))
            live_interval = _clamp_interval(live_start, now_utc, start_dt, end_dt)
            if live_interval:
                live_start_clamped, live_end_clamped = live_interval
                live_seconds = _seconds_between(live_start_clamped, live_end_clamped)
                summary["present_seconds"] += live_seconds
                if summary["time_in_dt"] is None or live_start_clamped < summary["time_in_dt"]:
                    summary["time_in_dt"] = live_start_clamped
                if summary["time_out_dt"] is None or live_end_clamped > summary["time_out_dt"]:
                    summary["time_out_dt"] = live_end_clamped
                if summary["session_seconds"] <= 0:
                    summary["session_seconds"] = live_seconds
                if state_info.get("state") in EAT_DRINK_STATES:
                    live_extra = _normalise_live_extra(state_info.get("extra"))
                    live_event = live_extra["event"]
                    live_items = set(live_extra["items"])
                    effective_live = live_event or state_info.get("state")
                    if effective_live == "drinking" or any(item in DRINKING_ITEMS for item in live_items):
                        summary["drinking_count"] += 1
                    elif effective_live == "eating" or any(item in EATING_ITEMS for item in live_items):
                        summary["eating_count"] += 1
                    elif live_items:
                        summary["eating_count"] += 1

        time_in_dt = summary["time_in_dt"]
        time_out_dt = summary["time_out_dt"]

        if summary["session_seconds"] <= 0 and time_in_dt and time_out_dt:
            summary["session_seconds"] = _seconds_between(time_in_dt, time_out_dt)
        if summary["session_seconds"] <= 0 and summary["present_seconds"] > 0:
            summary["session_seconds"] = summary["present_seconds"]

        status = "Present" if (summary["present_seconds"] > 0 or summary["session_seconds"] > 0 or emp_id in live_state_snapshot) else "Absent"

        total_pct = 0.0
        if summary["session_seconds"] > 0:
            total_pct = min(100.0, (summary["present_seconds"] / summary["session_seconds"]) * 100.0)

        rows.append({
            "Employee_ID": emp_id,
            "Employee_Name": employee_name,
            "Time_In": time_in_dt.strftime("%H:%M:%S") if time_in_dt else "",
            "Time_Out": time_out_dt.strftime("%H:%M:%S") if time_out_dt else "",
            "Total_Duration": f"{total_pct:.2f}%",
            "Performance": _performance_from_percentage(total_pct),
            "Eating_Count": summary["eating_count"],
            "Drinking_Count": summary["drinking_count"],
            "Status": status,
            "CreatedAt": report_created_at
        })

    conn.close()
    return rows

def write_report_to_db(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    # The updated reporting flow does not persist report rows back into the
    # activity_logs table. Keeping this function as a stub preserves the
    # signature for any legacy callers.
    return False

def write_report_csv(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    prefix: str = "shutdown_report"
) -> Optional[str]:
    rows = gather_report_rows(employee_id=employee_id, start_date=start_date, end_date=end_date)
    if rows:
        df = pd.DataFrame(rows).reindex(columns=REPORT_COLUMNS)
    else:
        df = pd.DataFrame(columns=REPORT_COLUMNS)
    timestamp = int(time.time())
    suffix = employee_id if employee_id is not None else "all"
    target_dir = ensure_report_directory(employee_id)
    file_path = os.path.join(
        target_dir,
        f"{prefix}_{suffix}_{timestamp}.csv"
    )
    try:
        df.to_csv(file_path, index=False)
    except Exception as exc:
        print(f"Failed to write report for employee {suffix}: {exc}")
        return None
    return file_path


def gather_activity_logs(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    start_dt = parse_report_date(start_date, end_of_day=False)
    end_dt = parse_report_date(end_date, end_of_day=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    query = (
        """
        SELECT employee_id, activity_type, start_timestamp, end_timestamp, duration_seconds, extra_info
        FROM activity_logs
        """
    )
    params: List[Any] = []
    if employee_id is not None:
        query += " WHERE employee_id = ?"
        params.append(employee_id)
    query += " ORDER BY employee_id, start_timestamp"

    cur.execute(query, params)

    rows: List[Dict[str, Any]] = []
    for emp_id, activity_type, start_iso, end_iso, duration_seconds, extra_info in cur.fetchall():
        start_dt_log = parse_iso_datetime(start_iso)
        end_dt_log = parse_iso_datetime(end_iso)
        if start_dt or end_dt:
            if start_dt_log is None:
                continue
            interval = _clamp_interval(start_dt_log, end_dt_log, start_dt, end_dt)
            if interval is None:
                continue
        extra_payload = _parse_activity_extra(extra_info)
        duration_value = ""
        if duration_seconds is not None:
            try:
                duration_value = f"{float(duration_seconds):.2f}"
            except (TypeError, ValueError):
                duration_value = str(duration_seconds)
        rows.append({
            "Employee_ID": emp_id,
            "Activity_Type": activity_type,
            "Start_Timestamp": start_dt_log.isoformat() if start_dt_log else (start_iso or ""),
            "End_Timestamp": end_dt_log.isoformat() if end_dt_log else (end_iso or ""),
            "Duration_Seconds": duration_value,
            "Food_Event": extra_payload.get("event") or "",
            "Food_Items": ", ".join(extra_payload.get("items", [])) if extra_payload.get("items") else "",
            "Reason": extra_payload.get("reason") or ""
        })

    conn.close()
    return rows


def write_logs_csv(
    employee_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    prefix: str = "logs"
) -> Optional[str]:
    rows = gather_activity_logs(employee_id=employee_id, start_date=start_date, end_date=end_date)
    if rows:
        df = pd.DataFrame(rows).reindex(columns=LOG_COLUMNS)
    else:
        df = pd.DataFrame(columns=LOG_COLUMNS)
    timestamp = int(time.time())
    suffix = employee_id if employee_id is not None else "all"
    target_dir = ensure_report_directory(employee_id)
    file_path = os.path.join(
        target_dir,
        f"{prefix}_{suffix}_{timestamp}.csv"
    )
    try:
        df.to_csv(file_path, index=False)
    except Exception as exc:
        print(f"Failed to write logs for employee {suffix}: {exc}")
        return None
    return file_path


def export_reports_on_shutdown():
    global _shutdown_reports_generated, last_report_employee_id
    if _shutdown_reports_generated:
        return
    _shutdown_reports_generated = True
    target_employee_ids: List[int] = []
    if last_report_employee_id is not None:
        target_employee_ids = [last_report_employee_id]
    elif manual_attendance_ids:
        target_employee_ids = sorted(manual_attendance_ids)
    else:
        print("No employee selected for shutdown report.")
        return

    generated_paths = []
    for emp_id in target_employee_ids:
        path = write_report_csv(employee_id=emp_id)
        if path:
            generated_paths.append(path)

    if generated_paths:
        print("Shutdown reports saved:")
        for path in generated_paths:
            print(" -", path)
    else:
        print("No shutdown reports were generated.")


def _handle_sigint(signum, frame):
    export_reports_on_shutdown()
    handler = _original_sigint_handler
    if handler in (None, signal.SIG_IGN):
        return
    if handler is signal.default_int_handler:
        raise KeyboardInterrupt
    handler(signum, frame)


def extract_employee_id_from_request() -> Optional[int]:
    payload = request.get_json(silent=True) or {}
    raw_value = (
        payload.get("employee_id")
        if isinstance(payload, dict)
        else None
    )
    if raw_value is None:
        raw_value = request.form.get("employee_id")
    if raw_value is None:
        raw_value = request.args.get("employee_id")
    if raw_value in (None, ""):
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("employee_id must be an integer") from exc

# -------------------------
# Face embedding helpers
# -------------------------
def image_to_tensor(img_bgr: np.ndarray) -> Image.Image:
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(img_rgb)

def get_face_embeddings_from_image(cv2_img: np.ndarray) -> List[np.ndarray]:
    pil = image_to_tensor(cv2_img)
    with torch.no_grad():
        try:
            face_tensors = mtcnn(pil, return_prob=False)
        except RuntimeError as exc:
            print(f"MTCNN inference failed: {exc}")
            return []
        embeddings = []
        if face_tensors is None:
            return embeddings
        if isinstance(face_tensors, torch.Tensor):
            face_tensors = face_tensors.unsqueeze(0) if face_tensors.dim() == 3 else face_tensors
        if isinstance(face_tensors, list):
            face_tensors = torch.stack(face_tensors).to(device)
        else:
            face_tensors = face_tensors.to(device)
        embs = resnet(face_tensors).detach().cpu().numpy()
        for e in embs:
            norm = np.linalg.norm(e)
            if norm > 0:
                e = e / norm
            embeddings.append(e.astype(np.float32))
        return embeddings

def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 1.0
    return 1.0 - np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# -------------------------
# MediaPipe utilities
# -------------------------
LEFT_EYE_INDICES = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_INDICES = [362, 385, 387, 263, 373, 380]
POSE_2D_IDX = [1, 33, 263, 61, 291, 199]
MODEL_POINTS_3D = np.array([
    (0.0, 0.0, 0.0),
    (-30.0, -30.0, -30.0),
    (30.0, -30.0, -30.0),
    (-50.0, 30.0, -30.0),
    (50.0, 30.0, -30.0),
    (0.0, 80.0, -50.0)
], dtype=np.float64)
MOUTH_TOP_IDX = 13
MOUTH_BOTTOM_IDX = 14
MOUTH_LEFT_IDX = 61
MOUTH_RIGHT_IDX = 291

def eye_aspect_ratio(landmarks, eye_indices, img_w, img_h):
    pts = [(landmarks[idx].x * img_w, landmarks[idx].y * img_h) for idx in eye_indices]
    A = math.dist(pts[1], pts[5])
    B = math.dist(pts[2], pts[4])
    C = math.dist(pts[0], pts[3])
    return (A + B) / (2.0 * C) if C != 0 else 0.0

def get_head_pose(landmarks, img_w, img_h):
    image_points = np.array([(landmarks[idx].x * img_w, landmarks[idx].y * img_h) for idx in POSE_2D_IDX], dtype=np.float64)
    focal_length = img_w
    center = (img_w / 2, img_h / 2)
    camera_matrix = np.array([[focal_length, 0, center[0]], [0, focal_length, center[1]], [0, 0, 1]], dtype=np.float64)
    dist_coeffs = np.zeros((4,1))
    try:
        success, rotation_vector, translation_vector = cv2.solvePnP(MODEL_POINTS_3D, image_points, camera_matrix, dist_coeffs, flags=cv2.SOLVEPNP_ITERATIVE)
        if not success:
            return None
        rmat, _ = cv2.Rodrigues(rotation_vector)
        sy = math.sqrt(rmat[0,0]**2 + rmat[1,0]**2)
        singular = sy < 1e-6
        if not singular:
            x = math.degrees(math.atan2(rmat[2,1], rmat[2,2]))
            y = math.degrees(math.atan2(-rmat[2,0], sy))
            z = math.degrees(math.atan2(rmat[1,0], rmat[0,0]))
        else:
            x = math.degrees(math.atan2(-rmat[1,2], rmat[1,1]))
            y = math.degrees(math.atan2(-rmat[2,0], sy))
            z = 0
        return {"pitch": x, "yaw": y, "roll": z}
    except:
        return None


def mouth_aspect_ratio(landmarks, img_w, img_h):
    top = landmarks[MOUTH_TOP_IDX]
    bottom = landmarks[MOUTH_BOTTOM_IDX]
    left = landmarks[MOUTH_LEFT_IDX]
    right = landmarks[MOUTH_RIGHT_IDX]
    top_pt = (top.x * img_w, top.y * img_h)
    bottom_pt = (bottom.x * img_w, bottom.y * img_h)
    left_pt = (left.x * img_w, left.y * img_h)
    right_pt = (right.x * img_w, right.y * img_h)
    vertical = math.dist(top_pt, bottom_pt)
    horizontal = math.dist(left_pt, right_pt)
    if horizontal <= 0:
        return 0.0
    return vertical / horizontal


def clamp_bbox(x1, y1, x2, y2, width, height):
    return (
        max(0, min(x1, width)),
        max(0, min(y1, height)),
        max(0, min(x2, width)),
        max(0, min(y2, height))
    )


def bbox_iou(box_a, box_b) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter_area
    if union <= 0:
        return 0.0
    return inter_area / union


def food_near_face(face_bbox, food_bbox, frame_w, frame_h) -> bool:
    fx1, fy1, fx2, fy2 = face_bbox
    cx = (fx1 + fx2) / 2.0
    cy = (fy1 + fy2) / 2.0
    fw = fx2 - fx1
    fh = fy2 - fy1
    expanded_half_w = fw * FACE_FOOD_EXPANSION / 2.0
    expanded_half_h = fh * FACE_FOOD_EXPANSION / 2.0
    expanded_box = clamp_bbox(
        cx - expanded_half_w,
        cy - expanded_half_h,
        cx + expanded_half_w,
        cy + expanded_half_h,
        frame_w,
        frame_h
    )
    if bbox_iou(expanded_box, food_bbox) >= FOOD_IOU_THRESHOLD:
        return True
    ox1, oy1, ox2, oy2 = food_bbox
    obj_cx = (ox1 + ox2) / 2.0
    obj_cy = (oy1 + oy2) / 2.0
    ex1, ey1, ex2, ey2 = expanded_box
    if ex1 <= obj_cx <= ex2 and ey1 <= obj_cy <= ey2:
        return True
    if fw <= 0 or fh <= 0:
        return False
    dx = abs(obj_cx - cx) / fw
    dy = abs(obj_cy - cy) / fh
    return max(dx, dy) <= FOOD_CENTER_THRESHOLD

# -------------------------
# Monitoring Thread
# -------------------------
class MonitoringThread(threading.Thread):
    def __init__(self, camera_index=CAMERA_INDEX):
        super().__init__()
        self.camera_index = camera_index
        self.stop_event = threading.Event()
        self.capture = None
        self.current_frame = None
        self.employees_embeddings = []
        self.current_states = {}
        self.last_seen = {}
        self.mp_face_mesh = mp_face_mesh.FaceMesh(max_num_faces=5, refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5)
        self.load_embeddings_from_db()
        self.lock = threading.Lock()
        self.eating_counters: Dict[int, int] = {}
        self.eating_state: Dict[int, Dict[str, float]] = {}
        self.mouth_motion: Dict[int, Dict[str, float]] = {}

    def load_embeddings_from_db(self):
        self.employees_embeddings = db_get_all_embeddings()
        print("Loaded embeddings for employees:", [e[1] for e in self.employees_embeddings])

    def stop(self):
        self.stop_event.set()

    def finalize_all_states(self):
        now = time.time()
        utc_now_iso = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
        with self.lock:
            for emp_id, state_info in list(self.current_states.items()):
                start_ts = datetime.datetime.utcfromtimestamp(state_info["start"]+(timedelta(hours=5).total_seconds())).isoformat()
                duration = now - state_info["start"]
                extra_payload = _attach_reason(state_info.get("extra"), "stream_stopped")
                db_log_activity(
                    emp_id,
                    state_info["state"],
                    start_ts,
                    utc_now_iso,
                    duration,
                    extra_info=extra_payload
                )
                db_mark_attendance_checkout(emp_id, utc_now_iso)
                cancel_employee_session(emp_id)
            self.current_states.clear()
            self.last_seen.clear()
            self.mouth_motion.clear()

    def force_checkout(self, employee_id: int, checkout_epoch: float, reason: str) -> None:
        checkout_iso = datetime.datetime.utcfromtimestamp(checkout_epoch+(timedelta(hours=5).total_seconds())).isoformat()
        with self.lock:
            prev = self.current_states.pop(employee_id, None)
        if prev:
            start_iso = datetime.datetime.utcfromtimestamp(prev["start"]+(timedelta(hours=5).total_seconds())).isoformat()
            duration = max(0.0, checkout_epoch - prev["start"])
            extra_payload = _attach_reason(prev.get("extra"), reason)
            db_log_activity(
                employee_id,
                prev["state"],
                start_iso,
                checkout_iso,
                duration,
                extra_info=extra_payload
            )
        self.last_seen.pop(employee_id, None)
        self.eating_counters.pop(employee_id, None)
        self.eating_state.pop(employee_id, None)
        self.mouth_motion.pop(employee_id, None)
        db_mark_attendance_checkout(employee_id, checkout_iso)
        manual_attendance_ids.discard(employee_id)
        cancel_employee_session(employee_id)

    def run(self):
        print("Starting monitoring thread...")
        self.capture = cv2.VideoCapture(self.camera_index)
        if not self.capture.isOpened():
            print("Error: Camera index", self.camera_index, "not available.")
            return
        while not self.stop_event.is_set():
            ret, frame = self.capture.read()
            if not ret:
                time.sleep(0.1)
                continue
            img_h, img_w = frame.shape[:2]
            # Detect faces
            try:
                boxes, _ = mtcnn.detect(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            except:
                boxes = None
            recognized_faces = []
            if boxes is not None:
                for box in boxes:
                    x1, y1, x2, y2 = [int(max(0, v)) for v in box]
                    crop = frame[y1:y2, x1:x2]
                    embs = get_face_embeddings_from_image(crop)
                    matched_emp = None
                    matched_name = None
                    if embs:
                        emb = embs[0]
                        best_distance = 1.0
                        best_emp = None
                        for emp_id, name, db_emb in self.employees_embeddings:
                            dist = cosine_distance(emb, db_emb)
                            if dist < best_distance:
                                best_distance = dist
                                best_emp = (emp_id, name)
                        if best_emp and best_distance < EMBEDDING_DISTANCE_THRESHOLD:
                            matched_emp, matched_name = best_emp
                    if matched_emp:
                        recognized_faces.append(((x1, y1, x2, y2), matched_emp, matched_name, emb))
                        seen_now = time.time()
                        self.last_seen[matched_emp] = seen_now
                        resume_employee_session(matched_emp, now=seen_now)
                        self.maybe_mark_checkin(matched_emp)
            # MediaPipe
            mp_results = self.mp_face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            # YOLO detection
            try:
                yolo_results = yolo_model(frame, conf=0.15, verbose=False)
            except:
                yolo_results = None
            detected_objects = []
            if yolo_results:
                for res in yolo_results:
                    boxes_data = res.boxes
                    if boxes_data is None:
                        continue
                    for box in boxes_data:
                        cls = int(box.cls.cpu().numpy()[0])
                        raw_name = yolo_model.model.names[cls]
                        cls_name = raw_name.lower() if isinstance(raw_name, str) else str(raw_name).lower()
                        x1o, y1o, x2o, y2o = map(int, box.xyxy.cpu().numpy()[0])
                        detected_objects.append({"class": cls_name, "bbox": (x1o, y1o, x2o, y2o)})
            timestamp = time.time()
            for (bbox, emp_id, emp_name, emb_vec) in recognized_faces:
                x1, y1, x2, y2 = bbox
                face_center = ((x1 + x2)//2, (y1 + y2)//2)
                face_w, face_h = x2-x1, y2-y1
                ear_val = None
                pitch = None
                mouth_ratio = None
                if mp_results and mp_results.multi_face_landmarks:
                    best_idx = None
                    best_dist = 1e9
                    for idx, fm in enumerate(mp_results.multi_face_landmarks):
                        lm = fm.landmark[1]
                        lm_x, lm_y = int(lm.x*img_w), int(lm.y*img_h)
                        d = math.hypot(lm_x - face_center[0], lm_y - face_center[1])
                        if d < best_dist:
                            best_dist = d
                            best_idx = idx
                    if best_idx is not None and best_dist < max(face_w, face_h)*1.5:
                        fm = mp_results.multi_face_landmarks[best_idx]
                        ear_l = eye_aspect_ratio(fm.landmark, LEFT_EYE_INDICES, img_w, img_h)
                        ear_r = eye_aspect_ratio(fm.landmark, RIGHT_EYE_INDICES, img_w, img_h)
                        ear_val = (ear_l + ear_r)/2.0
                        pose = get_head_pose(fm.landmark, img_w, img_h)
                        if pose:
                            pitch = pose["pitch"]
                        mouth_ratio = mouth_aspect_ratio(fm.landmark, img_w, img_h)
                mouth_motion_detected = False
                mouth_state = self.mouth_motion.get(emp_id, {"last_ratio": None, "score": 0, "last_motion": 0.0})
                last_ratio = mouth_state.get("last_ratio")
                score = float(mouth_state.get("score", 0))
                last_motion_ts = float(mouth_state.get("last_motion", 0.0))
                if mouth_ratio is not None:
                    if last_ratio is None:
                        last_ratio = mouth_ratio
                    if mouth_ratio >= MOUTH_OPEN_THRESHOLD and abs(mouth_ratio - last_ratio) >= MOUTH_MOVEMENT_DELTA:
                        score = min(score + 1, MOUTH_FRAMES_REQUIRED + MOUTH_FRAMES_COOLDOWN)
                        last_motion_ts = timestamp
                    elif timestamp - last_motion_ts > MOUTH_ACTIVITY_GRACE_SECONDS:
                        score = max(0.0, score - 1)
                    mouth_state["last_ratio"] = mouth_ratio
                else:
                    if timestamp - last_motion_ts > MOUTH_ACTIVITY_GRACE_SECONDS:
                        score = max(0.0, score - 1)
                mouth_state["score"] = score
                mouth_state["last_motion"] = last_motion_ts
                mouth_motion_detected = score >= MOUTH_FRAMES_REQUIRED
                self.mouth_motion[emp_id] = mouth_state
                near_food = False
                current_food_event: Optional[str] = None
                food_items: List[str] = []
                counter = self.eating_counters.get(emp_id, 0)
                eating_info = self.eating_state.get(emp_id, default_eating_state())
                detection_this_frame = False

                for obj in detected_objects:
                    if obj["class"] in EATING_CLASSES:
                        if food_near_face((x1, y1, x2, y2), obj["bbox"], img_w, img_h):
                            detection_this_frame = True
                            counter = min(counter + 1, EATING_FRAMES_REQUIRED + EATING_FRAMES_COOLDOWN)
                            food_items.append(obj["class"])
                            eating_info["last_detected"] = timestamp
                            eating_info["last_items"] = list(food_items)
                            obj_event = "drinking" if obj["class"] in DRINKING_ITEMS else "eating"
                            eating_info["last_event"] = obj_event
                            if counter >= EATING_FRAMES_REQUIRED:
                                if not eating_info.get("active"):
                                    eating_info["active"] = True
                                    eating_info["start"] = timestamp
                                if timestamp - eating_info["start"] >= EATING_HOLD_SECONDS:
                                    near_food = True
                                    current_food_event = obj_event
                            break

                if not detection_this_frame and mouth_motion_detected:
                    detection_this_frame = True
                    counter = min(counter + 1, EATING_FRAMES_REQUIRED + EATING_FRAMES_COOLDOWN)
                    if "mouth_motion" not in food_items:
                        food_items.append("mouth_motion")
                    eating_info["last_detected"] = timestamp
                    eating_info["last_items"] = list(food_items)
                    eating_info["last_event"] = "eating"
                    if not eating_info.get("active"):
                        eating_info["active"] = True
                        eating_info["start"] = timestamp
                    if timestamp - eating_info["start"] >= EATING_HOLD_SECONDS:
                        near_food = True
                        current_food_event = "eating"

                if not detection_this_frame:
                    last_seen_delta = timestamp - float(eating_info.get("last_detected", 0.0))
                    if eating_info.get("active") and last_seen_delta <= EATING_GRACE_SECONDS:
                        if timestamp - eating_info["start"] >= EATING_HOLD_SECONDS:
                            near_food = True
                            current_food_event = eating_info.get("last_event") or current_food_event
                        counter = max(counter - 1, 1)
                        if not food_items:
                            food_items = list(eating_info.get("last_items", []))
                    else:
                        counter = max(0, counter - 1)
                        if counter == 0:
                            eating_info = default_eating_state()
                else:
                    if eating_info.get("active") and timestamp - eating_info["start"] >= EATING_HOLD_SECONDS:
                        near_food = True
                        if current_food_event is None:
                            current_food_event = eating_info.get("last_event")
                    if not food_items:
                        food_items = list(eating_info.get("last_items", []))

                self.eating_counters[emp_id] = counter
                self.eating_state[emp_id] = eating_info
                # determine state
                state = "working"
                extra = {}
                event_tag: Optional[str] = None
                food_payload: Optional[Dict[str, Any]] = None
                if ear_val and ear_val<EAR_THRESHOLD:
                    state="eyes_closed"; extra["ear"]=float(ear_val)
                if pitch and abs(pitch)>HEAD_DOWN_PITCH_THRESHOLD:
                    state="head_down"; extra["pitch"]=float(pitch)
                if near_food:
                    resolved_event = current_food_event or eating_info.get("last_event") or "eating"
                    state = resolved_event
                    normalised_items = [obj.lower() for obj in food_items] if food_items else []
                    if not normalised_items:
                        normalised_items = [obj.lower() for obj in eating_info.get("last_items", [])]
                    drinking_match = resolved_event == "drinking" or any(item in DRINKING_ITEMS for item in normalised_items)
                    event_tag = "drinking" if drinking_match else "eating"
                    extra["food_items"]=normalised_items
                    extra["food_event"] = event_tag
                    food_payload = {"food_event": event_tag, "food_items": normalised_items}
                now = time.time()
                with self.lock:
                    prev = self.current_states.get(emp_id)
                    if prev is None:
                        payload = food_payload if food_payload is not None else (extra if extra else None)
                        self.current_states[emp_id]={"state":state,"start":now,"extra":payload}
                    else:
                        if prev["state"]!=state:
                            start_ts=datetime.datetime.utcfromtimestamp(prev["start"] + (timedelta(hours=5).total_seconds())).isoformat()
                            end_ts=datetime.datetime.utcfromtimestamp(now + (timedelta(hours=5).total_seconds())).isoformat()
                            duration=now-prev["start"]
                            db_log_activity(emp_id, prev["state"], start_ts, end_ts, duration, extra_info=prev.get("extra"))
                            payload = food_payload if food_payload is not None else (extra if extra else None)
                            self.current_states[emp_id]={"state":state,"start":now,"extra":payload}
                        # update last seen
                        else:
                            if state in EAT_DRINK_STATES and food_payload is not None:
                                prev_extra = prev.setdefault("extra", {})
                                prev_extra["food_event"] = event_tag or prev_extra.get("food_event")
                                if food_payload.get("food_items"):
                                    prev_extra["food_items"] = food_payload["food_items"]
                            elif state not in EAT_DRINK_STATES:
                                prev["extra"] = extra if extra else None
                    self.last_seen[emp_id]=now
                # draw for live preview
                cv2.rectangle(frame, (x1,y1),(x2,y2),(0,255,0),2)
                cv2.putText(frame, f"{emp_name}:{state}", (x1,y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0),2)
            # handle absent
            current_time = time.time()
            for emp_id, last in list(self.last_seen.items()):
                if current_time - last > FACE_LOST_SECONDS:
                    with self.lock:
                        prev = self.current_states.get(emp_id)
                        if prev:
                            start_ts = datetime.datetime.utcfromtimestamp(prev["start"]+(timedelta(hours=5).total_seconds())).isoformat()
                            end_ts = datetime.datetime.utcfromtimestamp(last+(timedelta(hours=5).total_seconds())).isoformat()
                            duration = last - prev["start"]
                            payload = _attach_reason(prev.get("extra"), "face_lost")
                            db_log_activity(emp_id, prev["state"], start_ts, end_ts, duration, extra_info=payload)
                            del self.current_states[emp_id]
                    pause_employee_session(emp_id, now=last)
                    should_checkout = not session_is_configured(emp_id)
                    if should_checkout:
                        checkout_iso = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
                        db_mark_attendance_checkout(emp_id, checkout_iso)
                        manual_attendance_ids.discard(emp_id)
                    del self.last_seen[emp_id]
                    self.eating_counters.pop(emp_id, None)
                    self.eating_state.pop(emp_id, None)
                    self.mouth_motion.pop(emp_id, None)
            for emp_id, expiry_ts, reason in consume_expired_sessions(now=current_time):
                self.force_checkout(emp_id, expiry_ts, reason)
            # show live frame
            # At the end of each loop iteration:
            self.current_frame = frame.copy()
            # cv2.imshow("Employee Monitoring", frame)
            # if cv2.waitKey(1)&0xFF==27:  # ESC to stop
            #     self.stop()
        # cleanup
        self.finalize_all_states()
        # cv2.destroyAllWindows()
        if self.capture: self.capture.release()
        print("Monitoring thread stopped.")

    def maybe_mark_checkin(self, employee_id):
        conn=sqlite3.connect(DB_PATH)
        cur=conn.cursor()
        cur.execute("SELECT id, check_in FROM attendance WHERE employee_id=? ORDER BY id DESC LIMIT 1",(employee_id,))
        row=cur.fetchone()
        if row and row[1] is not None:
            conn.close()
            return
        conn.close()
        db_mark_attendance_checkin(employee_id, (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat())

monitor_thread=None

# -------------------------
# Flask App
# -------------------------
app=Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # current directory
from flask import Response

@app.route('/video_feed')
def video_feed():
    return Response(stream_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def stream_frames():
    while True and monitor_thread is not None:
        if monitor_thread.current_frame is None:
            time.sleep(0.03)
            continue

        frame = monitor_thread.current_frame

        # Encode to JPEG
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        
        frame_bytes = buffer.tobytes()

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# Allowed image extensions
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}

@app.route('/images/<path:filename>')
def serve_image(filename):
    # Check file extension
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return abort(403)  # Forbidden

    # Build absolute path
    file_path = os.path.join(BASE_DIR, filename)

    # Check if file exists
    if not os.path.isfile(file_path):
        return abort(404)  # Not found

    return send_from_directory(BASE_DIR, filename)

@app.route("/get_employees")
def get_employees():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # access columns by name
    cur = conn.cursor()

    # Fetch all employees
    cur.execute("""
        SELECT employee_id, full_name, email, profile_image_path
        FROM employees
        ORDER BY full_name
    """)
    rows = cur.fetchall()
    conn.close()

    employees = []
    for row in rows:
        employees.append({
            "id": row["employee_id"],
            "emp_id": row["employee_id"],  # using employee_id as emp_id
            "name": row["full_name"],
            "contact": row["email"],
            "front_img": f"http://127.0.0.1:5001/images/{row['profile_image_path']}",
            "left_img": None,
            "right_img": None
        })

    return jsonify(employees)

@app.get("/get_reports")
def get_report_all_filtered():
    employee_id = request.args.get("employee_id", type=int)
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    rows = gather_report_rows(employee_id=employee_id, start_date=start_date, end_date=end_date)
    return jsonify({"reports": rows})

@app.route("/enroll", methods=["POST"])
def enroll_employee():
    try:
        full_name = request.form.get("full_name")
        email = request.form.get("email")
        if not full_name or not email:
            return jsonify({"error":"full_name and email required"}),400
        images=[]
        for idx in ["image1","image2","image3"]:
            f=request.files.get(idx)
            if f is None:
                return jsonify({"error":f"{idx} required"}),400
            arr=np.frombuffer(f.read(),dtype=np.uint8)
            img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
            if img is None:
                return jsonify({"error":f"cannot read {idx}"}),400
            images.append(img)
        embeddings=[]
        for img in images:
            embs=get_face_embeddings_from_image(img)
            if not embs:
                return jsonify({"error":"face not detected in one image"}),400
            embeddings.append(embs[0])
        avg_emb=np.mean(np.stack(embeddings,axis=0),axis=0)
        avg_norm = np.linalg.norm(avg_emb)
        if avg_norm > 0:
            avg_emb = (avg_emb / avg_norm).astype(np.float32)
        else:
            avg_emb = avg_emb.astype(np.float32)
        filename=f"{IMG_DIR}/employee_{int(time.time())}.jpg"
        cv2.imwrite(filename,images[0])
        emp_id=db_insert_employee(full_name,email,avg_emb,profile_image_path=filename)
        global monitor_thread
        if monitor_thread: monitor_thread.load_embeddings_from_db()
        return jsonify({"status":"enrolled","employee_id":emp_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error":str(e)}),500


@app.route("/session/configure", methods=["POST"])
def configure_session_endpoint():
    payload = request.get_json(silent=True) or {}
    employee_id = payload.get("employee_id")
    if employee_id is None:
        return jsonify({"error": "employee_id is required"}), 400
    try:
        employee_id = int(employee_id)
    except (TypeError, ValueError):
        return jsonify({"error": "employee_id must be an integer"}), 400

    duration_seconds = payload.get("duration_seconds")
    duration_minutes = payload.get("duration_minutes")
    if duration_seconds is None and duration_minutes is None:
        return jsonify({"error": "Provide duration_seconds or duration_minutes"}), 400
    try:
        if duration_seconds is None:
            duration_seconds = float(duration_minutes) * 60.0
        else:
            duration_seconds = float(duration_seconds)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid duration value"}), 400

    snapshot = configure_employee_session(employee_id, duration_seconds)
    return jsonify({"message": "session_configured", "session": snapshot})


@app.route("/session/status", methods=["GET"])
def session_status_endpoint():
    employee_id = request.args.get("employee_id", default=None, type=int)
    status_rows = get_session_status(employee_id)
    if not status_rows:
        return jsonify({"sessions": [], "message": "no_active_session"})
    return jsonify({"sessions": status_rows})


@app.route("/session/cancel", methods=["POST"])
def session_cancel_endpoint():
    payload = request.get_json(silent=True) or {}
    employee_id = payload.get("employee_id")
    if employee_id is None:
        return jsonify({"error": "employee_id is required"}), 400
    try:
        employee_id = int(employee_id)
    except (TypeError, ValueError):
        return jsonify({"error": "employee_id must be an integer"}), 400

    checkout_now = bool(payload.get("checkout_now", False))
    snapshot = cancel_employee_session(employee_id)
    if checkout_now:
        checkout_epoch = time.time()
        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.force_checkout(employee_id, checkout_epoch, "session_cancelled")
        else:
            checkout_iso = datetime.datetime.utcfromtimestamp(checkout_epoch+(timedelta(hours=5).total_seconds())).isoformat()
            db_mark_attendance_checkout(employee_id, checkout_iso)
            manual_attendance_ids.discard(employee_id)
    if not snapshot:
        return jsonify({"message": "no_session_found", "employee_id": employee_id}), 404
    return jsonify({"message": "session_cancelled", "session": snapshot})

@app.route("/start_stream", methods=["POST"])
def start_stream():
    global monitor_thread, last_report_employee_id
    try:
        employee_id = extract_employee_id_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    already_running = monitor_thread is not None and monitor_thread.is_alive()
    if not already_running:
        monitor_thread = MonitoringThread(camera_index=CAMERA_INDEX)
        monitor_thread.daemon = True
        monitor_thread.start()

    response_payload = {"status": "already_running" if already_running else "started"}

    if employee_id is not None:
        check_in_ts = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
        db_mark_attendance_checkin(employee_id, check_in_ts)
        manual_attendance_ids.add(employee_id)
        last_report_employee_id = employee_id
        response_payload.update({
            "checked_in": [employee_id]
        })

    return jsonify(response_payload)

@app.route("/stop_stream", methods=["POST"])
def stop_stream():
    global monitor_thread, last_report_employee_id
    try:
        employee_id = extract_employee_id_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    checked_out_ids = []

    if monitor_thread is None:
        if employee_id is not None:
            checkout_ts = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
            db_mark_attendance_checkout(employee_id, checkout_ts)
            manual_attendance_ids.discard(employee_id)
            checked_out_ids.append(employee_id)
            last_report_employee_id = employee_id
        return jsonify({"status": "not_running", "checked_out": checked_out_ids})

    thread_ref = monitor_thread
    thread_ref.stop()
    thread_ref.join(timeout=5.0)
    thread_ref.finalize_all_states()
    monitor_thread = None

    checkout_ts = (datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()
    if employee_id is not None:
        db_mark_attendance_checkout(employee_id, checkout_ts)
        manual_attendance_ids.discard(employee_id)
        checked_out_ids.append(employee_id)
        last_report_employee_id = employee_id
        cancel_employee_session(employee_id)

    for manual_id in list(manual_attendance_ids):
        db_mark_attendance_checkout(manual_id, checkout_ts)
        manual_attendance_ids.discard(manual_id)
        if manual_id not in checked_out_ids:
            checked_out_ids.append(manual_id)
        last_report_employee_id = manual_id
        cancel_employee_session(manual_id)

    return jsonify({"status": "stopped", "checked_out": checked_out_ids, "check_out_time": checkout_ts})


@app.route('/delete_employee/<int:employee_id>', methods=['DELETE', 'POST'])
def delete_employee(employee_id):
    global monitor_thread, last_report_employee_id
    try:
        employee_id = int(employee_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if employee_id is None:
        return jsonify({"error": "employee_id is required"}), 400

    deleted = db_delete_employee(employee_id)
    if not deleted:
        return jsonify({"error": "Employee not found"}), 404

    manual_attendance_ids.discard(employee_id)
    if last_report_employee_id == employee_id:
        last_report_employee_id = None
    cancel_employee_session(employee_id)

    if monitor_thread is not None:
        if monitor_thread.is_alive():
            with monitor_thread.lock:
                monitor_thread.current_states.pop(employee_id, None)
                monitor_thread.last_seen.pop(employee_id, None)
                monitor_thread.eating_counters.pop(employee_id, None)
                monitor_thread.eating_state.pop(employee_id, None)
        monitor_thread.load_embeddings_from_db()

    return jsonify({"success": True, "message": "Employee deleted", "employee_id": employee_id})


@app.route("/report", methods=["GET"])
def generate_report_endpoint():
    global last_report_employee_id
    try:
        employee_id = request.args.get("employee_id", type=int)
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        if employee_id is not None:
            last_report_employee_id = employee_id
        csv_path = write_report_csv(
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
            prefix="report"
        )
        if not csv_path:
            return jsonify({"error": "Failed to generate report"}), 500
        return send_file(csv_path,as_attachment=True)
    except ValueError as ve:
        return jsonify({"error":str(ve)}),400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error":str(e)}),500
    
@app.route("/report_download", methods=["GET"])
def report_download_save_to_folder():
    global last_report_employee_id
    try:
        employee_id = request.args.get("employee_id", type=int)
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        if employee_id is not None:
            last_report_employee_id = employee_id

        # Use SAME CSV builder but DO NOT send file, just save it
        csv_path = write_report_csv(
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
            prefix="report"  # Prefix for filename
        )

        if not csv_path:
            return jsonify({"error": "Failed to generate report"}), 500

        # We only return success — file is already saved
        return jsonify({
            "status": "saved",
            "file_path": csv_path
        })

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/save_log_pdf", methods=["POST"])
def save_log_pdf():
    try:
        if 'pdf' not in request.files:
            return jsonify({"status": "error", "message": "No file uploaded"}), 400

        pdf_file = request.files['pdf']

        # Generate timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"logs_{timestamp}.pdf"

        save_path = os.path.join(LOGS_DIR, filename)
        pdf_file.save(save_path)

        return jsonify({"status": "ok", "path": save_path})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route("/logs", methods=["GET"])
def get_logs():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Fetch activities in range
    query = """
        SELECT 
            date(al.start_timestamp) as activity_date,
            e.employee_id,
            e.full_name,
            al.activity_type,
            SUM(al.duration_seconds) as total_duration
        FROM activity_logs al
        LEFT JOIN employees e ON al.employee_id = e.employee_id
    """
    params = ()
    if start_date and end_date:
        query += " WHERE date(al.start_timestamp) BETWEEN ? AND ?"
        params = (start_date, end_date)

    query += " GROUP BY activity_date, e.employee_id, al.activity_type"
    query += " ORDER BY activity_date DESC, e.employee_id"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    reports = {}

    for row in rows:
        date_str = row[0]
        emp_id = str(row[1])
        emp_name = row[2] or "—"
        activity_name = row[3] or "—"
        total_duration = row[4] or 0

        # Attendance: Present if any activity exists for that date
        attendance_status = "Present"

        record = {
            "date": date_str,
            "employee_id": row[1],
            "employee_name": emp_name,
            "activity_name": activity_name,
            "activity_duration": total_duration,
            "attendance": attendance_status
        }

        if emp_id not in reports:
            reports[emp_id] = []
        reports[emp_id].append(record)

    conn.close()
    return jsonify(reports)

# -------------------------
# Scheduled reports
# -------------------------
def scheduled_report(job_id="daily_report", employee_id=None):
    print(f"Running scheduled report {job_id} at {(datetime.datetime.utcnow() + timedelta(hours=5)).isoformat()}")
    end=datetime.date.today()
    start=end-datetime.timedelta(days=1)
    csv_path = write_report_csv(
        employee_id=employee_id,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        prefix=f"scheduled_report_{job_id}"
    )
    if csv_path:
        print("Saved scheduled report to", csv_path)
    else:
        print("Failed to generate scheduled report", job_id)

scheduler.add_job(scheduled_report,'cron',hour=17,minute=0,id="daily_report")

atexit.register(export_reports_on_shutdown)
signal.signal(signal.SIGINT, _handle_sigint)

# -------------------------
# Startup
# -------------------------
if __name__=="__main__":
    init_db()
    print("Database initialized at",DB_PATH)
    app.run(host="0.0.0.0",port=5001,threaded=True)
