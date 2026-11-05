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
  const node = {textContent: '', value: '', children: [], classList: {add() {}, remove() {}, toggle() {}}, focus() {},
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
const context = vm.createContext({document, Date, setTimeout() {},
  BX24: {init() {}, getAuth() { return {access_token: 'synthetic-ui-token', refresh_token: 'never-forward-refresh'}; }},
  fetch(url, options) { calls.push({url, options}); return Promise.resolve({ok: true, json: () => Promise.resolve(nextReply)}); },
});
vm.runInContext(script, context); // Parses and initializes definitions only; BX24.init is stubbed.
async function main() {
  context.memberId = 'test-portal';
  nextReply = {state: 'ready', enabled: true, providers: [{state: 'pending'}], uncertain: []};
  await context.prepareCrm();
  assert.ok(document.getElementById('crmNotice').textContent.includes('Обновляем'));
  context.renderCrmDiagnostics({providers: [{state: 'ready'}], uncertain: []});
  assert.equal(document.getElementById('crmNotice').textContent, '');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-ui-token');
  assert.ok(!JSON.stringify(calls).includes('never-forward-refresh'));
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
  console.log('CRM UI checks passed: preparation, notice recovery, authorization, rename');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
