const vscodeApi = acquireVsCodeApi();
const headerEl = document.getElementById("header");
const stepEl = document.getElementById("step");
const propsEl = document.getElementById("props");
const transformEl = document.getElementById("transform");
const paramsEl = document.getElementById("params");
const emptyEl = document.getElementById("empty");
const statusEl = document.getElementById("status");
const filterEl = document.getElementById("filter");
let objectId;
// set to the source id when the selection is a generated copy: it can be
// looked at, but nothing can be written to it
let generatedFrom = null;
let schema;
let values = { set: {}, resolved: {} };
let nextReqId = 1;
const pending = new Map();

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const reqId = nextReqId++;
    pending.set(reqId, { resolve, reject });
    vscodeApi.postMessage({ type: "rpcRequest", reqId, method, params });
  });
}

function leafPaths(props, prefix, out) {
  for (const [name, spec] of Object.entries(props)) {
    const path = prefix ? prefix + "." + name : name;
    if (spec.properties) leafPaths(spec.properties, path, out);
    else out.push([path, spec]);
  }
  return out;
}

/** Generated copies exist only as long as the step that made them. */
function refuseIfGenerated() {
  if (!generatedFrom) return false;
  statusEl.textContent =
    "This is a generated copy. Edit " +
    generatedFrom +
    ", its pattern step, or the variables it is written in terms of.";
  return true;
}

async function applyEdit(path, value) {
  if (refuseIfGenerated()) return;
  statusEl.textContent = "";
  const res = await rpc("apply_edit", { object_id: objectId, path, value });
  if (!res.ok) {
    statusEl.textContent = res.error;
  }
  await reloadValues();
}

async function resetPath(path) {
  if (refuseIfGenerated()) return;
  statusEl.textContent = "";
  const res = await rpc("reset_style", { object_id: objectId, path });
  if (!res.ok) {
    statusEl.textContent = res.error;
  }
  await reloadValues();
}

