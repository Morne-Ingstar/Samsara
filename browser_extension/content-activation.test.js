/* 1031b: exercise the actual content script, without a browser or network. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function harness() {
  let listener, next = 0;
  const fired = [];
  const fields = [{ id: 'to', name: 'To', type: 'text', value: 'synthetic-recipient',
    checked: false, textContent: '', getAttribute: () => '' }];
  const body = { innerHTML: '<button>Send</button>' };
  const leaf = { isConnected: true, disabled: false,
    scrollIntoView() {}, dispatchEvent(event) { fired.push(event.type); } };
  const parent = { appendChild(el) { el.parentNode = this; }, removeChild() {} };
  const document = { body, documentElement: parent, querySelectorAll: () => fields,
    addEventListener() {}, removeEventListener() {},
    createElement() { return { style: {}, appendChild() {},
      attachShadow() { return { querySelectorAll: () => [], appendChild() {} }; } }; } };
  const window = { innerWidth: 800, innerHeight: 600, addEventListener() {}, removeEventListener() {},
    SamsaraCore: { discoverCandidates: () => [{ el: leaf, rect: {}, kind: 'button' }],
      prioritize: value => value, rectOf: () => ({}), computeLabelPosition: () => ({left: 0, top: 0}) } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'content.js'), 'utf8'), {
    window, document, location: { href: 'https://synthetic.invalid/' },
    crypto: { randomUUID: () => `synthetic-session-${++next}` },
    chrome: { runtime: { onMessage: { addListener(fn) { listener = fn; } }, sendMessage() {} } },
    MouseEvent: class { constructor(type) { this.type = type; } }, requestAnimationFrame() {},
  });
  function message(msg) { let response; listener(msg, {}, r => { response = r; }); return response; }
  function show() { return message({type: 'show_hints'}).hints[0].binding; }
  function select(binding) { return message({type: 'select', number: 1, action: 'click', binding}); }
  return { fired, leaf, fields, body, message, show, select };
}

test('one bound selection sends one click and cannot be reused', () => {
  const h = harness(), binding = h.show();
  assert.equal(h.select(binding).ok, true);
  assert.equal(h.select(binding).ok, false);
  assert.deepEqual(h.fired, ['mousedown', 'mouseup', 'click']);
});

for (const change of ['recipient', 'page', 'detached', 'disabled', 'new session', 'missing binding', 'scroll race']) {
  test(`changed ${change} never activates a numbered control`, () => {
    const h = harness();
    let binding = h.show();
    if (change === 'recipient') h.fields[0].value = 'different-synthetic-recipient';
    if (change === 'page') h.body.innerHTML = '<button>Pay</button>';
    if (change === 'detached') h.leaf.isConnected = false;
    if (change === 'disabled') h.leaf.disabled = true;
    if (change === 'new session') h.show();
    if (change === 'missing binding') binding = null;
    if (change === 'scroll race') h.leaf.scrollIntoView = () => { h.fields[0].value = 'changed'; };
    assert.equal(h.select(binding).ok, false);
    assert.deepEqual(h.fired, []);
  });
}
