/* main.js — cleaned and simplified
   - Uses backend stream at http://127.0.0.1:5001
   - Frontend endpoints: /api/sessions, /api/sessions/delete, /api/sessions/upload
   - Replaces old video/WebRTC code with MJPEG <img> stream injection
*/
window.intervalFeed = null;
document.addEventListener('DOMContentLoaded', () => {
  // --- basic UI toggles ---
  const toggle = document.getElementById('menu-toggle');
  const sidebar = document.getElementById('sidebar');
  toggle?.addEventListener('click', () => {
    sidebar.style.display = sidebar.style.display === 'none' ? 'block' : 'none';
  });
  // stopSession();
  const genBtn = document.getElementById('generateReportBtn');
  genBtn?.addEventListener('click', () => { window.location = '/reports'; });

  // --- sessions table (polling refresh) ---
  async function loadSessions() {
    const table = document.getElementById('sessionsTable');
    if (!table) return;
    try {
      const res = await fetch('/api/sessions');
      const data = await res.json();
      const tbody = table.querySelector('tbody');
      tbody.innerHTML = '';

      if (!data || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No sessions found</td></tr>';
        updateSessionControls();
        return;
      }

      data.forEach(s => {
        const tr = document.createElement('tr');
        const date = s.created_at ? new Date(s.created_at).toLocaleString() : '-';
        const dur = (s.duration_seconds !== undefined && s.duration_seconds !== null) ? `${s.duration_seconds}s` : '-';
        const filename = s.filename ? `/static/videos/${s.filename}` : '#';
        tr.innerHTML = `
          <td><input class="sessionCheckbox" type="checkbox" value="${s.id}"></td>
          <td>${date}</td>
          <td>${dur}</td>
          <td>
            ${s.filename ? `<a class="btn btn-outline-secondary btn-sm" href="${filename}" download>Download</a>` : ''}
          </td>
        `;
        tbody.appendChild(tr);
      });

      // wire up checkboxes + select all
      updateSessionControls();
    } catch (err) {
      const tbody = document.querySelector('#sessionsTable tbody');
      if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="text-muted">Could not load sessions</td></tr>';
      console.error('loadSessions error', err);
    }
  }

  function updateSessionControls() {
    const tbody = document.querySelector('#sessionsTable tbody');
    if (!tbody) return;
    const checkboxes = Array.from(tbody.querySelectorAll('.sessionCheckbox'));
    const deleteBtn = document.getElementById('deleteSelectedBtn');
    const selectAll = document.getElementById('selectAllSessions');

    function updateDeleteBtn() {
      const any = checkboxes.some(c => c.checked);
      if (deleteBtn) deleteBtn.disabled = !any;
    }

    checkboxes.forEach(cb => cb.removeEventListener('change', updateDeleteBtn));
    checkboxes.forEach(cb => cb.addEventListener('change', updateDeleteBtn));

    if (selectAll) {
      selectAll.checked = false;
      selectAll.removeEventListener('change', onSelectAllChange);
      selectAll.addEventListener('change', onSelectAllChange);
    }

    updateDeleteBtn();

    function onSelectAllChange() {
      checkboxes.forEach(cb => cb.checked = selectAll.checked);
      updateDeleteBtn();
    }
  }

  // initial load + polling
  loadSessions();
  setInterval(loadSessions, 30_000);

  // --- delete selected sessions ---
  const deleteSelectedBtn = document.getElementById('deleteSelectedBtn');
  deleteSelectedBtn?.addEventListener('click', async () => {
    const tbody = document.querySelector('#sessionsTable tbody');
    if (!tbody) return;
    const checked = Array.from(tbody.querySelectorAll('.sessionCheckbox:checked')).map(i => i.value);
    if (!checked.length) return;
    if (!confirm(`Delete ${checked.length} session(s) from records? This will not remove the video files.`)) return;

    try {
      const res = await fetch('/api/sessions/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: checked })
      });
      const data = await res.json();
      if (data.status === 'ok') {
        document.getElementById('selectAllSessions') && (document.getElementById('selectAllSessions').checked = false);
        loadSessions();
      } else {
        alert('Could not delete sessions: ' + (data.message || 'unknown'));
      }
    } catch (err) {
      console.error('delete sessions error', err);
      alert('Error deleting sessions');
    }
  });

  // --- start session UI (modal) ---
  const startBtn = document.getElementById('startSessionBtn');
  const startModalEl = document.getElementById('startSessionModal');
  const startForm = document.getElementById('startSessionForm');
  const cameraContainer = document.getElementById('cameraContainer') || document.getElementById('videoBox');
  const videoBox = document.getElementById('videoBox');

  if (startBtn && startModalEl) {
    startBtn.addEventListener('click', () => {
      const modal = new bootstrap.Modal(startModalEl);
      modal.show();
    });
  }

  // --- recording timer UI (frontend only) ---
  let recordingTimer = null;
  let startTime = null;
  let autoStopTimeout = null;

  function formatTime(ms) {
    const s = Math.floor(ms / 1000);
    const hh = Math.floor(s / 3600);
    const mm = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    return (hh > 0 ? String(hh).padStart(2, '0') + ':' : '') + String(mm).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
  }

  function startTimerUI() {
    startTime = new Date();
    const indicator = document.getElementById('recIndicator');
    indicator && (indicator.style.display = 'inline-block');
    const timerEl = document.getElementById('recTimer');
    const recordStatus = document.getElementById('recordingStatus');
    const recordStatusText = document.getElementById('recordingStatusText');
    if (recordStatus) recordStatus.classList.remove('d-none');
    if (recordStatusText) recordStatusText.textContent = 'Recording';
    recordingTimer = setInterval(() => {
      const now = new Date();
      const ms = now - startTime;
      if (timerEl) timerEl.textContent = formatTime(ms);
      const recordStatusTimer = document.getElementById('recordingStatusTimer');
      if (recordStatusTimer) recordStatusTimer.textContent = formatTime(ms);
    }, 250);
  }

  function stopTimerUI() {
    if (recordingTimer) {
      clearInterval(recordingTimer);
      recordingTimer = null;
    }
    const timerEl = document.getElementById('recTimer');
    if (timerEl) timerEl.textContent = '00:00:00';
    const recordStatus = document.getElementById('recordingStatus');
    if (recordStatus) recordStatus.classList.add('d-none');
  }

  function injectLiveFeed() {
    const target = document.getElementById('cameraContainer') || document.getElementById('videoBox');
    if (!target) return;
    target.innerHTML = `<img id="liveFeed" src="http://127.0.0.1:5001/video_feed?cache=${new Date().getTime()} class="w-100" style="max-height:400px; object-fit:contain; border-radius:6px;">`;
  }

  function removeLiveFeed() {
    const target = document.getElementById('cameraContainer') || document.getElementById('videoBox');
    if (!target) return;
    target.innerHTML = '<div class="placeholder-inner text-center"><p class="mb-0">Start a session to open the camera</p></div>';
  }

  // Start/stop session handlers forwarded to backend (no WebRTC)
  async function startSessionOnBackend() {
    try {
      const res = await fetch("http://127.0.0.1:5001/start_stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: "Start the stream" })
      });
      const data = await res.json();
      if (res.ok) {
        injectLiveFeed();
        // window.intervalFeed = setInterval(()=>{removeLiveFeed();injectLiveFeed();},3000); // slight delay to allow backend to start stream
        console.log('Stream started:', data);
      } else {
        alert('Could not start stream: ' + (data.error || JSON.stringify(data)));
      }
    } catch (err) {
      console.error('start stream error', err);
      alert('Error starting stream: ' + err.message);
    }
  }

  async function stopSessionOnBackend() {
    try {
      const res = await fetch("http://127.0.0.1:5001/stop_stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: "Stop the stream" })
      });
      const data = await res.json();
      if (res.ok) {
        clearInterval(window.intervalFeed);
        removeLiveFeed();
        console.log('Stream stopped:', data);
      } else {
        alert('Could not stop stream: ' + (data.error || JSON.stringify(data)));
      }
    } catch (err) {
      console.error('stop stream error', err);
      alert('Error stopping stream: ' + err.message);
    }
  }

  // hook up the start form submit — this will call backend and then start timer and UI
  if (startForm) {
    startForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();

      // duration fields (hours/minutes/seconds)
      const hours = parseInt(document.getElementById('sessionHours')?.value || '0', 10);
      const minutes = parseInt(document.getElementById('sessionMinutes')?.value || '0', 10);
      const seconds = parseInt(document.getElementById('sessionSeconds')?.value || '0', 10);
      const durationSec = (Math.max(0, hours) * 3600) + (Math.max(0, minutes) * 60) + Math.max(0, seconds);
      if (!durationSec || durationSec <= 0) { alert('Please enter a duration greater than 0'); return; }

      const modal = bootstrap.Modal.getInstance(startModalEl);
      modal?.hide();

      // start backend stream + UI
      await startSessionOnBackend();
      startTimerUI();

      // enable force stop button
      const fsbtn = document.getElementById('forceStopBtn');
      if (fsbtn) { fsbtn.disabled = false; fsbtn.classList.remove('disabled'); fsbtn.classList.remove('d-none'); }

      // schedule auto-stop
      autoStopTimeout = setTimeout(() => {
        stopSession();
      }, durationSec * 1000);

      // expose for debug
      window.stopSession = stopSession;
    });
  }

  // force stop logic
  async function stopSession() {
    try {
      if (autoStopTimeout) { clearTimeout(autoStopTimeout); autoStopTimeout = null; }
      stopTimerUI();

      // disable/hide force stop button
      const fsbtn = document.getElementById('forceStopBtn');
      if (fsbtn) { fsbtn.disabled = true; fsbtn.classList.add('disabled'); fsbtn.classList.add('d-none'); }

      // call backend to stop stream
      await stopSessionOnBackend();

      // refresh session list (server may have recorded a session)
      loadSessions();
    } catch (err) {
      console.error('stopSession error', err);
    }
  }

  // wire force stop UI
  const forceStopBtn = document.getElementById('forceStopBtn');
  if (forceStopBtn) {
    forceStopBtn.addEventListener('click', () => {
      const modalEl = document.getElementById('forceStopConfirmModal');
      if (modalEl) {
        const modal = new bootstrap.Modal(modalEl);
        modal.show();
      } else {
        if (confirm('Force stop the current recording session?')) stopSession();
      }
    });
  }

  const confirmBtn = document.getElementById('forceStopConfirmBtn');
  confirmBtn?.addEventListener('click', () => {
    stopSession();
    const m = bootstrap.Modal.getInstance(document.getElementById('forceStopConfirmModal'));
    m?.hide();
  });

  // --- utility: expose functions for console debugging ---
  window.appLoadSessions = loadSessions;
  window.appStartSession = startSessionOnBackend;
  window.appStopSession = stopSession;
});
