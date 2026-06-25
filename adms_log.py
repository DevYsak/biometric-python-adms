#!/usr/bin/env python3
"""
Biometric Attendance ADMS server + premium dashboard  (stdlib only, no pip).

Two jobs in one file:
  1. ADMS push receiver -> the ESSL/ZKTeco device POSTs punches to /iclock/cdata.
  2. HRMS-style dashboard -> /dashboard  (UI in dashboard.html, data via /api/dashboard).

Every individual punch is stored in SQLite (table `punches`). IN/OUT direction is
derived by alternating each employee's punches for the day (1st=IN, 2nd=OUT, ...),
which is how ESSL/ZKTeco single-button punches are read.

Stdlib only (http.server + sqlite3). Run:
    python3 adms_log.py            # port 5000 (default)
    python3 adms_log.py 80         # or PORT=80 python3 adms_log.py
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone
from contextlib import closing
from zoneinfo import ZoneInfo
import json
import os
import sys
import sqlite3
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard.html")

STANDARD_DAY_MIN = 480       # 8h; beyond this counts as overtime
TIMELINE_START_H = 8         # dashboard timeline axis start (08:00)
TIMELINE_END_H = 18          # dashboard timeline axis end   (18:00)

# The device records punches in this local timezone. The server may run on UTC,
# so we compute "now"/"today" in this zone to match the punch timestamps.
TIMEZONE = "Asia/Kolkata"
TZ_OFFSET_FALLBACK = timedelta(hours=5, minutes=30)   # used if tzdata is unavailable
try:
    _TZ = ZoneInfo(TIMEZONE)
except Exception:
    _TZ = None


def now_local():
    """Current wall-clock time in the device's timezone (independent of server TZ)."""
    if _TZ:
        return datetime.now(_TZ).replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None) + TZ_OFFSET_FALLBACK

# Device PIN -> employee name
EMPLOYEES = {
    "1": "EMAD", "2": "NIKITA", "3": "NICK", "4": "RUSTOM", "5": "MAZHAR",
    "6": "MEHUL", "7": "Carol", "8": "Hasan Mirza", "9": "Esha", "10": "Ankita",
    "11": "Shivani", "12": "Sakshi", "13": "Walid", "14": "Gurmeet",
    "15": "ABDULBASIT", "16": "Mayuresh", "17": "Yogesh", "18": "Pratish",
    "19": "Shivendra", "20": "Saad", "21": "Digambar", "22": "Shradha",
    "23": "Abhishek Bhoir", "24": "Altamash", "25": "Reeba", "26": "Sunita",
    "27": "Suhail khan", "28": "Gayatri", "29": "Sunil", "30": "Surekha",
    "31": "Kajal", "32": "Zaheer", "33": "Kashif", "34": "Chinmay",
    "35": "Atif", "36": "Sudhanshu",
}

# ── Employee metadata (the device does NOT send this -- edit freely) ──────────
# Shift definitions drive Late / Early-leave / Completed-shift logic.
SHIFTS = {
    "General": {"start": "09:00", "end": "18:00", "grace": 15, "hours": 480},
    "Morning": {"start": "07:00", "end": "15:00", "grace": 10, "hours": 480},
    "Evening": {"start": "14:00", "end": "22:00", "grace": 10, "hours": 480},
}
DEFAULT_META = {"department": "General", "designation": "Staff", "shift": "General"}

# emp_id -> department (designation/shift default unless added below).
EMPLOYEE_DEPT = {
    "1": "Sales", "2": "Sales", "3": "Operations", "4": "Marketing", "5": "IT",
    "6": "Accounts", "7": "Sales", "8": "Operations", "9": "Marketing",
    "10": "HR", "11": "Sales", "12": "Operations", "13": "Marketing", "14": "IT",
    "15": "Accounts", "16": "Sales", "17": "Operations", "18": "Marketing",
    "19": "IT", "20": "Accounts", "21": "Sales", "22": "Operations",
    "23": "Marketing", "24": "IT", "25": "Accounts", "26": "Sales",
    "27": "Operations", "28": "Marketing", "29": "IT", "30": "Accounts",
    "31": "Sales", "32": "Operations", "33": "Marketing", "34": "IT",
    "35": "Accounts", "36": "Admin",
}
# Optional richer overrides: emp_id -> {"department","designation","shift"}.
EMPLOYEE_META = {}


