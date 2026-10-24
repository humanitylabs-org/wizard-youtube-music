'use strict';
const el = (id) => document.getElementById(id);
const say = (t, kind = 'error') => { const s = el('status'); s.className = 'wz-status' + (kind === 'note' ? ' note' : ''); s.textContent = t; };
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'Something went wrong. Try again.');
  return j;
}
const LABEL = { ok: 'Working', untested: 'Not tested yet', blocked: 'Blocked by YouTube', failed: 'Last test failed' };
function render(s) {
  const p = s.playback || {};
  const state = el('state');
  state.textContent = (LABEL[p.state] || 'Unknown') + '. ' + (p.message || '');
  state.dataset.state = p.state || '';
  el('via').textContent = s.account ? 'Signed in with your YouTube account.' : '';
  el('relay-on').hidden = !s.relay;
  el('relay-form').hidden = s.relay;
}
async function run(fn, busy) {
  const b = el(busy); b.disabled = true; say('');
  try { render(await fn()); } catch (e) { say(e.message); } finally { b.disabled = false; }
}
el('test').onclick = () => { el('state').textContent = 'Testing… this downloads one song.'; run(() => api('api/test', {}), 'test'); };
el('relay-form').onsubmit = (e) => {
  e.preventDefault();
  const v = el('relay').value.trim(); if (!v) return;
  run(async () => { await api('api/relay', { relay: v }); el('relay').value = ''; el('state').textContent = 'Testing…'; return api('api/test', {}); }, 'save');
};
el('relay-off').onclick = () => run(() => api('api/relay', { relay: '' }), 'relay-off');
window.__wizardCloseOverlay = () => false;
api('api/status').then(render).catch((e) => say(e.message));
