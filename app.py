import os
import io
import uuid
import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import requests
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'dev-secret')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'data.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'images')
app.config['VIDEO_FOLDER'] = os.path.join(BASE_DIR, 'static', 'videos')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['VIDEO_FOLDER'], exist_ok=True)

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@gmail.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin@123')
ADMIN_NAME = os.environ.get('ADMIN_NAME', 'Admin')

db = SQLAlchemy(app)


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    department = db.Column(db.String(128))
    contact = db.Column(db.String(128))
    front_img = db.Column(db.String(256))
    left_img = db.Column(db.String(256))
    right_img = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'emp_id': self.emp_id,
            'name': self.name,
            'department': self.department,
            'contact': self.contact,
            'front_img': self.front_img,
            'left_img': self.left_img,
            'right_img': self.right_img,
            'created_at': self.created_at.isoformat()
        }


class ReportEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=True)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    activity_type = db.Column(db.String(64))
    duration_seconds = db.Column(db.Integer, default=0)
    details = db.Column(db.Text)

    employee = db.relationship('Employee', backref='reports')


class SessionRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=True)
    filename = db.Column(db.String(256), nullable=False)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    employee = db.relationship('Employee', backref='sessions')


def _default_report_filters():
    today_str = datetime.date.today().isoformat()
    return {
        "start_date": today_str,
        "end_date": today_str,
        "employee_id": ""
    }


@app.before_request
def create_tables():
    db.create_all()
    # ensure folders exist
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['VIDEO_FOLDER'], exist_ok=True)


@app.before_request
def require_login():
    allowed_endpoints = {'index', 'login', 'logout', 'static'}
    if request.endpoint in allowed_endpoints or request.endpoint is None:
        return
    if session.get('logged_in'):
        return
    return redirect(url_for('index'))


@app.route('/')
def index():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    total_employees = Employee.query.count()
    recent_reports = ReportEntry.query.order_by(ReportEntry.id.desc()).limit(6).all()
    return render_template('dashboard.html', total_employees=total_employees, recent_reports=recent_reports)

@app.route('/employees')
def employees():
    response = requests.get("http://127.0.0.1:5001/get_employees")
    all_emp = response.json()
    return render_template('employee.html', employees=all_emp)

@app.route("/enroll_frontend", methods=["POST"])
def enroll_employee_frontend():
    try:
        # Prepare files for backend
        files = {
            "image1": request.files.get("image1"),
            "image2": request.files.get("image2"),
            "image3": request.files.get("image3"),
        }

        # Prepare text fields
        data = {
            "full_name": request.form.get("full_name"),
            "email": request.form.get("email"),
        }

        # Send request to backend
        res = requests.post('http://127.0.0.1:5001/enroll', files=files, data=data)

        # Decode JSON
        result = res.json()

        if res.status_code == 200 and result.get("status") == "enrolled":
            flash(f"Employee enrolled successfully! ID: {result['employee_id']}", "success")
        else:
            flash(result.get("error", "Enrollment failed"), "danger")

    except Exception as e:
        flash("Server error: " + str(e), "danger")

    return redirect(url_for("employees"))


@app.route('/employees/edit/<int:eid>', methods=['GET', 'POST'])
def edit_employee(eid):
    emp = Employee.query.get_or_404(eid)
    if request.method == 'GET':
        return render_template('employee_edit.html', employee=emp)
    # POST - update
    try:
        name = request.form.get('name')
        emp_id = request.form.get('emp_id')
        department = request.form.get('department')
        contact = request.form.get('contact')

        if not name or not emp_id:
            flash('Name and Employee ID are required', 'danger')
            return redirect(url_for('edit_employee', eid=eid))

        # handle images if provided
        def save_file(fieldname):
            f = request.files.get(fieldname)
            if f and f.filename:
                filename = secure_filename(f.filename)
                path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                f.save(path)
                return url_for('static', filename='images/' + filename)
            return None

        front = save_file('front_img')
        left = save_file('left_img')
        right = save_file('right_img')

        emp.name = name
        emp.emp_id = emp_id
        emp.department = department
        emp.contact = contact
        if front: emp.front_img = front
        if left: emp.left_img = left
        if right: emp.right_img = right

        db.session.commit()
        flash('Employee updated', 'success')
        return redirect(url_for('employees'))
    except Exception as e:
        db.session.rollback()
        flash('Error updating employee: ' + str(e), 'danger')
        return redirect(url_for('employees'))

