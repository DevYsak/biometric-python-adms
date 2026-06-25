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
from datetime import datetime, timedelta
from contextlib import closing
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
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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


def analyze_employee(rows):
    """rows: sqlite Rows for one employee+day, ordered ascending by punch_dt."""
    times = [datetime.strptime(r["punch_dt"], "%Y-%m-%d %H:%M:%S") for r in rows]
    n = len(times)
    events = []
    for i, t in enumerate(times):
        events.append({
            "time": t.strftime("%H:%M:%S"),
            "type": "IN" if i % 2 == 0 else "OUT",
            "pct": _to_pct(t),
            "first": i == 0,
            "last": i == n - 1,
        })

    work_sec = brk_sec = 0
    i = 0
    while i + 1 < n:
        work_sec += (times[i + 1] - times[i]).total_seconds()
        if i + 2 < n:
            brk_sec += (times[i + 2] - times[i + 1]).total_seconds()
        i += 2

    inside = (n % 2 == 1)
    work_min = work_sec / 60
    brk_min = brk_sec / 60
    overtime_min = max(0, work_min - STANDARD_DAY_MIN)
    status = "Absent" if n == 0 else ("Present (Incomplete)" if inside else "Present")

    return {
        "events": events,
        "first_punch": times[0].strftime("%H:%M:%S") if n else None,
        "last_punch": times[-1].strftime("%H:%M:%S") if n else None,
        "_first_dt": times[0] if n else None,
        "punch_count": n,
        "working_min": round(work_min),
        "working": _fmt_hm(work_min),
        "break_min": round(brk_min),
        "break": _fmt_hm(brk_min),
        "overtime_min": round(overtime_min),
        "overtime": _fmt_hm(overtime_min),
        "inside": inside,
        "status": status,
    }


def build_dashboard(date_str):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM punches WHERE punch_date=? ORDER BY emp_id, punch_dt",
            (date_str,),
        ).fetchall()
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        active_ids = {
            r["emp_id"] for r in conn.execute(
                "SELECT DISTINCT emp_id FROM punches WHERE punch_date>=?", (cutoff,)
            ).fetchall()
        }

    by_emp = {}
    for r in rows:
        by_emp.setdefault(r["emp_id"], []).append(r)

    table, timeline, feed = [], [], []
    total_punches = total_work = 0
    present = incomplete = on_duty = 0
    first_buckets = {"before9": 0, "h9_10": 0, "h10_11": 0, "after11": 0}
    work_buckets = [0, 0, 0, 0, 0]
    first_punch_minutes = []

    for emp_id, erows in by_emp.items():
        a = analyze_employee(erows)
        name = erows[0]["emp_name"]
        present += 1
        total_punches += a["punch_count"]
        total_work += a["working_min"]
        if a["inside"]:
            incomplete += 1
            on_duty += 1

        fdt = a["_first_dt"]
        first_punch_minutes.append(fdt.hour * 60 + fdt.minute)
        if fdt.hour < 9:
            first_buckets["before9"] += 1
        elif fdt.hour < 10:
            first_buckets["h9_10"] += 1
        elif fdt.hour < 11:
            first_buckets["h10_11"] += 1
        else:
            first_buckets["after11"] += 1

        wh = a["working_min"] / 60
        work_buckets[4 if wh >= 8 else int(wh // 2)] += 1

        entry = {"emp_id": emp_id, "name": name, **a}
        entry.pop("_first_dt", None)
        table.append(entry)
        timeline.append({
            "emp_id": emp_id, "name": name,
            "first_punch": a["first_punch"], "last_punch": a["last_punch"],
            "inside": a["inside"], "events": a["events"],
        })
        for ev in a["events"]:
            feed.append({"time": ev["time"], "name": name, "type": ev["type"]})

    table.sort(key=lambda e: e["first_punch"] or "99")
    timeline.sort(key=lambda e: e["first_punch"] or "99")
    feed.sort(key=lambda f: f["time"], reverse=True)

    total_emp = len(EMPLOYEES)
    absent = max(total_emp - present, 0)
    avg_first = (
        datetime(2000, 1, 1)
        + timedelta(minutes=sum(first_punch_minutes) / len(first_punch_minutes))
    ).strftime("%H:%M") if first_punch_minutes else "--:--"
    top5 = sorted(table, key=lambda e: e["working_min"], reverse=True)[:5]

    return {
        "now": datetime.now().strftime("%H:%M:%S"),
        "date": date_str,
        "server_today": datetime.now().strftime("%Y-%m-%d"),
        "date_label": datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y"),
        "kpis": {
            "total_employees": total_emp,
            "active": len(active_ids) or present,
            "present": present,
            "absent": absent,
            "present_pct": round(present / total_emp * 100, 1) if total_emp else 0,
            "absent_pct": round(absent / total_emp * 100, 1) if total_emp else 0,
            "total_punches": total_punches,
            "avg_punches": round(total_punches / present, 2) if present else 0,
            "total_working": _fmt_hm(total_work),
            "avg_working": _fmt_hm(total_work / present) if present else "00h 00m",
            "on_duty": on_duty,
        },
        "first_punch": {**first_buckets, "avg": avg_first},
        "work_buckets": work_buckets,
        "timeline": timeline,
        "table": table,
        "summary": {
            "present": present, "absent": absent, "incomplete": incomplete,
            "total_punches": total_punches, "total_working": _fmt_hm(total_work),
            "avg_working": _fmt_hm(total_work / present) if present else "00h 00m",
        },
        "top5": [{"name": e["name"], "working": e["working"],
                  "overtime": e["overtime"]} for e in top5],
        "feed": feed[:15],
        "axis": {"start": TIMELINE_START_H, "end": TIMELINE_END_H},
    }


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
        return q.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]

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
            end = q.get("to", [datetime.now().strftime("%Y-%m-%d")])[0]
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
            with closing(db()) as conn:
                rows = conn.execute(
                    "SELECT * FROM punches WHERE emp_id=? AND punch_date=? ORDER BY punch_dt",
                    (emp_id, self._today()),
                ).fetchall()
            a = analyze_employee(rows)
            a.pop("_first_dt", None)
            a.update({"emp_id": emp_id, "name": EMPLOYEES.get(emp_id, emp_id)})
            return self._send(json.dumps(a), content_type="application/json")

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
