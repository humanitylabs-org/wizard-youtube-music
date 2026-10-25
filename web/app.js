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
  const a = s.account_status || {};
  const labels = { signed_out: 'Signed out. Public catalog only.', unverified: 'Account credentials found, not verified. Tap Verify connection.', connected: 'Account connected: library access verified.', needs_attention: 'Account needs attention. Verify again or disconnect and reconnect.' };
  el('via').textContent = labels[a.state] || 'Account status unknown.';
  el('connect').disabled = !a.configured || a.state !== 'signed_out';
  if (!a.configured && a.state === 'signed_out') el('via').textContent += ' Owner must provision oauth-client.json privately first (see README).';
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
el('connect').onclick = async () => {
  el('connect').disabled = true; say('');
  try {
    const a = await api('api/account/start', {});
    el('google-link').href = a.verification_url;
    el('user-code').textContent = a.user_code;
    el('auth-progress').textContent = 'Authorization expires in ' + a.expires_in + ' seconds.';
    el('auth-step').hidden = false;
  } catch (e) { say(e.message); }
  finally { api('api/status').then(render).catch(e => say(e.message)); }
};
el('finish').onclick = async () => {
  el('finish').disabled = true;
  try {
    const a = await api('api/account/poll', {});
    if (a.state === 'pending') el('auth-progress').textContent = 'Still waiting for Google. Wait ' + a.retry_after + ' seconds, then check again.';
    else {
      el('auth-step').hidden = true; el('user-code').textContent = ''; el('google-link').removeAttribute('href');
      if (a.status) render(a.status);
      else say('Authorization ' + a.state + '. Start again if you want to connect.');
    }
  } catch (e) { say(e.message); }
  finally { el('finish').disabled = false; }
};
el('check-account').onclick = () => run(() => api('api/account/check', {}), 'check-account');
el('disconnect').onclick = () => run(async () => {
  const s = await api('api/account/disconnect', {});
  el('auth-step').hidden = true; el('user-code').textContent = ''; el('google-link').removeAttribute('href');
  return s;
}, 'disconnect');
el('ask-help').onclick = () => {
  el('help-request').value = 'Help me set up YouTube Music on my Wizard. First check whether Wizard runs at home or on a VPS. For a VPS, guide me through an always-on home Mac, Windows or Linux relay over my private Tailscale network. Ask which computer I will use. Keep relay passwords, OAuth client secrets, cookies and codes out of chat. For playlists and likes, help me privately configure my own Google Cloud TV/device OAuth client and complete consent on Google. Verify account access and playback separately; do not promise ad-free, unlimited or risk-free playback. Do not change a working relay without my approval.';
  el('help-step').hidden = false;
};
el('copy-help').onclick = async () => {
  try { await navigator.clipboard.writeText(el('help-request').value); say('Request copied. Paste it into your agent chat; nothing was sent.', 'note'); }
  catch { el('help-request').focus(); el('help-request').select(); say('Select and copy the request manually.', 'note'); }
};
el('open-agents').onclick = () => {
  if (window.parent !== window) {
    window.parent.postMessage({ type: 'wizard:open-app', id: 'agents' }, location.origin);
    say('Asked the shell to open Agents. Choose your agent and paste the request. If it does not open, use Agents from Home.', 'note');
  } else say('Open Agents from your Wizard Home, then choose your agent chat and paste the request.', 'note');
};
window.__wizardCloseOverlay = () => false;
api('api/status').then(render).catch((e) => say(e.message));
