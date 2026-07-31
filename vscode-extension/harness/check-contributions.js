/**
 * The contracts between package.json and the code, checked at build time.
 *
 *   node harness/check-contributions.js
 *
 * Every one of these is silent when broken — a command that never appears, a
 * menu entry that matches nothing, a palette entry that throws when picked.
 * They were all found by hand at least once; this is so they stay found.
 */
const fs = require('fs');
const path = require('path');

const EXT = path.join(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(EXT, 'package.json'), 'utf8'));
const src = fs
  .readdirSync(path.join(EXT, 'src'))
  .filter((f) => f.endsWith('.ts'))
  .map((f) => fs.readFileSync(path.join(EXT, 'src', f), 'utf8'))
  .join('\n');

const problems = [];
const declared = new Set(pkg.contributes.commands.map((c) => c.command));
const registered = new Set([...src.matchAll(/registerCommand\(\s*'([\w.-]+)'/g)].map((m) => m[1]));
const referenced = new Set(
  [...src.matchAll(/command:\s*'(magpylib-studio\.[\w.-]+)'/g)].map((m) => m[1]),
);

for (const command of declared) {
  if (!registered.has(command)) problems.push(`${command}: declared, never registered`);
}
for (const command of registered) {
  if (!declared.has(command)) problems.push(`${command}: registered, not declared`);
}
for (const command of referenced) {
  if (!registered.has(command)) problems.push(`${command}: referenced, not registered`);
}

// A command whose handler needs a tree object cannot be run from the palette,
// where it is invoked with nothing: it must be hidden with `when: false`.
const palette = Object.fromEntries(
  (pkg.contributes.menus.commandPalette ?? []).map((m) => [m.command, m.when]),
);
const needsArgument = [
  ...src.matchAll(/registerCommand\(\s*'([\w.-]+)',\s*(?:async\s*)?\(\s*\w+\s*:\s*Scene\w+\s*\)/g),
].map((m) => m[1]);
for (const command of needsArgument) {
  if (palette[command] !== 'false') {
    problems.push(
      `${command}: needs a tree object, so it must be hidden from the ` +
        `Command Palette with {"command": "${command}", "when": "false"}`,
    );
  }
}

// Language model tools have the same two-sided contract as commands: a name
// declared in package.json and a registration in the code. Registering one
// that is not declared throws at activation.
const tools = pkg.contributes.languageModelTools ?? [];
const declaredTools = new Set(tools.map((t) => t.name));
const registeredTools = new Set(
  [...src.matchAll(/(?:queryTool|editTool)\(\s*'([\w.-]+)'/g)].map((m) => m[1]),
);
for (const tool of declaredTools) {
  if (!registeredTools.has(tool)) problems.push(`${tool}: tool declared, never registered`);
}
for (const tool of registeredTools) {
  if (!declaredTools.has(tool)) problems.push(`${tool}: tool registered, not declared`);
}
for (const tool of tools) {
  for (const field of ['displayName', 'modelDescription', 'inputSchema']) {
    if (!tool[field]) problems.push(`${tool.name}: tool is missing ${field}`);
  }
}

// An array parameter with no `items` is rejected at *registration*, by the
// chat client rather than by us — "tool parameters array type must have
// items". Nothing here catches it: it type-checks, it packages, and the tool
// simply is not there when the model reaches for it. Five of them shipped
// that way. Union types (`["number", "array"]`) count, because they can hold
// an array too and the next validator to tighten will say so.
const arraysWithoutItems = (node, path) => {
  if (Array.isArray(node)) {
    return node.flatMap((child, i) => arraysWithoutItems(child, `${path}[${i}]`));
  }
  if (!node || typeof node !== 'object') {
    return [];
  }
  // `node.type` is not always a schema keyword: inside a `properties` block a
  // property may itself be *named* "type" (add_object takes one), so the
  // value can be an object. Only a string or a list of them is a type.
  const t = node.type;
  const declared = typeof t === 'string' ? [t] : Array.isArray(t) ? t : [];
  const here = declared.includes('array') && node.items === undefined ? [path || '(root)'] : [];
  return here.concat(
    Object.entries(node).flatMap(([key, child]) =>
      arraysWithoutItems(child, path ? `${path}.${key}` : key),
    ),
  );
};
for (const tool of tools) {
  for (const where of arraysWithoutItems(tool.inputSchema, '')) {
    problems.push(`${tool.name}: ${where} is an array with no "items" — the chat client will refuse to register this tool`);
  }
}

// Menu visibility keys off contextValue, which only the tree sets. A value is
// often built from pieces ('magpyObject' + 'Visible'), so take every string
// literal in the assignment and every concatenation of two of them: over-
// generating here only risks missing a broken clause, while under-generating
// would report working ones as broken, and a checker that cries wolf is worse
// than no checker.
const literals = new Set();
for (const [, rhs] of src.matchAll(/contextValue\s*=([^;]+);/g)) {
  for (const [, literal] of rhs.matchAll(/'([\w]+)'/g)) literals.add(literal);
}
const composed = new Set(literals);
for (const a of literals) for (const b of literals) composed.add(a + b);
for (const [group, items] of Object.entries(pkg.contributes.menus)) {
  for (const item of items) {
    for (const [, pattern] of (item.when ?? '').matchAll(
      /viewItem\s*(?:==|=~)\s*\/?([^/\s&|)]+)\/?/g,
    )) {
      const rx = new RegExp(pattern);
      if (![...composed].some((v) => rx.test(v))) {
        problems.push(`${group}: ${item.command} matches no contextValue (/${pattern}/)`);
      }
    }
  }
}

if (problems.length) {
  for (const problem of problems) console.log(`FAIL  ${problem}`);
  process.exit(1);
}
console.log(
  `ok    contributions (${declared.size} commands, ${declaredTools.size} lm tools, ` +
    `${Object.keys(palette).length} palette rules, ${composed.size} context values)`,
);
