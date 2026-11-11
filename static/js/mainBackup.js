document.addEventListener('DOMContentLoaded', ()=>{
  const toggle = document.getElementById('menu-toggle');
  const sidebar = document.getElementById('sidebar');
  toggle && toggle.addEventListener('click', ()=>{
    if (sidebar.style.display === 'none') sidebar.style.display = 'block'; else sidebar.style.display = 'none';
  });

  // Generate report button placeholder
  const genBtn = document.getElementById('generateReportBtn');
  if (genBtn) genBtn.addEventListener('click', ()=>{ window.location = '/reports'; });

  // load recent sessions and populate sessions table
  function loadSessions(){
    const table = document.getElementById('sessionsTable');
    if (!table) return;
    fetch('/api/sessions').then(r=>r.json()).then(data=>{
      const tbody = table.querySelector('tbody'); tbody.innerHTML = '';
      if (!data.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No sessions found</td></tr>';
        return;
      }
      data.forEach(s=>{
        const tr = document.createElement('tr');
        const date = new Date(s.created_at).toLocaleString();
        const emp = s.employee_name || '-';
        const dur = s.duration_seconds ? (s.duration_seconds + 's') : '-';
        tr.innerHTML = `
          <td><input class="sessionCheckbox" type="checkbox" value="${s.id}" /></td>
          <td>${date}</td>
          <td>${dur}</td>
          <td>
            <a class="btn btn-outline-secondary btn-sm" href="/static/videos/${s.filename}" download>Download</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      const deleteBtn = document.getElementById('deleteSelectedBtn');
      const allCheckboxes = Array.from(tbody.querySelectorAll('.sessionCheckbox'));
      function updateDeleteBtn(){
        const any = allCheckboxes.some(c=>c.checked);
        if (deleteBtn) deleteBtn.disabled = !any;
      }
      allCheckboxes.forEach(c=> c.addEventListener('change', updateDeleteBtn));
      const selectAll = document.getElementById('selectAllSessions');
      if (selectAll){
        selectAll.checked = false;
        selectAll.addEventListener('change', ()=>{
          allCheckboxes.forEach(cb=> cb.checked = selectAll.checked);
          updateDeleteBtn();
        });
      }
    }).catch(()=>{
      const tbody = document.querySelector('#sessionsTable tbody'); if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="text-muted">Could not load sessions</td></tr>';
    });
  }

  // initial load
  loadSessions();

  // Delete selected sessions (only DB records — files remain)
  const deleteSelectedBtn = document.getElementById('deleteSelectedBtn');
  if (deleteSelectedBtn){
    deleteSelectedBtn.addEventListener('click', async ()=>{
      const tbody = document.querySelector('#sessionsTable tbody'); if (!tbody) return;
      const checked = Array.from(tbody.querySelectorAll('.sessionCheckbox:checked')).map(i=>i.value);
      if (!checked.length) return;
      if (!confirm(`Delete ${checked.length} session(s) from records? This will not remove the video files.`)) return;
      try{
        const res = await fetch('/api/sessions/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ ids: checked }) });
        const data = await res.json();
        if (data.status === 'ok'){
          // refresh table and reset select-all
          const sel = document.getElementById('selectAllSessions'); if (sel) sel.checked = false;
          loadSessions();
        } else {
          alert('Could not delete sessions: ' + (data.message||'unknown'));
        }
      }catch(err){ console.error('delete sessions error', err); alert('Error deleting sessions'); }
    });
  }

  // also reload sessions after successful upload (simple approach: poll periodically)
  setInterval(loadSessions, 30_000);

  // Session recording logic
  let mediaStream = null;
  let mediaRecorder = null;
  let recordedChunks = [];
  let recordingTimer = null;
  let startTime = null;
  let autoStopTimeout = null;

  const startBtn = document.getElementById('startSessionBtn');
  const startModalEl = document.getElementById('startSessionModal');
  const startForm = document.getElementById('startSessionForm');
  // primary display area for camera/recording — use the main video box to keep everything in-place
  const cameraContainer = document.getElementById('cameraContainer') || document.getElementById('videoBox');
  const videoBox = document.getElementById('videoBox');

  if (startBtn && startModalEl) {
    startBtn.addEventListener('click', ()=>{
      // show bootstrap modal
      const modal = new bootstrap.Modal(startModalEl);
      modal.show();
    });
  }

  // helper to format time
  function formatTime(ms){
    const s = Math.floor(ms/1000);
    const hh = Math.floor(s/3600); const mm = Math.floor((s%3600)/60); const ss = s%60;
    return (hh>0?String(hh).padStart(2,'0')+':':'') + String(mm).padStart(2,'0')+':'+String(ss).padStart(2,'0');
  }

  function stopAllStreams(){
    if (mediaStream) {
      mediaStream.getTracks().forEach(t=>t.stop());
      mediaStream = null;
    }
  }

  function cleanupRecordingUI(){
    // stopAllStreams();
    if (recordingTimer) { clearInterval(recordingTimer); recordingTimer = null; }
    const fsbtnAfter = document.getElementById('forceStopBtn'); if (fsbtnAfter) { fsbtnAfter.disabled = true; fsbtnAfter.classList.add('disabled'); fsbtnAfter.classList.add('d-none'); }
  }

  if (startForm) startForm.addEventListener('submit', async (ev)=>{
    ev.preventDefault();
    // read duration as hours/minutes/seconds
    const hours = parseInt(document.getElementById('sessionHours').value || '0', 10);
    const minutes = parseInt(document.getElementById('sessionMinutes').value || '0', 10);
    const seconds = parseInt(document.getElementById('sessionSeconds').value || '0', 10);
    const durationSec = (Math.max(0, hours) * 3600) + (Math.max(0, minutes) * 60) + Math.max(0, seconds);
    if (!durationSec || durationSec <= 0) { alert('Please enter a duration greater than 0'); return; }
    // No employee selection required — only duration is used
    const modal = bootstrap.Modal.getInstance(startModalEl);
    modal.hide();

  //   // open camera with compatibility checks
  //   async function getCameraStream(constraints){
  //     if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
  //       return await navigator.mediaDevices.getUserMedia(constraints);
  //     }
  //     const getUserMedia = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
  //     if (getUserMedia) {
  //       return new Promise((resolve, reject)=> getUserMedia.call(navigator, constraints, resolve, reject));
  //     }
  //     throw new Error('getUserMedia is not supported by this browser. Use a modern browser (Chrome, Edge, Firefox) on localhost or HTTPS.');
  //   }

  //   try{
  //     mediaStream = await getCameraStream({video:true,audio:true});
  //   }catch(err){
  //     console.error('Camera open error', err);
  //     let msg = err && err.message ? err.message : String(err);
  //     if (/not allowed|Permission|denied/i.test(msg)) msg = 'Permission denied. Please allow camera/microphone access.';
  //     if (/secure context/i.test(msg) || /https/i.test(msg)) msg += ' Note: getUserMedia requires a secure context (https) except for localhost.';
  //     alert('Could not open camera: ' + msg);
  //     return;
  //   }

  // // replace the existing dashboard video box with the live preview and controls
  // if (videoBox) videoBox.innerHTML = '';
  // cameraContainer.innerHTML = '';
  //   const video = document.createElement('video');
  //   video.id = 'sessionVideo'; video.autoplay = true; video.playsInline = true; video.muted = false;
  //   video.style.maxWidth = '100%'; video.style.borderRadius = '6px';
  //   video.srcObject = mediaStream;
  // // mute preview so browsers will allow autoplay; recording still captures audio from stream
  // video.muted = true;
  // // ensure the preview is placed inside the dashboard video area
  // const target = (videoBox || cameraContainer);
  // target.appendChild(video);
  // // try to play the preview
  // video.play().catch(()=>{});
  // // prevent the video element from intercepting pointer events so overlay controls stay clickable
  // try{ video.style.pointerEvents = 'none'; }catch(e){}

  //   // we don't add in-frame start/pause/stop buttons — recording is controlled from the right column (Force Stop) and auto-stop

  // recordedChunks = [];
  //   try{
  //     mediaRecorder = new MediaRecorder(mediaStream, { mimeType: 'video/webm;codecs=vp8,opus' });
  //   }catch(e){
  //     // fallback to default if requested mimeType not supported
  //     mediaRecorder = new MediaRecorder(mediaStream);
  //   }

  //mediaRecorder.ondataavailable = function(e){ if (e.data && e.data.size>0) recordedChunks.push(e.data); };
  // mediaRecorder.onstop = async function(){
  //   // assemble blob
  //   const blob = new Blob(recordedChunks, {type:'video/webm'});

  //   // create display label and safe filename based on startTime (include seconds)
  //   function pad(n){ return String(n).padStart(2,'0'); }
  //   const st = startTime || new Date();
  //   const dd = pad(st.getDate()), mm = pad(st.getMonth()+1), yyyy = st.getFullYear();
  //   const hh = pad(st.getHours()), mins = pad(st.getMinutes()), ss = pad(st.getSeconds());
  //   const displayLabel = `${dd}/${mm}/${yyyy}-${hh}:${mins}:${ss}`; // for UI display
  //   // final filename format: DD-MM-YYYY_HH-MM-SS.webm
  //   const safeFilename = `${dd}-${mm}-${yyyy}_${hh}-${mins}-${ss}.webm`; // filesystem-friendly

  //       // Immediately stop/close the camera preview so the container is cleared for the user
  //       stopAllStreams();
  //       if (videoBox) videoBox.innerHTML = '<div class="placeholder-inner text-muted p-3">Camera closed</div>';
  //       else cameraContainer.innerHTML = '<div class="placeholder-inner text-muted p-3">Camera closed</div>';

  //       // update right-column recording status to indicate saving
  //       const recordStatusText = document.getElementById('recordingStatusText');
  //       if (recordStatusText) recordStatusText.textContent = 'Saving...';

  //       // prepare upload with requested filename (upload in background)
  //       const form = new FormData();
  //       form.append('video', blob, safeFilename);
  //       form.append('filename', safeFilename);
  // // no employee_id sent (session not associated with a specific employee)
  //       if (startTime) form.append('start_time', startTime.toISOString());
  //       const endTime = new Date(); form.append('end_time', endTime.toISOString());
  //       const durationSec = Math.round((endTime - startTime)/1000); form.append('duration_seconds', String(durationSec));

  //       try{
  //         const res = await fetch('/api/sessions/upload', { method:'POST', body: form });
  //         const data = await res.json();
  //         if (data.status === 'ok') {
  //           if (recordStatusText) recordStatusText.textContent = 'Saved';
  //           loadSessions();
  //         } else {
  //           if (recordStatusText) recordStatusText.textContent = 'Upload failed';
  //           console.error('Upload error', data);
  //         }
  //       }catch(err){
  //         if (recordStatusText) recordStatusText.textContent = 'Upload error';
  //         console.error('Upload failed', err);
  //       }

  //       // cleanup timers and UI
  //       if (recordingTimer) { clearInterval(recordingTimer); recordingTimer = null; }
  //       const fsbtnAfter = document.getElementById('forceStopBtn'); if (fsbtnAfter) { fsbtnAfter.disabled = true; fsbtnAfter.classList.add('disabled'); }
  //     };

    // timer
    function startTimer(){
      startTime = new Date();
      const indicator = document.getElementById('recIndicator');
      if (indicator) indicator.style.display = 'inline-block';
      const timerEl = document.getElementById('recTimer');
      // show right-column recording status and timer
      const recordStatus = document.getElementById('recordingStatus');
      const recordStatusTimer = document.getElementById('recordingStatusTimer');
      const recordStatusText = document.getElementById('recordingStatusText');
      if (recordStatus) { recordStatus.classList.remove('d-none'); }
      if (recordStatusText) { recordStatusText.textContent = 'Recording'; }
      recordingTimer = setInterval(()=>{
        const now = new Date(); const ms = now - startTime; if (timerEl) timerEl.textContent = formatTime(ms);
        if (recordStatusTimer) recordStatusTimer.textContent = formatTime(ms);
      }, 250);
    }

    function stopTimer(){ if (recordingTimer) { clearInterval(recordingTimer); recordingTimer=null; } }

    // centralized stop logic to ensure consistent behavior
    function stopSession(){
      try{
        // clear scheduled auto-stop
        if (autoStopTimeout) { clearTimeout(autoStopTimeout); autoStopTimeout = null; }
        // hide right-column recording status
        const recordStatus = document.getElementById('recordingStatus'); if (recordStatus) { recordStatus.classList.add('d-none'); }
        const recordStatusTimer = document.getElementById('recordingStatusTimer'); if (recordStatusTimer) { recordStatusTimer.textContent = '00:00:00'; }
        // stop timer
        stopTimer();
        // stop the recorder if active
        // if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')){
        //   try{ mediaRecorder.stop(); }catch(e){ console.warn('mediaRecorder.stop() error', e); }
        // } else {
        //   // no recorder running: just cleanup UI and streams
        //   cleanupRecordingUI();
        // }
        // disable force stop button
    // disable and hide force stop button
    const fs = document.getElementById('forceStopBtn'); if (fs) { fs.disabled = true; fs.classList.add('disabled'); fs.classList.add('d-none'); }
      }catch(err){ console.error('stopSession error', err); }
      fetch("http://127.0.0.1:5001/stop_stream", {
          method: "POST",
          headers: {
              "Content-Type": "application/json"
          },
          body: JSON.stringify({
              message: "Start the stream"
          })
      })
      .then(response => response.json())
      .then(data => {
          console.log("Server Response:", data);
          cleanupRecordingUI();
      })
      .catch(error => {
          console.error("Error:", error);
      });

    }

    // no in-frame controls; recording will start automatically below and be stopped via Force Stop or auto-stop

    // hook up the dashboard-level force stop button
    const forceStopBtn = document.getElementById('forceStopBtn');
    if (forceStopBtn) {
      forceStopBtn.addEventListener('click', ()=>{
        // show confirmation modal instead of plain confirm
        const modalEl = document.getElementById('forceStopConfirmModal');
        let modal = null;
        // let modalEl = false;
        if (modalEl) {
          modal = new bootstrap.Modal(modalEl);
          modal.show();

        } else {
          const ok = confirm('Force stop the current recording session?');
          if (ok) stopSession();
        }
      });
      
      //API Call for start recording session...
      fetch("http://127.0.0.1:5001/start_stream", {
          method: "POST",
          headers: {
              "Content-Type": "application/json"
          },
          body: JSON.stringify({
              message: "Start the stream"
          })
      })
      .then(response => response.json())
      .then(data => {
          console.log("Server Response:", data);
          document.getElementById('videoBox').innerHTML = '<img id="liveFeed" src="http://127.0.0.1:5001/video_feed">';
      })
      .catch(error => {
          console.error("Error:", error);
      });

      // hook confirm button
      
    }

  // auto-start recording immediately (start recorder and timer)
 // try{ mediaRecorder.start(1000); }catch(e){}
  startTimer();
  // enable force stop button in dashboard (also make it visible)
  const fsbtn = document.getElementById('forceStopBtn'); if (fsbtn) { fsbtn.disabled = false; fsbtn.classList.remove('disabled'); fsbtn.classList.remove('d-none'); }
  // no in-frame controls to update

    // optional auto-stop after requested duration (durationSec from form)
    autoStopTimeout = setTimeout(()=>{
      try{
        console.log('Auto-stop triggered');
        stopSession();
      }catch(e){ console.error('autoStop error', e); }
      autoStopTimeout = null;
    }, durationSec * 1000);
    // expose for debug
    window.stopSession = stopSession;
  });

});

const confirmBtn = document.getElementById('forceStopConfirmBtn');
      if (confirmBtn) confirmBtn.addEventListener('click', ()=>{ stopSession(); const m = bootstrap.Modal.getInstance(document.getElementById('forceStopConfirmModal'));console.log("a"); if (m) m.hide(); });