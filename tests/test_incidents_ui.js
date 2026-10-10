const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

// The renderers only need text nodes and append/replaceChildren; no browser or
// application startup is needed to verify their user-facing incident evidence.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join('\n'); }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.text = ''; this.children = []; }
}
const context = vm.createContext({document: {createElement: tag => new Element(tag)}});
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf("$('login-form').addEventListener")), context);

function render(details) {
  const node = new Element('div');
  context.renderIncidents(node, [{monitor_name:'Example', severity:'degraded',
    started_at:'2026-10-10T00:00:00Z', ended_at:'2026-10-10T00:01:00Z', ended_reason:'recovered', details}]);
  return node;
}
function evidence(changes = {}) {
  return {check_count:2, sources:['application'], core_states:['available'], components:{},
    reasons:[], response_ms:{min:12, max:18}, ...changes};
}

test('optional outage displays core availability, exact component latency, and safe text', () => {
  const node = render(evidence({components:{gameApi:{roles:['noncritical'], statuses:['down'],
    reasons:['Request timed out', '<script>example</script>'], latency_ms:{min:2500, max:3000}}}}));
  assert.match(node.textContent, /Core functionality remained available/);
  assert.match(node.textContent, /gameApi · Down · non-critical/);
  assert.match(node.textContent, /Latency while affected: 2500–3000 ms/);
  assert.match(node.textContent, /Application latency during incident: 12–18 ms/);
  assert.match(node.textContent, /Observation ended: recovered/);
  const componentLine = node.children[0].children[1].children[1].children[1];
  assert.equal(componentLine.tag, 'div');
  assert.equal(componentLine.children.length, 0);
  assert.match(componentLine.textContent, /<script>example<\/script>/);
});

test('critical and passive observations retain their distinct meaning', () => {
  const node = render(evidence({core_states:['available', 'unavailable'], components:{
    database:{roles:['critical'], statuses:['down'], reasons:['Database query failed'], latency_ms:null},
    feature:{roles:['passive'], statuses:['down'], reasons:[], latency_ms:null}}}));
  assert.match(node.textContent, /Core functionality was unavailable in at least one/);
  assert.match(node.textContent, /database · Down · critical · Database query failed/);
  assert.match(node.textContent, /passive observation; does not determine availability/);
  assert.match(node.textContent, /Latency while affected: not recorded/);
});

test('endpoint-only and missing evidence do not imply an application component diagnosis', () => {
  const endpoint = render(evidence({sources:['http'], core_states:[], response_ms:null, reasons:['Request timed out']}));
  assert.match(endpoint.textContent, /Internal component health was not measured/);
  assert.match(endpoint.textContent, /Check reason: Request timed out/);
  assert.match(endpoint.textContent, /Check response time during incident: not recorded/);
  assert.doesNotMatch(endpoint.textContent, /Core functionality/);
  assert.match(render(null).textContent, /Cause, component impact, and latency were not recorded/);
  assert.match(render(evidence({check_count:0})).textContent, /were not recorded/);
});
