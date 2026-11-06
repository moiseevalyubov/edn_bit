// Dependency-free UI behavior checks. No browser, application server, or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/routers/settings_page.py'), 'utf8');
const script = [...source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(value => value.includes('BX24.init'));
assert.ok(script);
const elements = new Map();
function element() {
  const node = {textContent: '', value: '', children: [], style: {}, outerHTML: '<tr id="emptyRow"></tr>', classList: {add() {}, remove() {}, toggle() {}}, focus() {},
    appendChild(child) { this.children.push(child); }};
  Object.defineProperty(node, 'id', {set(value) { elements.set(value, node); }});
  return node;
}
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  createElement: element,
};
const calls = [];
let nextReply = {};
let initialize;
const context = vm.createContext({document, Date, setTimeout() {},
  BX24: {init(callback) { initialize = callback; }, getAuth() { return {member_id: 'test-portal', access_token: 'synthetic-ui-token', refresh_token: 'never-forward-refresh'}; }},
  fetch(url, options) { calls.push({url, options}); return Promise.resolve({ok: true, json: () => Promise.resolve(nextReply)}); },
});
vm.runInContext(script, context); // Parses and initializes definitions only; BX24.init is stubbed.
async function main() {
  context.memberId = 'test-portal';
  nextReply = {state: 'ready', enabled: true, providers: [{state: 'pending'}], uncertain: []};
  await context.prepareCrm();
  assert.equal(document.getElementById('crmNotice').textContent, ''); // Background sync stays silent.
  context.renderCrmDiagnostics({providers: [{state: 'error'}], uncertain: []});
  assert.ok(document.getElementById('crmNotice').textContent.includes('Не удалось'));
  context.renderCrmDiagnostics({providers: [{state: 'ready'}], uncertain: []});
  assert.equal(document.getElementById('crmNotice').textContent, '');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-ui-token');
  assert.ok(!JSON.stringify(calls).includes('never-forward-refresh'));
  context.crmAdmin = false;
  context.renderChannels([{id: 9, name: 'Existing name', connected_at: '2026-11-05', is_active: true, channel_type: 'MAX'}]);
  assert.ok(document.getElementById('channelsList').innerHTML.includes('Переименовать'));
  assert.ok(document.getElementById('channelsList').innerHTML.includes(' disabled'));
  context.crmAdmin = true;
  context.renderChannels([{id: 9, name: 'Existing name', connected_at: '2026-11-05', is_active: true, channel_type: 'MAX'}]);
  assert.ok(!document.getElementById('channelsList').innerHTML.includes(' disabled'));
  let resolvePreparation;
  let renders = 0;
  calls.length = 0;
  context.renderChannels = () => { renders++; assert.equal(context.crmAdmin, true); };
  context.crmAdmin = false;
  context.BX24.callMethod = (method, params, callback) => callback({error() { return false; }});
  context.fetch = (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/crm/prepare') return new Promise(resolve => { resolvePreparation = resolve; });
    const data = url.startsWith('/api/channels?') ? [] : {};
    return Promise.resolve({ok: true, json: () => Promise.resolve(data)});
  };
  initialize();
  for (let i = 0; i < 12; i++) await Promise.resolve();
  assert.equal(renders, 0, 'No intermediate channel list before permissions are checked');
  assert.equal(calls.filter(call => call.url.startsWith('/api/channels?')).length, 1);
  resolvePreparation({ok: true, json: () => Promise.resolve({state: 'ready', enabled: true, providers: []})});
  for (let i = 0; i < 15; i++) await Promise.resolve();
  assert.equal(renders, 1, 'The initial channel list renders once');
  assert.equal(document.getElementById('loading').style.display, 'none');
  for (const reply of [
    {ok: true, data: {state: 'disabled', enabled: false}},
    {ok: false, data: {detail: 'Preparation unavailable'}},
  ]) {
    renders = 0;
    context.crmAdmin = false;
    context.renderChannels = () => { renders++; assert.equal(context.crmAdmin, false); };
    context.fetch = (url, options = {}) => Promise.resolve({
      ok: url === '/api/crm/prepare' ? reply.ok : true,
      json: () => Promise.resolve(url === '/api/crm/prepare' ? reply.data : []),
    });
    initialize();
    for (let i = 0; i < 25; i++) await Promise.resolve();
    assert.equal(renders, 1, 'Legacy mode and preparation failure must still show the form once');
    if (!reply.ok) assert.equal(document.getElementById('crmNotice').textContent, 'Preparation unavailable');
  }
  context.fetch = (url, options) => {
    calls.push({url, options});
    return Promise.resolve({ok: true, json: () => Promise.resolve({})});
  };
  context.showModal = options => { context.modalOptions = options; };
  context.loadChannels = () => {};
  context.refreshCrmDiagnostics = () => {};
  context.channelNames[9] = 'Existing name';
  context.renameChannel(9);
  assert.equal(document.getElementById('renameChannelInput').value, 'Existing name');
  document.getElementById('renameChannelInput').value = ' New name ';
  context.modalOptions.onOk();
  await Promise.resolve();
  const renamed = calls.find(call => call.options.method === 'PATCH');
  assert.equal(renamed.url, '/api/channels/9');
  assert.equal(JSON.parse(renamed.options.body).name, 'New name');
  assert.equal(renamed.options.headers.Authorization, 'Bearer synthetic-ui-token');
  console.log('CRM UI checks passed: silent background sync, visible errors, single initial render, authorization, rename');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
