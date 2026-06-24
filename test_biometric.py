from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from datetime import datetime

# EMPLOYEE MASTER
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
    "36": "Sudhanshu"
}

# STORE IN/OUT  { "emp_id_date": { in_time, out_time } }
attendance = {}


class MyServer(BaseHTTPRequestHandler):

    # ------------------------------------------------------------------
    # GET  — device polls for commands + browser dashboard
    # ------------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path

        # Device polls for pending commands → just reply OK
        if path in ('/iclock/getrequest', '/iclock/getrequest.aspx'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK\r\n')
            return

        # Live attendance dashboard
        if path == '/dashboard':
            self._serve_dashboard()
            return

        # Default 404
        self.send_response(404)
        self.end_headers()

    # ------------------------------------------------------------------
    # POST — device pushes attendance logs
    # ------------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        data = post_data.decode('utf-8', errors='ignore')

        if path not in ('/iclock/cdata', '/iclock/cdata.aspx'):
            self.send_response(404)
            self.end_headers()
            return

        print("\n========== BIOMETRIC CONNECTED ==========\n")

        for line in data.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split()
                # ATTLOG: PIN  DATE  TIME  verify inout ...
                emp_id   = parts[0]
                date     = parts[1]
                time_val = parts[2]
                emp_name = employees.get(emp_id, "Unknown")
                key      = f"{emp_id}_{date}"

                if key not in attendance:
                    attendance[key] = {"in_time": time_val, "out_time": ""}
                    in_time  = time_val
                    out_time = ""
                else:
                    attendance[key]["out_time"] = time_val
                    in_time  = attendance[key]["in_time"]
                    out_time = time_val

                print(f"""
====================================
EMP ID     : {emp_id}
NAME       : {emp_name}
DATE       : {date}
IN TIME    : {in_time}
OUT TIME   : {out_time}
====================================
""")
            except Exception as e:
                print("ERROR:", e)

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    # ------------------------------------------------------------------
    # Dashboard HTML
    # ------------------------------------------------------------------
    def _serve_dashboard(self):
        today = datetime.now().strftime('%Y-%m-%d')

        rows = []
        for key, rec in sorted(attendance.items()):
            emp_id, date = key.rsplit('_', 1)
            name     = employees.get(emp_id, "Unknown")
            in_time  = rec['in_time']
            out_time = rec['out_time'] or '—'

            # Highlight today's records
            highlight = ' style="background:#e6f4ea;"' if date == today else ''
            rows.append(
                f'<tr{highlight}>'
                f'<td>{emp_id}</td><td>{name}</td><td>{date}</td>'
                f'<td>{in_time}</td><td>{out_time}</td>'
                f'</tr>'
            )

        rows_html = '\n'.join(rows) if rows else '<tr><td colspan="5">No records yet</td></tr>'

        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>Biometric Attendance</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 24px; background: #f5f5f5; }}
    h1   {{ color: #333; }}
    table{{ border-collapse: collapse; width: 100%; background: #fff;
            box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
    th   {{ background: #1a73e8; color: #fff; padding: 10px 14px; text-align: left; }}
    td   {{ padding: 9px 14px; border-bottom: 1px solid #eee; }}
    tr:hover td {{ background: #f0f7ff; }}
    .ts  {{ font-size: .85em; color: #888; margin-bottom: 12px; }}
  </style>
</head>
<body>
  <h1>Biometric Attendance</h1>
  <p class="ts">Auto-refreshes every 15 s &nbsp;|&nbsp; {datetime.now().strftime('%d %b %Y %H:%M:%S')}</p>
  <table>
    <thead><tr>
      <th>Emp ID</th><th>Name</th><th>Date</th><th>In Time</th><th>Out Time</th>
    </tr></thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>"""

        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


server = HTTPServer(('0.0.0.0', 5000), MyServer)
print("Attendance Server Running On Port 5000")
print("Dashboard: http://127.0.0.1:5000/dashboard")
server.serve_forever()