function makeWidget(path, spec, value) {
  const wrap = document.createElement("div");
  wrap.className = "widget";
  const types = [].concat(spec.type || []);
  const enums = (spec.enum || []).filter((v) => typeof v === "string");

  if (spec.format === "color") {
    const text = document.createElement("input");
    text.type = "text";
    text.value = value ?? "";
    text.placeholder = "default";
    text.addEventListener("change", () => {
      if (text.value) applyEdit(path, text.value);
    });
    const pick = document.createElement("input");
    pick.type = "color";
    if (/^#[0-9a-fA-F]{6}$/.test(value || "")) pick.value = value;
    pick.addEventListener("change", () => applyEdit(path, pick.value));
    wrap.append(text, pick);
  } else if (enums.length) {
    const sel = document.createElement("select");
    sel.append(new Option("(default)", ""));
    for (const opt of enums) sel.append(new Option(opt, opt));
    sel.value = typeof value === "string" ? value : "";
    sel.addEventListener("change", () => {
      if (sel.value) applyEdit(path, sel.value);
      else if (path in values.set) resetPath(path);
    });
    wrap.append(sel);
  } else if (types.includes("boolean")) {
    const sel = document.createElement("select");
    sel.append(
      new Option("(default)", ""),
      new Option("true", "true"),
      new Option("false", "false"),
    );
    sel.value = value === true ? "true" : value === false ? "false" : "";
    sel.addEventListener("change", () => {
      if (sel.value) applyEdit(path, sel.value === "true");
      else if (path in values.set) resetPath(path);
    });
    wrap.append(sel);
  } else if (types.includes("number")) {
    const num = document.createElement("input");
    num.type = "number";
    num.step = "any";
    num.className = "num";
    if (value !== null && value !== undefined) num.value = value;
    num.addEventListener("change", () => {
      if (num.value !== "") applyEdit(path, parseFloat(num.value));
      else if (path in values.set) resetPath(path);
    });
    if (spec.minimum !== undefined && spec.maximum !== undefined) {
      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = spec.minimum;
      slider.max = spec.maximum;
      slider.step = (spec.maximum - spec.minimum) / 100;
      if (value !== null && value !== undefined) slider.value = value;
      slider.addEventListener("input", () => {
        num.value = slider.value;
      });
      slider.addEventListener("change", () =>
        applyEdit(path, parseFloat(slider.value)),
      );
      wrap.append(slider);
    }
    wrap.append(num);
  } else if (types.includes("string")) {
    const text = document.createElement("input");
    text.type = "text";
    text.value = value ?? "";
    text.placeholder = "default";
    text.addEventListener("change", () => {
      if (text.value) applyEdit(path, text.value);
      else if (path in values.set) resetPath(path);
    });
    wrap.append(text);
  } else {
    return null; // free-form specs (model3d.data, path.frames): not editable here
  }
  return wrap;
}

function render() {
  const openGroups = new Set(
    Array.from(propsEl.querySelectorAll("details[open]")).map(
      (d) => d.dataset.group,
    ),
  );
  propsEl.innerHTML = "";
  if (!schema || !objectId) return;
  const filter = filterEl.value.trim().toLowerCase();
  for (const [group, spec] of Object.entries(schema.properties)) {
    const leaves = spec.properties
      ? leafPaths(spec.properties, group, [])
      : [[group, spec]];
    const rows = [];
    for (const [path, leafSpec] of leaves) {
      if (filter && !path.toLowerCase().includes(filter)) continue;
      const widget = makeWidget(path, leafSpec, values.resolved[path]);
      if (!widget) continue;
      const row = document.createElement("div");
      row.className = "row" + (path in values.set ? " set" : "");
      const label = document.createElement("label");
      label.textContent = path.startsWith(group + ".")
        ? path.slice(group.length + 1)
        : path;
      label.title =
        path + (leafSpec.description ? " — " + leafSpec.description : "");
      const reset = document.createElement("button");
      reset.className = "reset";
      reset.textContent = "↺";
      reset.title = "Reset to default";
      reset.addEventListener("click", () => resetPath(path));
      row.append(label, widget, reset);
      rows.push(row);
    }
    if (!rows.length) continue;
    const details = document.createElement("details");
    details.dataset.group = group;
    if (filter || openGroups.has(group) || !spec.properties)
      details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = group;
    details.append(summary, ...rows);
    propsEl.appendChild(details);
  }
}

// --- step section: the selected construction step's own values --------
//
// Selecting a step in the Scene tree shows what it did, right above the
// object it did it to: the property grid of a CAD history, rather than a
// dialog you have to open and close.
let stepId = null;

const STEP_SKIP = [
  "id",
  "op",
  "target",
  "type",
  "children",
  "style",
  "hidden_style",
  "visible",
  "parent",
];

// Step fields that hold a name from a fixed set. An axis may also be
// given as a vector, which arrives as an array and gets the vector row
// instead — this is only for when it arrived as one of these.
const STEP_CHOICES = {
  axis: ["x", "y", "z"],
  // exactly the engine's _MIRROR_NORMALS keys — a test asserts that, since
  // a dropdown offering a plane the engine has never heard of is worse
  // than a text box ("zx" was in this list and produced a KeyError)
  plane: ["xy", "xz", "yz"],
};

// An event's own geometric vectors are points and directions, so their
// parts are x, y and z. A create step's parameters are not listed here:
// they are the object's, and the engine says what their parts are called
// (a dimension's depend on the shape, so it has none).
const STEP_COMPONENTS = {
  anchor: ["x", "y", "z"],
  step: ["x", "y", "z"],
  normal: ["x", "y", "z"],
  position: ["x", "y", "z"],
};

async function loadStep() {
  stepEl.innerHTML = "";
  if (!stepId) return;
  const [listed, document_] = await Promise.all([
    rpc("get_events", {}),
    rpc("to_dict", {}),
  ]);
  const shown = listed.events.find((e) => e.id === stepId);
  const stored = (document_.events || []).find((e) => e.id === stepId);
  if (!shown || !stored) {
    stepId = null;
    return;
  }

  const box = document.createElement("details");
  box.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "step — " + shown.label;
  summary.title = shown.source;
  box.appendChild(summary);
  if (shown.error) {
    const why = document.createElement("div");
    why.className = "hint";
    why.style.color = "var(--vscode-errorForeground)";
    why.textContent = shown.error;
    box.appendChild(why);
  }

  // a create step carries the object's constructor parameters; every
  // other kind carries its own arguments
  const isCreate = shown.op === "create";
  const values = isCreate ? stored.params || {} : stored;
  // For a create step the fields are the object's own parameters, so the
  // engine already says what each one's parts are called and what unit it
  // is in — read that rather than keeping a second copy of the table here.
  const described = {};
  if (isCreate) {
    for (const p of await rpc("get_params", { object_id: shown.target })) {
      described[p.name] = p;
    }
  }
  const commit = (name, value) => {
    statusEl.textContent = "";
    const changes = isCreate
      ? { params: Object.assign({}, values, { [name]: value }) }
      : { [name]: value };
    rpc("edit_event", { event_id: stepId, changes })
      .then((res) => {
        if (res && res.ok === false) statusEl.textContent = res.error;
        else if (res && res.broken && res.broken.length)
          statusEl.textContent =
            res.broken.length +
            " later step(s) no longer apply — undo to put them back";
        return reloadAll();
      })
      .catch((err) => {
        statusEl.textContent = String(err);
      });
  };

  for (const name of Object.keys(values)) {
    if (STEP_SKIP.includes(name)) continue;
    const value = values[name];
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    label.append(document.createTextNode(name + " "));
    label.appendChild(unitTag(described[name] && described[name].unit));
    if (described[name]) label.title = described[name].doc;
    const wrap = document.createElement("div");
    wrap.className = "widget";
    if (Array.isArray(value) && !Array.isArray(value[0])) {
      wrap.style.display = "block";
      const resolved = value.map((v) => (typeof v === "string" ? 0 : v));
      const parts =
        (described[name] && described[name].components) ||
        STEP_COMPONENTS[name] ||
        value.map((_, i) => String(i + 1));
      wrap.appendChild(
        vecRow(parts, resolved, (v) => commit(name, v), undefined, value),
      );
    } else if (STEP_CHOICES[name] && STEP_CHOICES[name].includes(value)) {
      // a field whose values are named and countable: pick, don't type
      const sel = document.createElement("select");
      for (const option of STEP_CHOICES[name])
        sel.append(new Option(option, option));
      sel.value = value;
      sel.addEventListener("change", () => commit(name, sel.value));
      wrap.appendChild(sel);
    } else if (typeof value === "number" || typeof value === "string") {
      // A create step's fields are the object's own parameters, so the engine
      // reports what they currently come to; every other kind of step has no
      // resolved value to show, and numberInput says so rather than inventing
      // one.
      const described_ = described[name];
      const resolved =
        described_ && typeof described_.value === "number"
          ? described_.value
          : value;
      wrap.appendChild(numberInput(value, resolved, (v) => commit(name, v)));
    } else {
      const fixed = document.createElement("span");
      fixed.className = "hint";
      fixed.textContent = JSON.stringify(value);
      wrap.appendChild(fixed);
    }
    row.append(label, wrap, document.createElement("span"));
    box.appendChild(row);
  }
  stepEl.appendChild(box);
}

/** "7 × 7 × 3", so a table says what it is before it says what it holds. */
function shapeOf(value) {
  const dims = [];
  for (let v = value; Array.isArray(v); v = v[0]) dims.push(v.length);
  return dims.join(" × ");
}

/** The unit, kept quiet next to the name it belongs to. */
function unitTag(unit) {
  const el = document.createElement("span");
  el.className = "unit";
  el.textContent = unit ? "(" + unit + ")" : "";
  return el;
}

// --- properties section: the object's physics parameters --------------
async function loadParams() {
  const params = await rpc("get_params", { object_id: objectId });
  paramsEl.innerHTML = "";
  if (!params.length) return;
  const box = document.createElement("details");
  box.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "properties";
  box.appendChild(summary);

  for (const p of params) {
    const commit = (value) => {
      if (refuseIfGenerated()) return;
      statusEl.textContent = "";
      rpc("set_param", { object_id: objectId, name: p.name, value })
        .then((res) => {
          if (res && res.ok === false) statusEl.textContent = res.error;
          return Promise.all([loadParams(), loadTransform()]);
        })
        .catch((err) => {
          statusEl.textContent = String(err);
        });
    };
    if (p.kind === "scalar") {
      const row = document.createElement("div");
      row.className = "row";
      const label = document.createElement("label");
      label.append(document.createTextNode(p.name + " "));
      label.appendChild(unitTag(p.unit));
      label.title = p.doc;
      const input = numberInput(
        p.written === undefined ? p.value : p.written,
        p.value,
        commit,
      );
      const wrap = document.createElement("div");
      wrap.className = "widget";
      wrap.appendChild(input);
      row.append(label, wrap, document.createElement("span"));
      box.appendChild(row);
    } else if (p.kind === "vector") {
      // one row like every other property, so the labels line up
      const row = document.createElement("div");
      row.className = "row";
      const label = document.createElement("label");
      label.append(document.createTextNode(p.name + " "));
      label.appendChild(unitTag(p.unit));
      label.title = p.doc;
      const wrap = document.createElement("div");
      wrap.className = "widget";
      wrap.style.display = "block";
      wrap.appendChild(
        vecRow(
          p.components || p.value.map((_, i) => String(i + 1)),
          p.value,
          commit,
          undefined,
          p.written,
        ),
      );
      row.append(label, wrap, document.createElement("span"));
      box.appendChild(row);
    } else if (p.kind === "sampled") {
      // A run of points stated as the curve that draws them. The points are
      // what it comes to, not what it is: handing them to the table editor
      // below would let one stray edit replace a helix with the sixty points
      // it happened to draw, and the variables it followed with nothing. So
      // the formula is what you see, and it is edited where it is written.
      const spec = p.written.sampled;
      const shown = document.createElement("details");
      shown.className = "matrix";
      const summary = document.createElement("summary");
      summary.textContent =
        p.name +
        " — " +
        shapeOf(p.value) +
        ", sampled" +
        (p.unit ? " (" + p.unit + ")" : "");
      summary.title = p.doc;
      const area = document.createElement("textarea");
      area.readOnly = true;
      area.spellcheck = false;
      const terms = Array.isArray(spec.of) ? spec.of : [spec.of];
      area.value = terms
        .map((term) => String(term).replace(/^=/, ""))
        .concat(
          "for t in " +
            JSON.stringify(spec.over || [0, 1]) +
            ", " +
            String(spec.count).replace(/^=/, "") +
            " points",
        )
        .join("\n");
      area.rows = Math.min(8, terms.length + 2);
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent =
        "Drag the variables it is written in terms of, or edit it in the script tab.";
      shown.append(summary, area, hint);
      box.appendChild(shown);
    } else {
      // Tables (vertices, faces, sensor pixels). A 12x12 pixel grid on
      // one line of JSON is not an editor, it is a wall — so the shape is
      // what you see, and the numbers are there when you want them.
      const table = document.createElement("details");
      table.className = "matrix";
      const shape = document.createElement("summary");
      shape.textContent =
        p.name + " — " + shapeOf(p.value) + (p.unit ? " (" + p.unit + ")" : "");
      shape.title = p.doc;
      const area = document.createElement("textarea");
      area.rows = Math.min(8, p.value.length + 1);
      area.spellcheck = false;
      // one row of numbers per line
      area.value = p.value.map((r) => JSON.stringify(r)).join(",\n");
      area.addEventListener("change", () => {
        try {
          commit(JSON.parse("[" + area.value + "]"));
        } catch (err) {
          statusEl.textContent = p.name + ": " + err;
        }
      });
      table.append(shape, area);
      box.appendChild(table);
    }
  }
  paramsEl.appendChild(box);
}

// --- numbers that may be written as expressions -----------------------
//
// A field holds either a number or an expression over the document's
// variables, so the widgets are text inputs, not number inputs: a number
// input cannot hold "gap*2" at all. What the user types goes back as
// typed; only a value that parses as a number is sent as one.

// The dot is escaped on purpose. Unescaped it matches *any* character, so
// `/.?0+$/` ate the last significant digit along with the trailing zeros:
// 2.5 showed as "2.", 3.25 as "3.2" and 0.005 as "0.00" — and those strings
// are what a neighbouring edit committed back through asValue().
function short(value) {
  return Number(value)
    .toFixed(4)
    .replace(/\.?0+$/, "");
}

/** Document value -> what to show in the field. */
function asWritten(value, resolved) {
  if (typeof value === "string" && value.startsWith("=")) return value.slice(1);
  return short(resolved);
}

/** Field text -> document value: a number if it is one, else "=expr". */
function asValue(text) {
  const trimmed = String(text).trim();
  if (!trimmed) return 0;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : "=" + trimmed;
}

/** A value written as a name rather than a number: an axis "z", a mirror
 *  plane "xy". Not everything in a step is arithmetic, and reading one of
 *  these as a number is where the NaN came from. */
function isName(value) {
  return typeof value === "string" && !value.startsWith("=");
}

function numberInput(value, resolved, onCommit) {
  const input = document.createElement("input");
  input.type = "text";
  input.spellcheck = false;
  if (isName(value)) {
    // shown and committed verbatim: turning "z" into "=z" would make it
    // an expression over a variable of that name, which is a different
    // thing entirely
    input.value = value;
    input.addEventListener("change", () => onCommit(input.value.trim()));
    return input;
  }
  input.value = asWritten(value, resolved);
  // What makes it an expression is the leading '=', not a mismatch with the
  // resolved value: a step's own fields have no resolved value to compare
  // against, and comparing against one anyway is what produced "currently
  // NaN" on every expression in a pattern step.
  if (typeof value === "string" && value.startsWith("=")) {
    input.classList.add("expr");
    const current = Number(resolved);
    input.title = Number.isFinite(current)
      ? "expression — currently " + short(current)
      : "expression";
  }
  input.addEventListener("change", () => onCommit(asValue(input.value)));
  return input;
}

// --- transform section: absolute pose, relative ops, path tools -------
function vecRow(labels, values, onCommit, readonly, written) {
  const row = document.createElement("div");
  row.className = "vec" + (readonly ? " readonly" : "");
  const inputs = [];
  // A component is sent as its document value unless the user typed in it.
  // Editing x has to send y and z too — the engine takes the whole vector —
  // and reading those back off the screen rounds them to what the field can
  // show: a position of 795774.715564545 came back as 795774.7156, and every
  // fifth decimal in the scene went that way one sibling edit at a time.
  const originals = [];
  const shown = [];
  const commitAll = () =>
    onCommit(
      inputs.map((el, i) =>
        el.value === shown[i] ? originals[i] : asValue(el.value),
      ),
    );
  labels.forEach((name, i) => {
    const tag = document.createElement("span");
    tag.textContent = name;
    const original =
      written && written[i] !== undefined ? written[i] : values[i];
    const input = numberInput(original, values[i], commitAll);
    originals.push(original);
    shown.push(input.value);
    if (readonly) {
      input.readOnly = true;
      input.tabIndex = -1;
      input.title = readonly;
    }
    inputs.push(input);
    row.append(tag, input);
  });
  return row;
}

function transformOp(method, params) {
  if (refuseIfGenerated()) return Promise.resolve();
  statusEl.textContent = "";
  return rpc(method, Object.assign({ object_id: objectId }, params))
    .then((res) => {
      if (res && res.ok === false) statusEl.textContent = res.error;
      return loadTransform();
    })
    .catch((err) => {
      statusEl.textContent = String(err);
    });
}

async function loadTransform() {
  const t = await rpc("get_transform", { object_id: objectId });
  transformEl.innerHTML = "";
  const box = document.createElement("details");
  box.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "pose";
  box.appendChild(summary);

  // With a path there is no single pose to edit: the fields show the
  // last step read-only, and Transform… does the editing instead.
  const pathed =
    t.path_length > 1
      ? "read-only while this object has a path (" +
        t.path_length +
        " steps) — use Transform… on the object in the Scene view"
      : "";
  if (pathed) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "path: " + t.path_length + " steps (showing the last)";
    box.appendChild(hint);
  }
  box.appendChild(
    vecRow(
      ["x", "y", "z"],
      t.position,
      (v) => transformOp("set_transform", { position: v }),
      pathed,
      t.written_position,
    ),
  );
  box.appendChild(
    vecRow(
      ["rx", "ry", "rz"],
      t.orientation,
      (v) => transformOp("set_transform", { orientation: v }),
      pathed,
      t.written_orientation,
    ),
  );

  // Relative moves and rotations are not shown here: they record a
  // step, and this panel says what the object *is*. They live where the
  // other actions live, on the object in the Scene view.
  const where = document.createElement("div");
  where.className = "hint";
  where.textContent =
    "to move or rotate by an amount, use Transform… on " +
    "the object in the Scene view — those record a step";
  box.appendChild(where);
  transformEl.appendChild(box);
}

