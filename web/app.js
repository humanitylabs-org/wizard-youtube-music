'use strict';
// YouTube Music, connector pattern: status card, playback, the optional account and the home relay (their rare
// actions open a sheet), one primary action (Connect), details.
const el = (id) => document.getElementById(id);
const say = (t, kind = 'error') => { const s = el('status'); s.className = 'wz-status' + (kind === 'note' ? ' note' : ''); s.textContent = t; };
async function api(path, body, authorization) {
  const headers = { 'Content-Type': 'application/json' };
  if (authorization) headers.Authorization = 'Bearer ' + authorization;
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'Something went wrong. Try again.');
  return j;
}
// One pop-up: the shared sheet (wizard-sheet.js). Without it (tests, old shells) the content still updates.
function openSheet(id) { const d = el(id); if (window.WzSheet) window.WzSheet.open(d); else if (d.showModal && !d.open) d.showModal(); }
function closeSheet(id) { const d = el(id); if (window.WzSheet) window.WzSheet.close(d); else if (d.open && d.close) d.close(); }
function pill(id, text, kind) { const p = el(id); p.className = 'wz-pill ' + kind; p.textContent = text; }
const LABEL = { ok: 'Working', untested: 'Not tested yet', blocked: 'Blocked by YouTube', failed: 'Last test failed' };
const RELAY = { verified: ['Paired', 'ok'], imported_unverified: ['Not tested', 'off'], provider_blocked: ['Blocked', 'warn'], failed: ['Failed', 'warn'], not_configured: ['Not set up', 'off'] };
function render(s) {
  const p = s.playback || {};
  const state = el('state');
  state.textContent = LABEL[p.state] || 'Unknown';
  const label = LABEL[p.state] || 'Unknown';
  el('state-via').textContent = String(p.message || '').replace(new RegExp('^' + label + '\\.\\s*'), '');
  state.dataset.state = p.state || '';
  const a = s.account_status || {};
  const labels = { signed_out: 'Signed out. Public catalog only.', unverified: 'Account credentials found, not verified. Tap Verify connection.', connected: 'Account connected: library access verified.', needs_attention: 'Account needs attention. Verify again or disconnect and reconnect.' };
  el('via').textContent = labels[a.state] || 'Account status unknown.';
  el('connect').disabled = !a.configured || a.state !== 'signed_out';
  el('connect').hidden = !!a.state && a.state !== 'signed_out';
  if (!a.configured && a.state === 'signed_out') el('via').textContent += ' Owner must provision oauth-client.json privately first (see README).';
  const signedIn = !!a.state && a.state !== 'signed_out';
  el('account-count').textContent = signedIn ? '1' : '0';
  el('account-name').textContent = signedIn ? 'YouTube account' : 'No account connected';
  pill('account-pill', a.state === 'connected' ? 'Active' : a.state === 'unverified' ? 'Not verified' : a.state === 'needs_attention' ? 'Needs attention' : 'Signed out', a.state === 'connected' ? 'ok' : signedIn ? 'warn' : 'off');
  el('relay-on').hidden = !s.relay;
  el('relay-form').hidden = s.relay;
  const relay = s.relay_status || {};
  el('relay-state').textContent = relay.message || 'Relay status unknown.';
  el('relay-sheet-state').textContent = relay.message || 'Relay status unknown.';
  const [rt, rk] = RELAY[relay.state] || (s.relay ? ['Saved', 'ok'] : RELAY.not_configured);
  pill('relay-pill', rt, rk);
  el('save').disabled = !relay.import_enabled;
  el('relay-test').disabled = !s.relay || !relay.import_enabled;
  el('relay-off').disabled = !relay.import_enabled;
  el('relay-setup').hidden = !!relay.import_enabled;
  const attention = p.state === 'blocked' || p.state === 'failed' || a.state === 'needs_attention';
  pill('pill', attention ? 'Needs attention' : p.state === 'ok' ? 'Connected' : 'Not connected', attention ? 'warn' : p.state === 'ok' ? 'ok' : 'off');
  el('about').textContent = [s.version && 'Version ' + s.version, s.contract, s.playback?.via && 'Plays through ' + s.playback.via].filter(Boolean).join(' · ');
}
async function run(fn, busy) {
  const b = el(busy); b.disabled = true; say('');
  let next;
  try { next = await fn(); } catch (e) { say(e.message); } finally { b.disabled = false; }
  if (next) render(next); // render authoritative disabled states after clearing busy
}
el('test').onclick = () => { el('state').textContent = 'Testing…'; el('state-via').textContent = 'This downloads one song.'; run(() => api('api/test', {}), 'test'); };
async function relayAction(path, body) {
  try {
    if (!window.isSecureContext || location.protocol !== 'https:') throw new Error('Use your private authenticated Wizard HTTPS page for relay operations.');
    const key = el('relay-key').value;
    if (!key) throw new Error('Enter your privately provisioned import authorization key. Never send it in chat.');
    return await api(path, body, key);
  }
  finally { el('relay-key').value = ''; }
}
el('relay-form').onsubmit = async (e) => {
  e.preventDefault();
  await run(async () => {
    const file = el('relay-file').files[0];
    try {
      if (!file || file.size > 16384) throw new Error('Choose a private pairing JSON file no larger than 16 KiB.');
      if (!el('relay-confirm').checked) throw new Error('Confirm the home Tailscale device first.');
      let pairing;
      try { pairing = JSON.parse(await file.text()); }
      catch { throw new Error('Invalid pairing JSON file. Export a new file from your home relay.'); }
      return await relayAction('api/relay/import', {pairing, confirm_home_device: true});
    } finally { el('relay-file').value = ''; el('relay-confirm').checked = false; el('relay-key').value = ''; }
  }, 'save');
};
el('relay-off').onclick = () => run(() => relayAction('api/relay', { relay: '' }), 'relay-off');
el('relay-test').onclick = () => run(() => relayAction('api/relay/test', {}), 'relay-test');
el('connect').onclick = async () => {
  el('connect').disabled = true; say('');
  try {
    const a = await api('api/account/start', {});
    el('google-link').href = a.verification_url;
    el('user-code').textContent = a.user_code;
    el('auth-progress').textContent = 'Authorization expires in ' + a.expires_in + ' seconds.';
    el('auth-step').hidden = false;
    openSheet('auth-sheet');
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
      closeSheet('auth-sheet');
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
  closeSheet('auth-sheet'); closeSheet('account-sheet');
  return s;
}, 'disconnect');
el('account-row').onclick = () => openSheet('account-sheet');
el('relay-row').onclick = () => openSheet('relay-sheet');
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
// Root Back leaves the app; wizard-sheet.js (loaded after this file) chains this and closes an open sheet first.
window.__wizardCloseOverlay = () => false;
api('api/status').then(render).catch((e) => say(e.message));
