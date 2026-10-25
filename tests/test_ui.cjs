'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup() {
  const elements = {};
  const requests = [];
  const messages = [];
  const s = { account: false, account_status: {state:'signed_out',configured:true}, playback:{state:'untested'}, relay:false };
  const context = {
    document: {getElementById(id) { return elements[id] ||= {dataset:{},hidden:false,value:'',textContent:'',removeAttribute(k){delete this[k];}, focus(){},select(){}}; }},
    fetch: async (path, options) => { requests.push([path,options]); let body = s;
      if(path.endsWith('/start')) body={verification_url:'https://www.google.com/device', user_code:'OWNER-CODE',expires_in:900};
      if(path.endsWith('/poll')) body={state:'complete',status:{...s,account:true,account_status:{state:'connected',configured:true}}};
      return {ok:true,json:async()=>body}; },
    navigator:{clipboard:{writeText:async text=>{context.copied=text;}}},
    location:{origin:'https://wizard.example'},
    window:{parent:{postMessage:(...args)=>messages.push(args)}}
  };
  vm.runInNewContext(fs.readFileSync('web/app.js','utf8'),context);
  return {context,e:elements,requests,messages};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));
test('account screen distinguishes consent and verified connection, disconnect clears code',async()=>{
  const {e,requests}=setup(); await settle();
  assert.match(e.via.textContent,/Signed out/);
  await e.connect.onclick(); await settle();
  assert.equal(e['user-code'].textContent,'OWNER-CODE');
  assert.equal(e['google-link'].href,'https://www.google.com/device');
  assert.match(e.via.textContent,/Signed out/);
  await e.finish.onclick();
  assert.match(e.via.textContent,/connected/);
  assert.equal(e['auth-step'].hidden,true);
  assert.equal(e['user-code'].textContent,'');
  await e.disconnect.onclick();
  assert.ok(requests.some(([p])=>p==='api/account/disconnect'));
});
test('setup help never sends a message; shell navigation and copy are explicit',async()=>{
  const {context,e,requests,messages}=setup(); await settle();
  e['ask-help'].onclick();
  assert.match(e['help-request'].value,/Mac, Windows or Linux/);
  assert.equal(requests.length,1);
  assert.equal(messages.length,0);
  await e['copy-help'].onclick();
  assert.equal(context.copied,e['help-request'].value);
  e['open-agents'].onclick();
  assert.equal(messages[0][0].type,'wizard:open-app');
  assert.equal(messages[0][0].id,'agents');
  assert.equal(messages[0][1],'https://wizard.example');
  assert.match(e.status.textContent,/Asked the shell/);
});