def emp_meta(emp_id):
    if emp_id in EMPLOYEE_META:
        m = {**DEFAULT_META, **EMPLOYEE_META[emp_id]}
    else:
        m = {**DEFAULT_META, "department": EMPLOYEE_DEPT.get(emp_id, "General")}
    return m


def initials(name):
    parts = [p for p in name.replace("(", " ").split() if p[:1].isalnum()]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


# ── Server -> device command queue ───────────────────────────────────────────
# Commands are delivered when the device polls GET /iclock/getrequest. Used to
# force the device to re-upload its stored attendance logs (DATA QUERY ATTLOG).
_CMD_LOCK = threading.Lock()
_PENDING = {}        # serial_number -> [ "C:<id>:<command>", ... ]
_CMD_ID = 0
LAST_SN = None       # most recent real device serial seen


def remember_sn(sn):
    global LAST_SN
    if sn and sn not in ("?", "TEST"):
        LAST_SN = sn


def queue_command(sn, command):
    global _CMD_ID
    with _CMD_LOCK:
        _CMD_ID += 1
        cid = _CMD_ID
        _PENDING.setdefault(sn, []).append(f"C:{cid}:{command}")
    return cid


def pop_commands(sn):
    with _CMD_LOCK:
        return _PENDING.pop(sn, [])


def queue_pull_attlog(sn, start_date, end_date):
    """Tell the device to re-upload all ATTLOG records in [start_date, end_date]."""
    cmd = (f"DATA QUERY ATTLOG StartTime={start_date} 00:00:00"
           f"\tEndTime={end_date} 23:59:59")
    return queue_command(sn, cmd)


# ── Database ─────────────────────────────────────────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS punches (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_id     TEXT NOT NULL,
                emp_name   TEXT NOT NULL,
                punch_dt   TEXT NOT NULL,
                punch_date TEXT NOT NULL,
                device_sn  TEXT,
                raw_status TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(emp_id, punch_dt)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_punch_date ON punches(punch_date)")


def store_punch(emp_id, dt_str, device_sn="", raw_status=""):
    """Insert one punch (deduped on emp_id+datetime). Returns True if newly added."""
    emp_name = EMPLOYEES.get(emp_id, f"Unknown ({emp_id})")
    punch_date = dt_str.split(" ")[0]
    with closing(db()) as conn, conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO punches
               (emp_id, emp_name, punch_dt, punch_date, device_sn, raw_status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (emp_id, emp_name, dt_str, punch_date, device_sn, raw_status,
             now_local().strftime("%Y-%m-%d %H:%M:%S")),
        )
        new = cur.rowcount > 0
    if new:
        print(f"  PUNCH  {emp_name:<16} | {dt_str} | SN={device_sn}")
    return new


# ── Analytics ────────────────────────────────────────────────────────────────
def _fmt_hm(minutes):
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}h {minutes % 60:02d}m"


def _to_pct(dt):
    mins = dt.hour * 60 + dt.minute
    span = (TIMELINE_END_H - TIMELINE_START_H) * 60
    pct = (mins - TIMELINE_START_H * 60) / span * 100
    return max(0.0, min(100.0, round(pct, 2)))


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _avg_time(minutes_list):
    if not minutes_list:
        return "--:--"
    return (datetime(2000, 1, 1)
            + timedelta(minutes=sum(minutes_list) / len(minutes_list))).strftime("%H:%M")


