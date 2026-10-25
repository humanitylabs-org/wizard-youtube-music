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
    location:{origin:'https://wizard.example',protocol:'https:'},
    window:{isSecureContext:true,parent:{postMessage:(...args)=>messages.push(args)}}
  };
  for (const match of fs.readFileSync('web/index.html','utf8').matchAll(/id="([^"]+)"/g)) context.document.getElementById(match[1]);
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
test('private file import authenticates without displaying secrets and never auto-probes',async()=>{
  const {e,requests}=setup(); await settle();
  const pairing={schema:'wizard-home-relay-pairing-v1',password:'fixture-private-secret'};
  e['relay-file'].files=[{size:200,text:async()=>JSON.stringify(pairing)}];
  e['relay-key'].value='fixture-import-authorization';
  e['relay-confirm'].checked=true;
  await e['relay-form'].onsubmit({preventDefault(){}});
  const [path, options]=requests.at(-1);
  assert.equal(path,'api/relay/import');
  assert.equal(options.headers.Authorization,'Bearer fixture-import-authorization');
  assert.deepEqual(JSON.parse(options.body),{pairing,confirm_home_device:true});
  assert.equal(requests.length,2);
  assert.equal(e['relay-file'].value,'');
  assert.equal(e['relay-key'].value,'');
  assert.equal(e['relay-confirm'].checked,false);
  assert.doesNotMatch(e.status.textContent,/fixture/);
});
test('malformed/oversized files, missing confirmation and insecure pages never upload',async()=>{
  for(const kind of ['malformed','oversized','unconfirmed','insecure']) {
    const {context,e,requests}=setup(); await settle();
    e['relay-file'].files=[{size:kind==='oversized'?20000:200,text:async()=>kind==='malformed'?'{"password":"secret-malformed",':'{}'}];
    e['relay-key'].value='authorization-fixture';
    e['relay-confirm'].checked=kind!=='unconfirmed';
    if(kind==='insecure') context.window.isSecureContext=false;
    await e['relay-form'].onsubmit({preventDefault(){}});
    assert.equal(requests.length,1);
    assert.equal(e['relay-key'].value,'');
    assert.doesNotMatch(e.status.textContent,/secret-malformed|authorization-fixture/);
  }
});
test('relay connectivity and removal are explicit authenticated operations, not playback tests',async()=>{
  const {e,requests}=setup(); await settle();
  e['relay-key'].value='private-authorization';
  await e['relay-test'].onclick(); await settle();
  assert.equal(requests.at(-1)[0],'api/relay/test');
  assert.equal(e['relay-key'].value,'');
  e['relay-key'].value='private-authorization';
  await e['relay-off'].onclick(); await settle();
  assert.equal(requests.at(-1)[0],'api/relay');
  assert.deepEqual(JSON.parse(requests.at(-1)[1].body),{relay:''});
  assert.ok(!requests.some(([p])=>p==='api/test'));
});
