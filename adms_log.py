# test_biometric.py

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from datetime import datetime
import json
import os

PORT = 5000

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

attendance = {}

LOG_FILE = "attendance_logs.json"

def load_logs():
global attendance
if os.path.exists(LOG_FILE):
with open(LOG_FILE, "r") as f:
attendance = json.load(f)

def save_logs():
with open(LOG_FILE, "w") as f:
json.dump(attendance, f, indent=4)

load_logs()

class MyServer(BaseHTTPRequestHandler):

```
def do_GET(self):

    path = urlparse(self.path).path

    if path == "/dashboard":
        return self.dashboard()

    if path.startswith("/api/attendance"):
        return self.api_attendance()

    if path in ["/iclock/getrequest", "/iclock/getrequest.aspx"]:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
        return

    self.send_response(404)
    self.end_headers()

def do_POST(self):

    path = urlparse(self.path).path

    if path not in ["/iclock/cdata", "/iclock/cdata.aspx"]:
        self.send_response(404)
        self.end_headers()
        return

    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode(
        "utf-8", errors="ignore"
    )

    print("\n========== ATTLOG RECEIVED ==========\n")
    print(body)

    for line in body.splitlines():

        line = line.strip()

        if not line:
            continue

        try:

            parts = line.split()

            emp_id = parts[0]
            date = parts[1]
            punch_time = parts[2]

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

            save_logs()

            print(
                f"{emp_name} | {date} | {punch_time}"
            )

        except Exception as e:
            print("ERROR:", e)

    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"OK")

def api_attendance(self):

    self.send_response(200)
    self.send_header(
        "Content-Type",
        "application/json"
    )
    self.end_headers()

    self.wfile.write(
        json.dumps(
            attendance,
            indent=4
        ).encode()
    )

def dashboard(self):

    rows = ""

    for item in attendance.values():

        rows += f"""
        <tr>
            <td>{item['employee_id']}</td>
            <td>{item['employee_name']}</td>
            <td>{item['date']}</td>
            <td>{item['check_in']}</td>
            <td>{item['check_out'] or '-'}</td>
            <td>{item['punch_count']}</td>
        </tr>
        """

    html = f"""
    <html>
    <head>
        <title>Attendance Dashboard</title>
        <meta http-equiv="refresh" content="10">
        <style>
        body {{
            font-family: Arial;
            margin:40px;
        }}
        table {{
            width:100%;
            border-collapse:collapse;
        }}
        th,td {{
            border:1px solid #ddd;
            padding:10px;
        }}
        th {{
            background:#0d6efd;
            color:white;
        }}
        </style>
    </head>
    <body>

    <h2>Biometric Attendance Dashboard</h2>

    <p>
        Last Refresh:
        {datetime.now()}
    </p>

    <table>
    <tr>
        <th>Emp ID</th>
        <th>Name</th>
        <th>Date</th>
        <th>Check In</th>
        <th>Check Out</th>
        <th>Punches</th>
    </tr>

    {rows}

    </table>

    </body>
    </html>
    """

    self.send_response(200)
    self.send_header(
        "Content-Type",
        "text/html"
    )
    self.end_headers()

    self.wfile.write(html.encode())

def log_message(self, format, *args):
    return
```

server = HTTPServer(
("0.0.0.0", PORT),
MyServer
)

print(
f"ADMS Server Running on Port {PORT}"
)

print(
f"Dashboard: http://SERVER_IP:{PORT}/dashboard"
)

server.serve_forever()
