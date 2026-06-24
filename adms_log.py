#!/usr/bin/env python3
"""
Standalone ZKTeco / AIFACE ADMS (push protocol) server.

Listens for the iclock push protocol used by ZKTeco-style biometric devices
(AIFACE-MAGNUM, ZMM510, etc.) and records attendance punches.

Endpoints implemented:
  GET  /iclock/cdata        ?SN=..&options=all   -> device init handshake (CRITICAL)
  POST /iclock/cdata        ?SN=..&table=ATTLOG  -> attendance punch upload
  GET  /iclock/getrequest   ?SN=..               -> command poll (returns OK)
  POST /iclock/devicecmd                          -> command ack (returns OK)
  GET  /dashboard                                 -> HTML attendance dashboard
  GET  /api/attendance                            -> JSON of all attendance

Why the handshake matters:
  On startup the device calls GET /iclock/cdata?options=all. The server MUST
  reply with a "GET OPTION" config block containing Realtime=1 and
  ATTLOGStamp=9999, otherwise the device will NOT push attendance logs.
  Returning 404 here is the usual cause of "device active but no punches".

Stdlib only -- no pip install required. Run with: python3 adms_log.py
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import json
import os
import sys

# Port priority: command-line arg  >  PORT env var  >  default 5000.
#   python3 adms_log.py 80        -> listen on port 80 (needs sudo)
#   PORT=80 python3 adms_log.py   -> same via env var
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5000))

# EMPLOYEE MASTER (device PIN -> name)
employees = {
    "1": "EMAD",
    "2": "NIKITA",
    "3": "NICK",
    "4": "RUSTOM",
    "5": "MAZHAR",
    "6": "MEHUL",
    "7": "Carol",
    "8": "Hasan Mirza",
    "9": "Esha",
    "10": "Ankita",
    "11": "Shivani",
    "12": "Sakshi",
    "13": "Walid",
    "14": "Gurmeet",
    "15": "ABDULBASIT",
    "16": "Mayuresh",
    "17": "Yogesh",
    "18": "Pratish",
    "19": "Shivendra",
    "20": "Saad",
    "21": "Digambar",
    "22": "Shradha",
    "23": "Abhishek Bhoir",
    "24": "Altamash",
    "25": "Reeba",
    "26": "Sunita",
    "27": "Suhail khan",
    "28": "Gayatri",
    "29": "Sunil",
    "30": "Surekha",
    "31": "Kajal",
    "32": "Zaheer",
    "33": "Kashif",
    "34": "Chinmay",
    "35": "Atif",
    "36": "Sudhanshu",
}

attendance = {}

LOG_FILE = "attendance_logs.json"


def load_logs():
    global attendance
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                attendance = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print("WARN: could not load log file:", e)
            attendance = {}


def save_logs():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(attendance, f, indent=4)


load_logs()


def record_punch(emp_id, date, punch_time):
    """Store one punch. First punch of the day = check_in, later ones move check_out."""
    emp_name = employees.get(emp_id, "Unknown")
    key = f"{emp_id}_{date}"

    if key not in attendance:
        attendance[key] = {
            "employee_id": emp_id,
            "employee_name": emp_name,
            "date": date,
            "check_in": punch_time,
            "check_out": "",
            "last_punch": punch_time,
            "punch_count": 1,
        }
    else:
        attendance[key]["check_out"] = punch_time
        attendance[key]["last_punch"] = punch_time
        attendance[key]["punch_count"] += 1

    print(f"  PUNCH  {emp_name:<16} | {date} | {punch_time}")


# Dashboard HTML template. Placeholders (__TOKEN__) are filled in dashboard_html().
# Plain string (not an f-string) so CSS/JS braces stay literal.
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Biometric Attendance Dashboard</title>
<meta http-equiv="refresh" content="15">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #0f172a; color: #e2e8f0; padding: 28px;
  }
  .head {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px; margin-bottom: 24px;
  }
  .head h1 { font-size: 22px; font-weight: 700; }
  .head h1 span { color: #38bdf8; }
  .head .meta { font-size: 13px; color: #94a3b8; text-align: right; }
  .live { color: #34d399; font-weight: 600; }
  .live::before {
    content: ""; display: inline-block; width: 8px; height: 8px;
    background: #34d399; border-radius: 50%; margin-right: 6px;
    animation: pulse 1.4s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }

  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
  .card {
    background: #1e293b; border-radius: 14px; padding: 20px;
    border: 1px solid #334155; box-shadow: 0 4px 14px rgba(0,0,0,.25);
  }
  .card .label { font-size: 13px; color: #94a3b8; margin-bottom: 6px; }
  .card .value { font-size: 30px; font-weight: 700; }
  .card.emp .value { color: #38bdf8; }
  .card.present .value { color: #34d399; }
  .card.absent .value { color: #f87171; }
  .card.punch .value { color: #fbbf24; }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .panel {
    background: #1e293b; border-radius: 14px; padding: 20px;
    border: 1px solid #334155;
  }
  .panel h2 { font-size: 15px; margin-bottom: 14px; color: #cbd5e1; }
  .chart-wrap { position: relative; height: 260px; }

  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 11px 14px; text-align: left; border-bottom: 1px solid #334155; }
  th { color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  tbody tr:hover { background: #243349; }
  td.id { color: #64748b; }
  td.name { font-weight: 600; color: #f1f5f9; }
  tr.is-absent td.name { color: #94a3b8; font-weight: 500; }
  .empty { text-align: center; color: #64748b; padding: 26px; }
  .badge {
    background: #334155; color: #e2e8f0; padding: 2px 10px;
    border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  .pill { padding: 3px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .pill-in { background: rgba(52,211,153,.15); color: #34d399; }
  .pill-out { background: rgba(248,113,113,.12); color: #f87171; }
  .scroll { max-height: 460px; overflow-y: auto; }
  @media (max-width: 900px) {
    .cards { grid-template-columns: repeat(2, 1fr); }
    .grid2 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
  <div class="head">
    <h1>Biometric <span>Attendance</span> Dashboard</h1>
    <div class="meta">
      <div class="live">LIVE</div>
      <div>Date: __TODAY__</div>
      <div>Last refresh: __REFRESHED__</div>
    </div>
  </div>

  <div class="cards">
    <div class="card emp"><div class="label">Total Employees</div><div class="value">__TOTAL_EMP__</div></div>
    <div class="card present"><div class="label">Present Today</div><div class="value">__PRESENT__</div></div>
    <div class="card absent"><div class="label">Absent Today</div><div class="value">__ABSENT__</div></div>
    <div class="card punch"><div class="label">Punches Today</div><div class="value">__PUNCHES__</div></div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>Attendance Overview</h2>
      <div class="chart-wrap"><canvas id="donut"></canvas></div>
    </div>
    <div class="panel">
      <h2>Punches per Employee (Today)</h2>
      <div class="chart-wrap"><canvas id="bar"></canvas></div>
    </div>
  </div>

  <div class="panel" style="margin-bottom:24px">
    <h2>Today's Attendance</h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>ID</th><th>Name</th><th>Check In</th><th>Check Out</th>
          <th>Last Punch</th><th>Punches</th><th>Status</th>
        </tr></thead>
        <tbody>__TODAY_ROWS__</tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>Employee List</h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>ID</th><th>Name</th><th>Check In</th><th>Last Punch</th><th>Status</th>
        </tr></thead>
        <tbody>__ROSTER__</tbody>
      </table>
    </div>
  </div>

<script>
  const DATA = __CHART_DATA__;
  Chart.defaults.color = "#94a3b8";
  Chart.defaults.borderColor = "#334155";

  new Chart(document.getElementById("donut"), {
    type: "doughnut",
    data: {
      labels: ["Present", "Absent"],
      datasets: [{ data: [DATA.present, DATA.absent],
        backgroundColor: ["#34d399", "#f87171"], borderWidth: 0 }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } }, cutout: "62%" }
  });

  new Chart(document.getElementById("bar"), {
    type: "bar",
    data: {
      labels: DATA.labels,
      datasets: [{ label: "Punches", data: DATA.counts,
        backgroundColor: "#38bdf8", borderRadius: 6 }]
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
  });
</script>
</body>
</html>"""


