/* Shared helpers for the login and onboarding pages. */
const API = '/api';
async function api(path, { method = 'GET', json } = {}) {
  const opts = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'yoga' } };
  if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers['Content-Type'] = 'application/json'; }
  const res = await fetch(API + path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}
function showError(el, msg) { el.textContent = msg; el.classList.toggle('d-none', !msg); }
