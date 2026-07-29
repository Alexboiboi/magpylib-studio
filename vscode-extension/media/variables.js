const vscodeApi = acquireVsCodeApi();
const listEl = document.getElementById('list');
const emptyEl = document.getElementById('empty');
const statusEl = document.getElementById('status');
let nextReqId = 1;
const pending = new Map();
// A rebuild replaces the slider element, so it must not happen while a
// thumb is held: edits elsewhere broadcast back here, and the broadcast
// is debounced, which is exactly long enough to land mid-drag.
let dragging = false;
let missedRefresh = false;

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const reqId = nextReqId++;
    pending.set(reqId, { resolve, reject });
    vscodeApi.postMessage({ type: 'rpcRequest', reqId, method, params });
  });
}

function short(value) {
  if (value === null || value === undefined) return '?';
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(6)));
}

/** Typed text -> document value: a number if it is one, else "=expr". */
function asValue(text) {
  const trimmed = String(text).trim();
  if (!trimmed) return 0;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : '=' + trimmed;
}

function commit(name, value) {
  statusEl.textContent = '';
  rpc('set_variable', { name, value })
    .then((res) => {
      if (res && res.ok === false) statusEl.textContent = res.error;
      return load();
    })
    .catch((err) => { statusEl.textContent = String(err); });
}

function button(glyph, title, action, name) {
  const el = document.createElement('button');
  el.textContent = glyph;
  el.title = title;
  el.addEventListener('click', () =>
    vscodeApi.postMessage({ type: 'action', action, name }));
  return el;
}

/** Read off the engine's own allow-list, so it cannot go stale. */
async function loadHelp() {
  const help = await rpc('expression_help', {});
  const body = document.getElementById('helpBody');
  body.innerHTML = '';
  const list = document.createElement('dl');
  for (const [name, value] of [
    ['operators', help.operators.join(' ')],
    ['functions', help.functions.join(' ')],
    ['constants', help.constants.join(' ')],
    ['for example', help.examples.join('   ')],
  ]) {
    const dt = document.createElement('dt');
    dt.textContent = name;
    const dd = document.createElement('dd');
    dd.textContent = value;
    list.append(dt, dd);
  }
  const note = document.createElement('div');
  note.style.opacity = '0.7';
  note.style.marginTop = '6px';
  note.textContent = help.note;
  body.append(list, note);
}

async function load() {
  if (dragging) {
    missedRefresh = true;
    return;
  }
  const { variables } = await rpc('get_variables', {});
  listEl.innerHTML = '';
  emptyEl.hidden = variables.length > 0;
  for (const v of variables) {
    const row = document.createElement('div');
    row.className = 'row';

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = v.name;
    const isExpression = typeof v.expression === 'string';
    name.title = isExpression
      ? v.name + ' = ' + v.expression.slice(1) + ', currently ' + short(v.value)
      : v.name;

    // Soft bounds win: they are the range worth dragging through. A
    // variable defined by an expression is not draggable - its value
    // belongs to the expression, not to the slider.
    const b = v.bounds || {};
    const low = b.soft_min !== undefined ? b.soft_min : b.min;
    const high = b.soft_max !== undefined ? b.soft_max : b.max;
    const slidable = !isExpression && low !== undefined && high !== undefined
      && low < high;

    const text = document.createElement('input');
    text.type = 'text';
    text.spellcheck = false;
    text.value = isExpression ? v.expression.slice(1) : short(v.value);
    if (b.integer) name.title += ' — whole numbers only';
    if (isExpression) { text.classList.add('expr'); text.title = 'currently ' + short(v.value); }
    text.addEventListener('change', () => commit(v.name, asValue(text.value)));

    const slot = document.createElement('div');
    if (slidable) {
      const slider = document.createElement('input');
      slider.type = 'range';
      slider.min = low;
      slider.max = high;
      // a count has no values between its values
      slider.step = b.integer ? 1 : (high - low) / 100;
      slider.value = v.value;
      slider.title = short(low) + ' .. ' + short(high);
      // live text while dragging, one edit when released
      slider.addEventListener('pointerdown', () => { dragging = true; });
      slider.addEventListener('input', () => { text.value = short(parseFloat(slider.value)); });
      slider.addEventListener('change', () => {
        dragging = false;
        commit(v.name, parseFloat(slider.value));
      });
      slider.addEventListener('pointerup', () => {
        dragging = false;
        if (missedRefresh) { missedRefresh = false; load(); }
      });
      slot.appendChild(slider);
    } else if (!isExpression) {
      const hint = document.createElement('span');
      hint.style.opacity = '0.5';
      hint.style.fontSize = '10px';
      hint.textContent = 'no range';
      hint.title = 'Give it a range to get a slider';
      slot.appendChild(hint);
    }

    const acts = document.createElement('div');
    acts.className = 'acts';
    acts.append(
      button('⋯', 'Set bounds…', 'bounds', v.name),
      button('✕', 'Remove ' + v.name, 'remove', v.name),
    );
    row.append(name, slot, text, acts);
    listEl.appendChild(row);

    // hard limits worth seeing when they differ from the slider's span
    const hard = b.min !== undefined || b.max !== undefined;
    if (hard && (b.soft_min !== undefined || b.soft_max !== undefined)) {
      const note = document.createElement('div');
      note.className = 'range';
      note.textContent = 'allowed ' +
        (b.min === undefined ? '−∞' : short(b.min)) + ' .. ' +
        (b.max === undefined ? '∞' : short(b.max));
      listEl.appendChild(note);
    }
  }
}

window.addEventListener('message', (event) => {
  const message = event.data;
  if (message.type === 'rpcResult' || message.type === 'rpcError') {
    const entry = pending.get(message.reqId);
    if (!entry) return;
    pending.delete(message.reqId);
    if (message.type === 'rpcResult') entry.resolve(message.result);
    else entry.reject(new Error(message.method + ': ' + message.error));
  } else if (message.type === 'refresh') {
    load().catch((err) => { statusEl.textContent = String(err); });
  } else if (message.type === 'help') {
    loadHelp().catch((err) => { statusEl.textContent = String(err); });
  } else {
    // A message the host sends and this end does not handle is a broken
    // contract, not a no-op: it is how "what can go in a value" stayed
    // empty. Say so where it can be seen.
    statusEl.textContent = 'unhandled message: ' + message.type;
  }
});

vscodeApi.postMessage({ type: 'ready' });