def analyze_employee(rows, emp_id=None, is_today=True, now_dt=None):
    """rows: sqlite Rows for one employee+day, ordered ascending by punch_dt."""
    emp_id = emp_id if emp_id is not None else (rows[0]["emp_id"] if rows else None)
    meta = emp_meta(emp_id) if emp_id is not None else dict(DEFAULT_META)
    shift = SHIFTS.get(meta["shift"], SHIFTS["General"])
    now_dt = now_dt or now_local()

    times = [datetime.strptime(r["punch_dt"], "%Y-%m-%d %H:%M:%S") for r in rows]
    n = len(times)
    events = []
    for i, t in enumerate(times):
        events.append({
            "time": t.strftime("%H:%M:%S"),
            "type": "IN" if i % 2 == 0 else "OUT",
            "pct": _to_pct(t), "first": i == 0, "last": i == n - 1,
        })

    # Pair IN -> OUT into sessions; a trailing unpaired IN is an open session.
    sessions, work_sec, brk_sec = [], 0, 0
    i = 0
    while i < n:
        if i + 1 < n:
            dur = (times[i + 1] - times[i]).total_seconds()
            work_sec += dur
            sessions.append({"in": times[i].strftime("%H:%M:%S"),
                             "out": times[i + 1].strftime("%H:%M:%S"),
                             "dur": _fmt_hm(dur / 60), "open": False})
            if i + 2 < n:
                brk_sec += (times[i + 2] - times[i + 1]).total_seconds()
            i += 2
        else:
            sessions.append({"in": times[i].strftime("%H:%M:%S"),
                             "out": None, "dur": "--", "open": True})
            i += 1

    inside = (n % 2 == 1)
    work_min = work_sec / 60
    brk_min = brk_sec / 60
    overtime_min = max(0, work_min - shift["hours"])

    first_min = (times[0].hour * 60 + times[0].minute) if n else None
    last_min = (times[-1].hour * 60 + times[-1].minute) if n else None
    late = bool(n and first_min > _mins(shift["start"]) + shift["grace"])
    delay_min = max(0, first_min - _mins(shift["start"])) if n else 0
    early_leave = bool(n and not inside and last_min < _mins(shift["end"]))

    if n == 0:
        status = "Absent"
    elif inside:
        status = "Inside Office" if is_today else "Missing Punch Out"
    else:
        status = "Completed Shift"

    elapsed_min = 0
    if inside and is_today and n:
        elapsed_min = max(0, (now_dt - times[-1]).total_seconds() / 60)

    return {
        "events": events, "sessions": sessions,
        "first_punch": times[0].strftime("%H:%M:%S") if n else None,
        "last_punch": times[-1].strftime("%H:%M:%S") if n else None,
        "_first_min": first_min, "_last_min": last_min,
        "punch_count": n,
        "working_min": round(work_min), "working": _fmt_hm(work_min),
        "break_min": round(brk_min), "break": _fmt_hm(brk_min),
        "overtime_min": round(overtime_min), "overtime": _fmt_hm(overtime_min),
        "inside": inside, "late": late, "delay_min": round(delay_min),
        "early_leave": early_leave, "status": status,
        "elapsed_min": round(elapsed_min), "elapsed": _fmt_hm(elapsed_min),
        "department": meta["department"], "designation": meta["designation"],
        "shift": meta["shift"],
    }


