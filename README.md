# Employee Smart Attendance System

An AI-assisted employee attendance and activity monitoring system built with
Python, Flask, OpenCV, FaceNet, MediaPipe and YOLOv8.

The project is split into two Flask services:

- **Frontend service (`app.py`, port `5000`)**: login, dashboard, employee
  management, session records and report pages.
- **AI/backend service (`main.py`, port `5001`)**: camera streaming, face
  enrollment and recognition, activity detection, session tracking and CSV/PDF
  report generation.

## Features

- Employee enrollment with front, left and right face images
- Face recognition using MTCNN and FaceNet embeddings
- Live camera stream with YOLOv8 object detection
- Activity tracking for working, eyes closed, head down, eating and drinking
- Configurable employee monitoring sessions
- Attendance/activity reports with CSV and PDF export
- Session video storage and downloadable reports
- SQLite persistence for employee, attendance and activity data

## Requirements

- Windows 10/11
- Python 3.10 or 3.11 recommended
- A working webcam
- Git (optional, for source control)
- `wkhtmltopdf` is optional and is required only for PDF export through
  `pdfkit`; without it, the application can still provide printable HTML.

## Installation

From the project directory, create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks script activation, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Configuration

The frontend reads these optional environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `FLASK_SECRET` | Flask session signing key | `dev-secret` |
| `ADMIN_EMAIL` | Dashboard login email | `admin@gmail.com` |
| `ADMIN_PASSWORD` | Dashboard login password | `admin@123` |
| `ADMIN_NAME` | Display name for the administrator | `Admin` |

For anything other than local development, set a strong `FLASK_SECRET` and
change the administrator credentials before starting the application.

Example for the current PowerShell session:

```powershell
$env:FLASK_SECRET = "replace-with-a-long-random-value"
$env:ADMIN_EMAIL = "admin@example.com"
$env:ADMIN_PASSWORD = "replace-with-a-strong-password"
```

## Running the application

Start both services in separate terminals. Start the backend first:

**Terminal 1 - AI/backend service**

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

**Terminal 2 - frontend service**

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Alternatively, on Windows, run `run.bat` after activating the environment.

Open the dashboard at <http://127.0.0.1:5000>. The frontend communicates with
the AI/backend service at <http://127.0.0.1:5001>.

## Service endpoints

### Frontend (`5000`)

- `/` - login page
- `/dashboard` - dashboard
- `/employees` - employee management
- `/reports` - report filters and downloads
- `/logs` - activity logs
- `/api/sessions` - recorded monitoring sessions

### AI/backend (`5001`)

- `/video_feed` - MJPEG camera stream
- `/get_employees` - list enrolled employees
- `/enroll` - enroll an employee with face images
- `/start_stream` and `/stop_stream` - camera controls
- `/session/configure`, `/session/status`, `/session/cancel` - session controls
- `/report` and `/report_download` - report data and downloads
- `/logs` and `/save_log_pdf` - activity log access and PDF export

## Project structure

```text
app.py                 Frontend Flask application
main.py                AI/backend Flask application
requirements.txt       Python dependencies
run.bat                Windows launcher
yolov8n.pt             YOLOv8 nano model
templates/             Jinja2 HTML templates
static/css/            Frontend styles
static/js/             Frontend JavaScript
```

Runtime databases, uploaded employee images, recorded videos, generated
reports and logs are intentionally excluded from Git because they may contain
personal or machine-specific data. They are created automatically when the
services run.

## Troubleshooting

- If the frontend shows no employees, confirm that `main.py` is running on
  port `5001`.
- If the camera cannot start, confirm that the webcam is available and not
  being used by another application.
- Large ML dependencies may take time to install and may require a compatible
  Python/PyTorch environment.
- PDF export requires `wkhtmltopdf` installed and available to `pdfkit`.

## License

No license has been specified yet. Add a license before distributing the
project publicly.
