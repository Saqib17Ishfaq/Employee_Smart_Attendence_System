call .\.venv\Scripts\activate
start "Backend Server" python main.py
start "Frontend Server" python app.py