def build_dashboard(date_str):
    now_dt = now_local()
    is_today = (date_str == now_dt.strftime("%Y-%m-%d"))
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM punches WHERE punch_date=? ORDER BY emp_id, punch_dt",
            (date_str,),
        ).fetchall()
        cutoff = (now_dt - timedelta(days=30)).strftime("%Y-%m-%d")
        active_ids = {
            r["emp_id"] for r in conn.execute(
                "SELECT DISTINCT emp_id FROM punches WHERE punch_date>=?", (cutoff,)
            ).fetchall()
        }

    by_emp = {}
    for r in rows:
        by_emp.setdefault(r["emp_id"], []).append(r)

    # Department / shift rosters (everyone, for attendance %).
    dept_stats, shift_stats = {}, {}
    for emp_id in EMPLOYEES:
        m = emp_meta(emp_id)
        dept_stats.setdefault(m["department"], {"name": m["department"], "total": 0,
            "present": 0, "late": 0, "work_min": 0})["total"] += 1
        shift_stats.setdefault(m["shift"], {"name": m["shift"], "total": 0,
            "present": 0, "late": 0, "work_min": 0})["total"] += 1

    table, timeline, feed = [], [], []
    late_list, inside_list = [], []
    total_punches = total_work = total_ot = 0
    present = inside_cnt = completed = missing_out = late_cnt = early_cnt = 0
    first_buckets = {"before9": 0, "h9_10": 0, "h10_11": 0, "after11": 0}
    work_buckets = [0, 0, 0, 0, 0]
    first_mins, last_mins = [], []

    for emp_id, erows in by_emp.items():
        a = analyze_employee(erows, emp_id, is_today, now_dt)
        name = erows[0]["emp_name"]
        present += 1
        total_punches += a["punch_count"]
        total_work += a["working_min"]
        total_ot += a["overtime_min"]
        first_mins.append(a["_first_min"])
        last_mins.append(a["_last_min"])

        if a["inside"]:
            inside_cnt += 1
            if is_today:
                inside_list.append({"name": name, "department": a["department"],
                    "last_in": a["last_punch"], "elapsed": a["elapsed"]})
            else:
                missing_out += 1
        else:
            completed += 1
        if a["late"]:
            late_cnt += 1
            late_list.append({"name": name, "department": a["department"],
                "first_punch": a["first_punch"], "delay": _fmt_hm(a["delay_min"])})
        if a["early_leave"]:
            early_cnt += 1

        ds = dept_stats[a["department"]]
        ds["present"] += 1; ds["work_min"] += a["working_min"]; ds["late"] += a["late"]
        ss = shift_stats[a["shift"]]
        ss["present"] += 1; ss["work_min"] += a["working_min"]; ss["late"] += a["late"]

        fh = a["_first_min"] // 60
        key = ("before9" if fh < 9 else "h9_10" if fh < 10 else "h10_11" if fh < 11 else "after11")
        first_buckets[key] += 1
        wh = a["working_min"] / 60
        work_buckets[4 if wh >= 8 else int(wh // 2)] += 1

        entry = {"emp_id": emp_id, "name": name, "initials": initials(name), **a}
        for k in ("_first_min", "_last_min"):
            entry.pop(k, None)
        table.append(entry)
        timeline.append({"emp_id": emp_id, "name": name, "first_punch": a["first_punch"],
            "last_punch": a["last_punch"], "inside": a["inside"], "events": a["events"]})
        for ev in a["events"]:
            feed.append({"time": ev["time"], "name": name, "type": ev["type"]})

    table.sort(key=lambda e: e["first_punch"] or "99")
    timeline.sort(key=lambda e: e["first_punch"] or "99")
    feed.sort(key=lambda f: f["time"], reverse=True)
    late_list.sort(key=lambda e: e["first_punch"], reverse=True)
    inside_list.sort(key=lambda e: e["elapsed"], reverse=True)

    total_emp = len(EMPLOYEES)
    absent = max(total_emp - present, 0)
    top10 = sorted(table, key=lambda e: e["working_min"], reverse=True)[:10]

    for d in dept_stats.values():
        d["attendance_pct"] = round(d["present"] / d["total"] * 100) if d["total"] else 0
        d["avg_working"] = _fmt_hm(d["work_min"] / d["present"]) if d["present"] else "00h 00m"
        d["absent"] = d["total"] - d["present"]
    for s in shift_stats.values():
        s["attendance_pct"] = round(s["present"] / s["total"] * 100) if s["total"] else 0
        s["avg_working"] = _fmt_hm(s["work_min"] / s["present"]) if s["present"] else "00h 00m"

    return {
        "now": now_dt.strftime("%H:%M:%S"),
        "date": date_str,
        "server_today": now_dt.strftime("%Y-%m-%d"),
        "is_today": is_today,
        "date_label": datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y"),
        "kpis": {
            "total_employees": total_emp, "active": len(active_ids) or present,
            "present": present, "absent": absent,
            "present_pct": round(present / total_emp * 100, 1) if total_emp else 0,
            "absent_pct": round(absent / total_emp * 100, 1) if total_emp else 0,
            "late": late_cnt, "inside": inside_cnt, "completed": completed,
            "missing_out": missing_out if not is_today else inside_cnt,
            "early": early_cnt,
            "total_punches": total_punches,
            "avg_punches": round(total_punches / present, 2) if present else 0,
            "total_working": _fmt_hm(total_work),
            "avg_working": _fmt_hm(total_work / present) if present else "00h 00m",
            "total_overtime": _fmt_hm(total_ot),
            "avg_first": _avg_time(first_mins), "avg_last": _avg_time(last_mins),
            "earliest": (min(table, key=lambda e: e["first_punch"])["first_punch"]
                         if table else "--:--"),
            "latest": (max(table, key=lambda e: e["last_punch"])["last_punch"]
                       if table else "--:--"),
            "live": inside_cnt,
        },
        "first_punch": {**first_buckets, "avg": _avg_time(first_mins)},
        "work_buckets": work_buckets,
        "timeline": timeline,
        "table": table,
        "summary": {
            "present": present, "absent": absent, "late": late_cnt,
            "inside": inside_cnt, "completed": completed, "missing_out": missing_out,
            "early": early_cnt, "total_punches": total_punches,
            "total_working": _fmt_hm(total_work), "total_overtime": _fmt_hm(total_ot),
            "avg_working": _fmt_hm(total_work / present) if present else "00h 00m",
        },
        "departments": sorted(dept_stats.values(), key=lambda d: d["present"], reverse=True),
        "shifts": sorted(shift_stats.values(), key=lambda s: s["name"]),
        "top10": [{"name": e["name"], "department": e["department"],
                   "working": e["working"], "overtime": e["overtime"]} for e in top10],
        "late_list": late_list,
        "inside_list": inside_list,
        "feed": feed[:18],
        "axis": {"start": TIMELINE_START_H, "end": TIMELINE_END_H},
    }


def employee_detail(emp_id, date_str):
    """Detailed record for the employee popup: today + weekly/monthly stats."""
    now_dt = now_local()
    is_today = (date_str == now_dt.strftime("%Y-%m-%d"))
    d = datetime.strptime(date_str, "%Y-%m-%d")
    week_start = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
    month_start = d.replace(day=1).strftime("%Y-%m-%d")

    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM punches WHERE emp_id=? AND punch_date=? ORDER BY punch_dt",
            (emp_id, date_str)).fetchall()
        week_days = conn.execute(
            "SELECT COUNT(DISTINCT punch_date) c FROM punches WHERE emp_id=? AND punch_date>=? AND punch_date<=?",
            (emp_id, week_start, date_str)).fetchone()["c"]
        month_days = conn.execute(
            "SELECT COUNT(DISTINCT punch_date) c FROM punches WHERE emp_id=? AND punch_date>=? AND punch_date<=?",
            (emp_id, month_start, date_str)).fetchone()["c"]

    a = analyze_employee(rows, emp_id, is_today, now_dt)
    for k in ("_first_min", "_last_min"):
        a.pop(k, None)
    meta = emp_meta(emp_id)
    a.update({
        "emp_id": emp_id, "name": EMPLOYEES.get(emp_id, emp_id),
        "initials": initials(EMPLOYEES.get(emp_id, emp_id)),
        "department": meta["department"], "designation": meta["designation"],
        "shift": meta["shift"], "shift_time": f"{SHIFTS[meta['shift']]['start']} - {SHIFTS[meta['shift']]['end']}",
        "week_present": week_days, "week_pct": round(week_days / 6 * 100),
        "month_present": month_days, "month_pct": round(month_days / 26 * 100),
    })
    return a


def calendar_data(emp_id, month_str):
    """Per-day attendance for a month. emp_id optional (org-wide if blank)."""
    start = datetime.strptime(month_str + "-01", "%Y-%m-%d")
    nxt = (start.replace(day=28) + timedelta(days=7)).replace(day=1)
    end = (nxt - timedelta(days=1)).strftime("%Y-%m-%d")
    with closing(db()) as conn:
        if emp_id:
            rows = conn.execute(
                "SELECT punch_date, COUNT(*) c FROM punches WHERE emp_id=? "
                "AND punch_date>=? AND punch_date<=? GROUP BY punch_date",
                (emp_id, start.strftime("%Y-%m-%d"), end)).fetchall()
        else:
            rows = conn.execute(
                "SELECT punch_date, COUNT(DISTINCT emp_id) c FROM punches WHERE "
                "punch_date>=? AND punch_date<=? GROUP BY punch_date",
                (start.strftime("%Y-%m-%d"), end)).fetchall()
    return {"month": month_str, "total_employees": len(EMPLOYEES),
            "days": [{"date": r["punch_date"], "count": r["c"]} for r in rows]}


# ── ADMS device protocol ─────────────────────────────────────────────────────
def handshake_lines(sn):
    # No TimeZone line -> server never resets the device clock.
    return "\r\n".join([
        f"GET OPTION FROM: {sn}",
        "Stamp=9999", "OpStamp=9999", "ErrorDelay=30", "Delay=10",
        "TransTimes=00:00;14:05", "TransInterval=1",
        "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP",
        "Realtime=1", "Encrypt=None",
        "ATTLOGStamp=9999", "OPERLOGStamp=9999", "ATTPHOTOStamp=9999", "",
    ])


def parse_attlog(body, sn):
    """ZKTeco ATTLOG rows: PIN <TAB> 'YYYY-MM-DD HH:MM:SS' <TAB> status <TAB> ..."""
    count = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if "\t" in line:
                parts = line.split("\t")
                emp_id = parts[0].strip()
                dt = parts[1].strip()
                status = parts[2].strip() if len(parts) > 2 else ""
            else:
                parts = line.split()
                emp_id = parts[0]
                dt = f"{parts[1]} {parts[2]}" if len(parts) > 2 else parts[1]
                status = parts[3] if len(parts) > 3 else ""
            if not emp_id or len(dt) < 10:
                continue
            if store_punch(emp_id, dt, sn, status):
                count += 1
        except (IndexError, ValueError) as e:
            print("  ERROR parsing:", repr(line), "->", e)
    return count


# ── HTTP server ──────────────────────────────────────────────────────────────
class MyServer(BaseHTTPRequestHandler):

    def _send(self, body, status=200, content_type="text/plain"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _today(self):
        q = self._query()
        return q.get("date", [now_local().strftime("%Y-%m-%d")])[0]

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/dashboard"):
            try:
                with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
                    return self._send(f.read(), content_type="text/html")
            except OSError:
                return self._send("dashboard.html not found", status=500)

        if path == "/api/dashboard":
            return self._send(json.dumps(build_dashboard(self._today())),
                              content_type="application/json")

        # Force the device to re-upload all stored attendance logs.
        if path == "/api/pull-logs":
            q = self._query()
            sn = q.get("sn", [LAST_SN])[0] or LAST_SN
            if not sn:
                return self._send(
                    json.dumps({"ok": False, "error": "No device has connected yet."}),
                    status=400, content_type="application/json")
            start = q.get("from", ["2020-01-01"])[0]
            end = q.get("to", [now_local().strftime("%Y-%m-%d")])[0]
            cid = queue_pull_attlog(sn, start, end)
            print(f"\n[PULL] queued ATTLOG re-import for SN={sn} "
                  f"({start} .. {end}), cmd C:{cid}")
            return self._send(json.dumps({
                "ok": True, "sn": sn, "cmd_id": cid, "from": start, "to": end,
                "note": "Command queued. The device will upload on its next poll "
                        "(usually within ~1 minute)."
            }), content_type="application/json")

        if path.startswith("/api/employee/"):
            emp_id = path.rsplit("/", 1)[-1]
            return self._send(json.dumps(employee_detail(emp_id, self._today())),
                              content_type="application/json")

        if path == "/api/calendar":
            q = self._query()
            emp_id = q.get("emp", [""])[0]
            month = q.get("month", [now_local().strftime("%Y-%m")])[0]
            return self._send(json.dumps(calendar_data(emp_id, month)),
                              content_type="application/json")

        # ADMS device init handshake
        if path in ("/iclock/cdata", "/iclock/cdata.aspx"):
            if self._query().get("options", [""])[0] == "all":
                sn = self._query().get("SN", ["?"])[0]
                remember_sn(sn)
                print(f"\n[INIT] handshake SN={sn}")
                return self._send(handshake_lines(sn))
            return self._send("OK")

        # Device command poll -- deliver any queued commands (e.g. log re-import).
        if path in ("/iclock/getrequest", "/iclock/getrequest.aspx"):
            sn = self._query().get("SN", ["?"])[0]
            remember_sn(sn)
            cmds = pop_commands(sn)
            if cmds:
                print(f"[CMD] -> SN={sn}: {len(cmds)} command(s)")
                return self._send("\r\n".join(cmds) + "\r\n")
            return self._send("OK")

        self._send("Not Found", status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        q = self._query()

        if path in ("/iclock/devicecmd", "/iclock/devicecmd.aspx"):
            return self._send("OK")

        if path not in ("/iclock/cdata", "/iclock/cdata.aspx"):
            return self._send("Not Found", status=404)

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        table = q.get("table", [""])[0].upper()
        sn = q.get("SN", ["?"])[0]
        remember_sn(sn)

        print(f"\n===== CDATA POST SN={sn} table={table or '(none)'} =====")
        if body.strip():
            print(body.rstrip())

        if table and table != "ATTLOG":
            return self._send("OK")
        parse_attlog(body, sn)
        self._send("OK")

    def log_message(self, fmt, *args):
        return


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MyServer)
    print(f"Dashboard:  http://<SERVER_IP>:{PORT}/dashboard")
    print(f"Device URL: http://<SERVER_IP>:{PORT}/iclock/cdata")
    print(f"ADMS server running on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