async function reloadValues() {
  values = await rpc("get_values", { object_id: objectId });
  render();
  await Promise.all([loadParams(), loadTransform()]);
}

async function loadObject(id) {
  objectId = id;
  emptyEl.style.display = id ? "none" : "";
  statusEl.textContent = "";
  filterEl.hidden = !id;
  if (!id) {
    headerEl.textContent = "";
    propsEl.innerHTML = "";
    transformEl.innerHTML = "";
    paramsEl.innerHTML = "";
    stepEl.innerHTML = "";
    return;
  }
  const listed = (await rpc("list_objects", {})).find((o) => o.id === id);
  generatedFrom = (listed && listed.derived) || null;
  headerEl.innerHTML = "";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = (listed && listed.label) || id;
  const what = document.createElement("div");
  what.className = "what";
  what.textContent = listed ? listed.type + "  ·  " + id : id;
  headerEl.append(name, what);
  if (generatedFrom) {
    const made = document.createElement("div");
    made.className = "generated";
    made.textContent =
      "generated from " +
      generatedFrom +
      " — change that object, its pattern step, or the variables";
    headerEl.appendChild(made);
  }
  [schema, values] = await Promise.all([
    rpc("get_schema", { object_id: id }),
    rpc("get_values", { object_id: id }),
  ]);
  render();
  await Promise.all([loadParams(), loadTransform()]);
}

/** The step form and the object's own sections, both back from source. */
async function reloadAll() {
  await loadStep();
  if (objectId) await reloadValues();
}

filterEl.addEventListener("input", render);

window.addEventListener("message", (event) => {
  const message = event.data;
  const fail = (err) => {
    statusEl.textContent = String(err);
  };
  if (message.type === "rpcResult" || message.type === "rpcError") {
    const entry = pending.get(message.reqId);
    if (!entry) return;
    pending.delete(message.reqId);
    if (message.type === "rpcResult") entry.resolve(message.result);
    else entry.reject(new Error(message.method + ": " + message.error));
  } else if (message.type === "select") {
    // picking an object directly is not picking a step: clear the form
    // before loading, so a stale step cannot linger above a new object
    stepId = null;
    loadObject(message.objectId).catch(fail);
  } else if (message.type === "operation") {
    stepId = message.eventId;
    loadStep().catch(fail);
  } else if (message.type === "refresh") {
    reloadAll().catch(fail);
  } else {
    // Nothing else posts into this webview, so an unknown type means the
    // two ends disagree — visible beats silent.
    fail("unhandled message: " + message.type);
  }
});

vscodeApi.postMessage({ type: "ready" });
