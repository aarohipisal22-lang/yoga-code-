/* Yoga-Code: connects the pose-tracking page to the backend.
   Handles login, saving practice sessions, progress dashboard, profile and photo uploads.
   Camera video never leaves the browser; only session summaries and photos you choose to upload are sent. */
(() => {
  'use strict';
  const API = (window.YOGA_API || '') + '/api';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const modal = (id) => bootstrap.Modal.getOrCreateInstance($(id));
  let me = null;

  // ---------- API helper ----------
  async function api(path, { method = 'GET', json, body, headers = {} } = {}) {
    const opts = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'yoga', ...headers } };
    if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers['Content-Type'] = 'application/json'; }
    if (body !== undefined) opts.body = body;
    const res = await fetch(API + path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) throw Object.assign(new Error((data && data.error) || `Request failed (${res.status})`), { status: res.status });
    return data;
  }

  function toast(msg) {
    $('appToastBody').textContent = msg;
    bootstrap.Toast.getOrCreateInstance($('appToast'), { delay: 3500 }).show();
  }
  function localDay() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  const poseName = (k) => (POSES[k] ? POSES[k].english : k);
  const fmtSec = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`);

  // Downscale + re-encode images in the browser: keeps uploads small and strips EXIF/GPS data.
  async function prepareImage(file, maxDim) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) throw new Error('Please choose a JPG, PNG or WebP image.');
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
      const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
      const c = document.createElement('canvas');
      c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
      c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
      const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.85));
      if (blob) return blob;
    } catch (e) { /* fall back to the original file */ }
    if (file.size > 5 * 1024 * 1024) throw new Error('Image is too large (max 5 MB).');
    return file;
  }

  // ---------- navbar / auth ----------
  function renderAuth() {
    const el = $('authArea');
    if (!me) {
      el.innerHTML = '<button class="btn btn-brand btn-sm" id="loginBtn"><i class="fa-solid fa-user me-1"></i>Log in / Sign up</button>';
      $('loginBtn').onclick = () => modal('authModal').show();
      return;
    }
    const av = me.has_avatar
      ? `<img class="nav-avatar" alt="" src="${API}/me/avatar?v=${esc(me.avatar_v)}">`
      : `<span class="nav-avatar nav-avatar-initial">${esc(me.name.slice(0, 1).toUpperCase())}</span>`;
    el.innerHTML = `<div class="dropdown">
      <button class="btn btn-sm btn-nav dropdown-toggle d-flex align-items-center gap-2" data-bs-toggle="dropdown">${av}<span class="d-none d-sm-inline">${esc(me.name)}</span></button>
      <ul class="dropdown-menu dropdown-menu-end">
        <li><button class="dropdown-item" id="menuProgress"><i class="fa-solid fa-chart-line me-2"></i>My progress</button></li>
        <li><button class="dropdown-item" id="menuProfile"><i class="fa-solid fa-user-gear me-2"></i>Profile</button></li>
        <li><a class="dropdown-item" href="/onboarding.html"><i class="fa-solid fa-heart-pulse me-2"></i>Health profile</a></li>
        <li><hr class="dropdown-divider"></li>
        <li><button class="dropdown-item" id="menuLogout"><i class="fa-solid fa-right-from-bracket me-2"></i>Log out</button></li>
      </ul></div>`;
    $('menuProgress').onclick = openDashboard;
    $('menuProfile').onclick = openProfile;
    $('menuLogout').onclick = async () => {
      try { await api('/auth/logout', { method: 'POST', json: {} }); } catch (e) { /* ignore */ }
      location.replace('/login.html');
    };
  }

  function setupAuthForms() {
    const err = $('authError');
    const show = (m) => { err.textContent = m; err.classList.toggle('d-none', !m); };
    $('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault(); show('');
      try {
        const r = await api('/auth/login', { method: 'POST', json: { email: $('loginEmail').value, password: $('loginPassword').value } });
        me = r.user; $('loginForm').reset(); modal('authModal').hide(); renderAuth(); toast(`Welcome back, ${me.name}!`);
      } catch (ex) { show(ex.message); }
    });
    $('registerForm').addEventListener('submit', async (e) => {
      e.preventDefault(); show('');
      try {
        const r = await api('/auth/register', { method: 'POST', json: { name: $('regName').value, email: $('regEmail').value, password: $('regPassword').value } });
        me = r.user; $('registerForm').reset(); modal('authModal').hide(); renderAuth(); toast(`Welcome, ${me.name}! Your sessions will now be saved.`);
      } catch (ex) { show(ex.message); }
    });
  }

  // ---------- session tracking (reads the existing pose-tracking globals) ----------
  let seg = null;
  const startSeg = () => { seg = { pose: selectedPoseKey, t0: Date.now(), best: 0, c0: completedHolds }; };
  function closeSeg() {
    if (!seg) return null;
    const s = seg; seg = null;
    const duration = Math.round((Date.now() - s.t0) / 1000);
    if (duration < 5) return null;
    const best = Math.min(Math.max(s.best, holdSeconds), duration);
    const holds = Math.max(0, completedHolds - s.c0) + (holdSeconds >= 3 ? 1 : 0);
    return { pose: s.pose, duration_sec: duration, best_hold_sec: best, holds_completed: holds, day: localDay() };
  }
  async function saveSession(s, announce) {
    if (!s) return;
    if (!me) { if (announce) toast('Log in to save your practice and track progress.'); return; }
    try {
      await api('/sessions', { method: 'POST', json: s });
      if (announce) toast(`Saved: ${poseName(s.pose)}, ${fmtSec(s.duration_sec)}.`);
      if ($('dashModal').classList.contains('show')) refreshDashboard();
    } catch (ex) { if (announce) toast(`Could not save session: ${ex.message}`); }
  }
  function hookTracking() {
    const origSelect = window.selectPose;
    window.selectPose = function (key, el) {
      saveSession(closeSeg(), false);
      origSelect(key, el);
      if (activeStream) startSeg();
    };
    const origStop = window.stopWebcam;
    window.stopWebcam = function () {
      const s = closeSeg();
      origStop();
      saveSession(s, true);
    };
    setInterval(() => {
      if (activeStream && !seg) startSeg();
      if (seg) seg.best = Math.max(seg.best, holdSeconds);
    }, 500);
    window.addEventListener('pagehide', () => {
      const s = closeSeg();
      if (s && me) {
        fetch(API + '/sessions', { method: 'POST', keepalive: true, credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'yoga' }, body: JSON.stringify(s) });
      }
    });
  }

  // ---------- pose library from the API + illustrations ----------
  function hookPoseImages() {
    const origRender = window.renderInstructions;
    window.renderInstructions = function (key) {
      origRender(key);
      const img = $('poseImage'), p = POSES[key];
      img.classList.remove('d-none');
      img.src = p.image || `/media/poses/${key}.svg`;
      img.alt = `${p.english} illustration`;
    };
  }
  async function loadPoseLibrary() {
    try {
      const { poses } = await api('/poses');
      poses.forEach((p) => {
        const t = POSES[p.key];
        if (t) Object.assign(t, { description: p.description, holdTarget: p.holdTarget, instructions: p.instructions, image: p.image });
      });
    } catch (e) { /* offline: built-in data still works */ }
    renderPoseList();
    renderInstructions(selectedPoseKey);
  }

  // ---------- dashboard ----------
  async function openDashboard() {
    const sel = $('photoPose');
    if (!sel.options.length) {
      sel.innerHTML = '<option value="">General</option>' + Object.keys(POSES).map((k) => `<option value="${esc(k)}">${esc(poseName(k))}</option>`).join('');
    }
    modal('dashModal').show();
    refreshDashboard();
  }
  async function refreshDashboard() {
    try {
      const [st, se, ph] = await Promise.all([api('/stats?today=' + localDay()), api('/sessions?limit=20'), api('/photos')]);
      $('statSessions').textContent = st.total_sessions;
      $('statMinutes').textContent = st.total_minutes;
      $('statStreak').textContent = st.streak_days;
      $('statBest').textContent = fmtSec(st.best_hold_sec);
      const max = Math.max(1, ...st.week.map((d) => d.minutes));
      $('weekBars').innerHTML = st.week.map((d) => {
        const wd = new Date(d.day + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' });
        return `<div class="wk-col" title="${d.minutes} min"><div class="wk-bar" style="height:${Math.max(3, (d.minutes / max) * 100)}%"></div><small>${esc(wd)}</small></div>`;
      }).join('');
      $('perPose').innerHTML = st.per_pose.length
        ? st.per_pose.map((p) => `<tr><td>${esc(poseName(p.pose))}</td><td>${p.sessions}</td><td>${fmtSec(p.total_sec)}</td><td>${fmtSec(p.best_hold_sec)}</td></tr>`).join('')
        : '<tr><td colspan="4" class="text-muted">No practice yet. Start your camera and hold a pose!</td></tr>';
      $('sessionList').innerHTML = se.sessions.length
        ? se.sessions.map((s) => `<li class="list-group-item d-flex justify-content-between align-items-center">
            <span><strong>${esc(poseName(s.pose))}</strong> <small class="text-muted">${esc(s.day)} · ${fmtSec(s.duration_sec)} · best hold ${fmtSec(s.best_hold_sec)} · ${s.holds_completed} hold${s.holds_completed === 1 ? "" : "s"}</small></span>
            <button class="btn btn-sm btn-link text-danger" data-del-session="${s.id}" title="Delete"><i class="fa-regular fa-trash-can"></i></button></li>`).join('')
        : '<li class="list-group-item text-muted">Nothing here yet.</li>';
      $('photoGrid').innerHTML = ph.photos.length
        ? ph.photos.map((p) => `<figure class="photo-tile"><img loading="lazy" alt="${esc(p.caption || 'Progress photo')}" src="${esc(p.url)}">
            <figcaption><small>${esc(p.pose ? poseName(p.pose) : 'General')}${p.caption ? ' · ' + esc(p.caption) : ''}</small>
            <button class="btn btn-sm btn-link text-danger p-0" data-del-photo="${p.id}" title="Delete"><i class="fa-regular fa-trash-can"></i></button></figcaption></figure>`).join('')
        : '<p class="text-muted small mb-0">No photos yet. Upload one to track how your form improves.</p>';
    } catch (ex) { toast(ex.message); }
  }
  function setupDashboard() {
    $('dashModal').addEventListener('click', async (e) => {
      const s = e.target.closest('[data-del-session]'), p = e.target.closest('[data-del-photo]');
      try {
        if (s && confirm('Delete this session?')) { await api('/sessions/' + s.dataset.delSession, { method: 'DELETE' }); refreshDashboard(); }
        if (p && confirm('Delete this photo?')) { await api('/photos/' + p.dataset.delPhoto, { method: 'DELETE' }); refreshDashboard(); }
      } catch (ex) { toast(ex.message); }
    });
    $('photoForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = $('photoFile').files[0];
      if (!f) return toast('Choose a photo first.');
      const btn = $('photoSubmit'); btn.disabled = true;
      try {
        const blob = await prepareImage(f, 1600);
        const q = `?pose=${encodeURIComponent($('photoPose').value)}&caption=${encodeURIComponent($('photoCaption').value.trim())}`;
        await api('/photos' + q, { method: 'POST', body: blob, headers: { 'Content-Type': blob.type } });
        $('photoForm').reset(); toast('Photo uploaded.'); refreshDashboard();
      } catch (ex) { toast(ex.message); } finally { btn.disabled = false; }
    });
  }

  // ---------- profile ----------
  function openProfile() {
    $('profName').value = me.name; $('profGoal').value = me.goal; $('profLevel').value = me.level;
    $('profEmail').textContent = me.email;
    refreshProfileAvatar();
    modal('profileModal').show();
  }
  function refreshProfileAvatar() {
    $('profAvatar').innerHTML = me.has_avatar ? `<img class="prof-avatar" alt="" src="${API}/me/avatar?v=${esc(me.avatar_v)}">`
      : `<span class="prof-avatar nav-avatar-initial">${esc(me.name.slice(0, 1).toUpperCase())}</span>`;
  }
  function setupProfile() {
    $('profileForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        me = (await api('/me', { method: 'PATCH', json: { name: $('profName').value, goal: $('profGoal').value, level: $('profLevel').value } })).user;
        renderAuth(); toast('Profile saved.');
      } catch (ex) { toast(ex.message); }
    });
    $('avatarFile').addEventListener('change', async (e) => {
      const f = e.target.files[0]; if (!f) return;
      try {
        const blob = await prepareImage(f, 512);
        me = (await api('/me/avatar', { method: 'POST', body: blob, headers: { 'Content-Type': blob.type } })).user;
        refreshProfileAvatar(); renderAuth(); toast('Profile photo updated.');
      } catch (ex) { toast(ex.message); } finally { e.target.value = ''; }
    });
    $('deleteAccountBtn').addEventListener('click', async () => {
      const pw = $('deletePassword').value;
      if (!pw) return toast('Enter your password to confirm.');
      if (!confirm('Permanently delete your account, sessions and photos? This cannot be undone.')) return;
      try {
        await api('/me/delete', { method: 'POST', json: { password: pw } });
        location.replace('/login.html');
      } catch (ex) { toast(ex.message); }
    });
  }

  // ---------- init ----------
  hookPoseImages();
  hookTracking();
  setupAuthForms();
  setupDashboard();
  setupProfile();
  renderInstructions(selectedPoseKey);
  loadPoseLibrary();
  api('/me').then((r) => {
    // Gate: must be logged in, and must have answered the health questions once.
    if (!r.user) return location.replace('/login.html');
    if (!r.user.onboarded) return location.replace('/onboarding.html');
    me = r.user; renderAuth();
  }).catch(() => {
    $('authArea').innerHTML = '<span class="small text-muted">Offline mode (server not running)</span>';
  });
})();