class MyServer(BaseHTTPRequestHandler):

    # ── helpers ──────────────────────────────────────────────────────────────

    def _send(self, body, status=200, content_type="text/plain"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(body)

    def _device_init_response(self):
        """The GET OPTION block that instructs the device to push in real time."""
        lines = [
            "GET OPTION FROM: %s" % self._query().get("SN", [""])[0],
            "Stamp=9999",
            "OpStamp=9999",
            "ErrorDelay=30",
            "Delay=10",
            "TransTimes=00:00;14:05",
            "TransInterval=1",
            "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP",
            # NOTE: TimeZone is intentionally NOT sent. Some ZKTeco devices reset
            # their own clock to match it, and if the server OS runs on UTC the
            # device time ends up wrong. Leaving it out means the server never
            # touches the device clock -- set the device time manually on-device.
            "Realtime=1",
            "Encrypt=None",
            "ATTLOGStamp=9999",
            "OPERLOGStamp=9999",
            "ATTPHOTOStamp=9999",
            "",
        ]
        return "\r\n".join(lines)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        query = self._query()

        if path == "/dashboard":
            return self._send(self.dashboard_html(), content_type="text/html")

        if path.startswith("/api/attendance"):
            return self._send(
                json.dumps(attendance, indent=4), content_type="application/json"
            )

        # Device initialisation handshake -- THIS is what enables punch pushing.
        if path in ("/iclock/cdata", "/iclock/cdata.aspx"):
            if query.get("options", [""])[0] == "all":
                sn = query.get("SN", ["?"])[0]
                print(f"\n[INIT] device handshake SN={sn} -> sending GET OPTION")
                return self._send(self._device_init_response())
            # Some firmware probes cdata with GET and no options; just ack.
            return self._send("OK")

        # Command poll
        if path in ("/iclock/getrequest", "/iclock/getrequest.aspx"):
            return self._send("OK")

        self._send("Not Found", status=404)

    # ── POST ───────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        query = self._query()

        # Command acknowledgement
        if path in ("/iclock/devicecmd", "/iclock/devicecmd.aspx"):
            return self._send("OK")

        if path not in ("/iclock/cdata", "/iclock/cdata.aspx"):
            return self._send("Not Found", status=404)

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="ignore")

        table = query.get("table", [""])[0].upper()
        sn = query.get("SN", ["?"])[0]

        print(f"\n===== CDATA POST  SN={sn}  table={table or '(none)'} =====")
        if body.strip():
            print(body)

        # Only ATTLOG rows are punches. OPERLOG / USERINFO etc. are acked, not stored.
        if table and table != "ATTLOG":
            print(f"[skip] table={table} acknowledged, not stored")
            return self._send("OK")

        self._parse_attlog(body)
        save_logs()
        self._send("OK")

    def _parse_attlog(self, body):
        """
        ZKTeco ATTLOG rows are tab-separated:
            PIN <TAB> YYYY-MM-DD HH:MM:SS <TAB> status <TAB> verify <TAB> ...
        The datetime field itself contains a space, so we split on tabs first
        and fall back to whitespace splitting for older/space-delimited firmware.
        """
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if "\t" in line:
                    parts = line.split("\t")
                    emp_id = parts[0].strip()
                    dt = parts[1].strip()  # "YYYY-MM-DD HH:MM:SS"
                    date, _, punch_time = dt.partition(" ")
                else:
                    parts = line.split()
                    emp_id = parts[0]
                    date = parts[1]
                    punch_time = parts[2] if len(parts) > 2 else ""

                if not emp_id or not date:
                    print("  [skip] unparseable line:", repr(line))
                    continue

                record_punch(emp_id, date, punch_time)
            except (IndexError, ValueError) as e:
                print("  ERROR parsing line:", repr(line), "->", e)

    # ── dashboard ──────────────────────────────────────────────────────────────

    def dashboard_html(self):
        today = datetime.now().strftime("%Y-%m-%d")

        records = list(attendance.values())
        today_records = [r for r in records if r["date"] == today]

        total_employees = len(employees)
        present_ids = {r["employee_id"] for r in today_records}
        present_today = len(present_ids)
        absent_today = max(total_employees - present_today, 0)
        punches_today = sum(r["punch_count"] for r in today_records)

        # ── today's attendance rows (sorted by check-in) ─────────────────────
        today_sorted = sorted(today_records, key=lambda r: r["check_in"])
        if today_sorted:
            today_rows = ""
            for r in today_sorted:
                out = r["check_out"] or "&mdash;"
                today_rows += (
                    "<tr>"
                    f"<td class='id'>{r['employee_id']}</td>"
                    f"<td class='name'>{r['employee_name']}</td>"
                    f"<td>{r['check_in']}</td>"
                    f"<td>{out}</td>"
                    f"<td>{r['last_punch']}</td>"
                    f"<td><span class='badge'>{r['punch_count']}</span></td>"
                    "<td><span class='pill pill-in'>Present</span></td>"
                    "</tr>"
                )
        else:
            today_rows = ("<tr><td colspan='7' class='empty'>"
                          "No punches recorded today yet.</td></tr>")

        # ── full employee roster (present / absent) ──────────────────────────
        roster = ""
        for emp_id, name in sorted(employees.items(), key=lambda kv: int(kv[0])):
            rec = next((r for r in today_records if r["employee_id"] == emp_id), None)
            if rec:
                roster += (
                    "<tr>"
                    f"<td class='id'>{emp_id}</td>"
                    f"<td class='name'>{name}</td>"
                    f"<td>{rec['check_in']}</td>"
                    f"<td>{rec['last_punch']}</td>"
                    "<td><span class='pill pill-in'>Present</span></td>"
                    "</tr>"
                )
            else:
                roster += (
                    "<tr class='is-absent'>"
                    f"<td class='id'>{emp_id}</td>"
                    f"<td class='name'>{name}</td>"
                    "<td>&mdash;</td><td>&mdash;</td>"
                    "<td><span class='pill pill-out'>Absent</span></td>"
                    "</tr>"
                )

        chart = json.dumps({
            "present": present_today,
            "absent": absent_today,
            "labels": [r["employee_name"] for r in today_sorted],
            "counts": [r["punch_count"] for r in today_sorted],
        })

        refreshed = datetime.now().strftime("%d %b %Y, %H:%M:%S")

        html = DASHBOARD_TEMPLATE
        for token, value in {
            "__TODAY__": today,
            "__REFRESHED__": refreshed,
            "__TOTAL_EMP__": str(total_employees),
            "__PRESENT__": str(present_today),
            "__ABSENT__": str(absent_today),
            "__PUNCHES__": str(punches_today),
            "__TODAY_ROWS__": today_rows,
            "__ROSTER__": roster,
            "__CHART_DATA__": chart,
        }.items():
            html = html.replace(token, value)
        return html

    # Silence default per-request logging (we print our own).
    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MyServer)
    print(f"ADMS Server running on port {PORT}")
    print(f"Dashboard:  http://<SERVER_IP>:{PORT}/dashboard")
    print(f"Device URL: http://<SERVER_IP>:{PORT}/iclock/cdata")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