@app.route('/employees/delete/<int:eid>', methods=['POST'])
def delete_employee(eid):
    try:
        response = requests.post(f"http://localhost:5001/delete_employee/{eid}")
        data = response.json()
        if data.get("success"):
            flash("Employee removed", "success")
        else:
            flash(f"Error deleting employee: {data.get('message')}", "danger")
    except Exception as e:
        flash(f"Error deleting employee: {str(e)}", "danger")
    return redirect(url_for('employees'))


@app.route('/employees')
def get_employees():
    try:
        # Fetch all employees from backend
        response = requests.get("http://localhost:5001/get_employees")
        response.raise_for_status()
        all_emp = response.json()  # should be a list of employee dicts
    except Exception as e:
        print(f"Error fetching employees: {e}")
        all_emp = []  # fallback to empty list

    # Pass employees to the template
    return render_template('employee.html', employees=all_emp)


@app.route('/login', methods=['POST'])
def login():
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    if email != ADMIN_EMAIL.lower() or password != ADMIN_PASSWORD:
        flash('Invalid credentials', 'danger')
        return redirect(url_for('index'))
    session['logged_in'] = True
    session['user_name'] = ADMIN_NAME
    flash('Welcome back', 'success')
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out', 'info')
    return redirect(url_for('index'))


@app.route('/api/sessions', methods=['GET'])
def api_sessions():
    q = SessionRecord.query.order_by(SessionRecord.created_at.desc()).limit(50).all()
    out = []
    for s in q:
        out.append({
            'id': s.id,
            'employee_id': s.employee_id,
            'employee_name': s.employee.name if s.employee else None,
            'filename': s.filename,
            'start_time': s.start_time.isoformat() if s.start_time else None,
            'end_time': s.end_time.isoformat() if s.end_time else None,
            'duration_seconds': s.duration_seconds,
            'created_at': s.created_at.isoformat()
        })
    return jsonify(out)


@app.route('/api/sessions/upload', methods=['POST'])
def api_sessions_upload():
    # expects multipart/form-data with 'video' file and optional metadata fields
    f = request.files.get('video')
    emp_id = request.form.get('employee_id')
    requested_name = request.form.get('filename')
    start_ts = request.form.get('start_time')
    end_ts = request.form.get('end_time')
    duration = request.form.get('duration_seconds')

    if not f:
        return jsonify({'status': 'error', 'message': 'No video file received'}), 400
    try:
        # prefer client-requested filename if provided (sanitize it)
        if requested_name:
            orig = secure_filename(requested_name)
        else:
            orig = secure_filename(f.filename)

        # ensure extension
        if not os.path.splitext(orig)[1]:
            orig = orig + os.path.splitext(f.filename)[1] if f.filename else orig + '.webm'

        # avoid overwrite: if file exists, append a short uuid
        save_name = orig
        save_path = os.path.join(app.config['VIDEO_FOLDER'], save_name)
        if os.path.exists(save_path):
            base, ext = os.path.splitext(orig)
            save_name = f"{base}_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(app.config['VIDEO_FOLDER'], save_name)

        f.save(save_path)

        sr = SessionRecord(filename=save_name,
                           employee_id=int(emp_id) if emp_id else None,
                           start_time=datetime.datetime.fromisoformat(start_ts) if start_ts else None,
                           end_time=datetime.datetime.fromisoformat(end_ts) if end_ts else None,
                           duration_seconds=int(duration) if duration else 0)
        db.session.add(sr)
        db.session.commit()

        return jsonify({'status': 'ok', 'id': sr.id, 'filename': save_name})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sessions/delete', methods=['POST'])
def api_sessions_delete():
    # expects JSON body: { "ids": [1,2,3] }
    data = request.get_json() or {}
    ids = data.get('ids') or []
    if not isinstance(ids, list):
        return jsonify({'status': 'error', 'message': 'ids must be a list'}), 400
    try:
        # convert to ints and filter existing
        ids_int = [int(x) for x in ids if str(x).isdigit()]
        if not ids_int:
            return jsonify({'status': 'ok', 'deleted': 0})
        records = SessionRecord.query.filter(SessionRecord.id.in_(ids_int)).all()
        deleted = 0
        for r in records:
            db.session.delete(r)
            deleted += 1
        db.session.commit()
        return jsonify({'status': 'ok', 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route("/report_download")
def report_download():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    employee_id = request.args.get("employee_id")

    backend_url = "http://localhost:5001/report_download"

    try:
        resp = requests.get(
            backend_url,
            params={
                "start_date": start_date,
                "end_date": end_date,
                "employee_id": employee_id
            },
            timeout=10
        )

        # Backend will return JSON status
        if resp.status_code == 200:
            flash("✅ Report generated and saved successfully!", "success")
        else:
            msg = resp.json().get("error", "Unknown error")
            flash(f"❌ Backend failed: {msg}", "danger")

        return redirect(url_for("reports"))

    except Exception as e:
        flash(f"⚠ Could not reach backend: {str(e)}", "warning")
        return redirect(url_for("reports"))

@app.route('/reports', methods=['GET', 'POST'])
def reports():
    filters = _default_report_filters()
    selected_employee_id = None

    form_source = request.form if request.method == 'POST' else request.args

    if form_source:
        start = (form_source.get('start_date') or '').strip()
        end = (form_source.get('end_date') or '').strip()
        if start:
            filters['start_date'] = start
        if end:
            filters['end_date'] = end

        employee_raw = (form_source.get('employee_id') or '').strip()
        if employee_raw:
            try:
                selected_employee_id = int(employee_raw)
                filters['employee_id'] = str(selected_employee_id)
            except ValueError:
                flash('Employee selection is invalid.', 'warning')
                selected_employee_id = None
        else:
            filters['employee_id'] = ''

    params = {
        'start_date': filters['start_date'],
        'end_date': filters['end_date']
    }
    if selected_employee_id is not None:
        params['employee_id'] = selected_employee_id

    report_rows = []
    try:
        response = requests.get(
            'http://localhost:5001/get_reports',
            params=params,
            timeout=10
        )
        response.raise_for_status()
        report_rows = response.json().get('reports', [])
    except Exception as exc:
        flash(f"Could not load reports: {exc}", 'danger')
        report_rows = []

    employees = []
    try:
        emp_resp = requests.get('http://localhost:5001/get_employees', timeout=10)
        emp_resp.raise_for_status()
        employees = emp_resp.json()
    except Exception as exc:
        flash(f"Could not load employees: {exc}", 'danger')
        employees = []

    return render_template(
        'report.html',
        reports=report_rows,
        filters=filters,
        employees=employees,
        selected_employee_id=selected_employee_id
    )

@app.route('/logs', methods=['GET', 'POST'])
def logs():
    # Default filters: today
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filters = {
        "start_date": today_str,
        "end_date": today_str
    }

    # Override with POSTed form data
    if request.method == 'POST':
        start = request.form.get('start_date') or today_str
        end = request.form.get('end_date') or today_str
        filters['start_date'] = start
        filters['end_date'] = end

        # Call backend with filters
        response = requests.get(
            f"http://localhost:5001/logs",
            params={"start_date": start, "end_date": end}
        )
        logs = response.json()
    else:
        # GET request: default today
        response = requests.get(
            "http://localhost:5001/logs",
            params={"start_date": today_str, "end_date": today_str}
        )
        logs = response.json()

    return render_template("logs.html", logs=logs, filters=filters)

    #     start = request.form.get('start_date')
    #     end = request.form.get('end_date')
    #     q = ReportEntry.query
    #     if emp_id and emp_id != 'all':
    #         q = q.filter(ReportEntry.employee_id == int(emp_id))
    #     if start:
    @app.route('/reports/download')
    def reports_download():
        filters = _default_report_filters()

        start = (request.args.get('start_date') or filters['start_date']).strip()
        end = (request.args.get('end_date') or filters['end_date']).strip()
        employee_raw = (request.args.get('employee_id') or '').strip()

        params = {
            'start_date': start,
            'end_date': end
        }

        redirect_kwargs = {
            'start_date': start,
            'end_date': end
        }

        if employee_raw:
            redirect_kwargs['employee_id'] = employee_raw
            try:
                params['employee_id'] = int(employee_raw)
            except ValueError:
                flash('Employee filter must be a number.', 'warning')
                return redirect(url_for('reports', **redirect_kwargs))
        elif request.args.get('employee_id') is not None:
            redirect_kwargs['employee_id'] = employee_raw

        try:
            backend_resp = requests.get(
                'http://localhost:5001/report',
                params=params,
                timeout=30
            )
            backend_resp.raise_for_status()
        except Exception as exc:
            flash(f"Could not download report: {exc}", 'danger')
            return redirect(url_for('reports', **redirect_kwargs))

        response = Response(backend_resp.content)
        response.headers['Content-Type'] = backend_resp.headers.get('Content-Type', 'text/csv')
        response.headers['Content-Disposition'] = backend_resp.headers.get(
            'Content-Disposition',
            'attachment; filename=report.csv'
        )
        return response

    #         q = q.filter(ReportEntry.date >= datetime.datetime.strptime(start, '%Y-%m-%d').date())
    #     if end:
    #         q = q.filter(ReportEntry.date <= datetime.datetime.strptime(end, '%Y-%m-%d').date())
    #     results = q.order_by(ReportEntry.date.desc()).all()
    #     # prepare simple aggregation for chart (activity types)
    #     agg = {}
    #     for r in results:
    #         agg[r.activity_type or 'unknown'] = agg.get(r.activity_type or 'unknown', 0) + (r.duration_seconds or 0)
    #     return render_template('report.html', employees=employees, results=results, agg=agg, filters=request.form)
    # return render_template('report.html', employees=employees, results=None, agg=None, filters=None)


@app.route('/logs/download')
def logs_download():
    # Get filters from query string
    start = request.args.get('start_date')
    end = request.args.get('end_date')

    # Fetch filtered report JSON from backend
    BACKEND_URL = "http://127.0.0.1:5001"
    params = {}
    if start and end:
        params = {"start_date": start, "end_date": end}

    response = requests.get(f"{BACKEND_URL}/logs", params=params)
    reports_json = response.json()  # aggregated by employee_id

    # Flatten the JSON for rendering
    rows = []
    for emp_id, records in reports_json.items():
        for r in records:
            rows.append({
                "date": r["date"],
                "employee_id": r["employee_id"],
                "employee_name": r["employee_name"],
                "activity_name": r["activity_name"],
                "activity_duration": round(r["activity_duration"], 2),
                "attendance": r["attendance"]
            })

    # Render PDF template
    rendered = render_template('logs_pdf.html', results=rows)

    try:
        import pdfkit
        import io
        pdf = pdfkit.from_string(rendered, False)
        return send_file(
            io.BytesIO(pdf),
            mimetype='application/pdf',
            as_attachment=True,
            download_name='logs.pdf'
        )
    except Exception:
        # fallback: return HTML for printing
        return rendered

# @app.route('/api/reports/save', methods=['POST'])
# def api_reports_save():
#     data = request.get_json() or {}
#     # expected: employee_id, date, start_time, end_time, activity_type, duration_seconds
#     try:
#         re = ReportEntry(
#             employee_id=data.get('employee_id'),
#             date=datetime.datetime.strptime(data.get('date'), '%Y-%m-%d').date() if data.get('date') else datetime.date.today(),
#             start_time=datetime.datetime.strptime(data.get('start_time'), '%H:%M').time() if data.get('start_time') else None,
#             end_time=datetime.datetime.strptime(data.get('end_time'), '%H:%M').time() if data.get('end_time') else None,
#             activity_type=data.get('activity_type'),
#             duration_seconds=int(data.get('duration_seconds') or 0),
#             details=data.get('details')
#         )
#         db.session.add(re)
#         db.session.commit()
#         return jsonify({'status': 'ok', 'id': re.id})
#     except Exception as e:
#         db.session.rollback()
#         return jsonify({'status': 'error', 'message': str(e)}), 400


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
