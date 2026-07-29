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
  `ok    contributions (${declared.size} commands, ` +
    `${Object.keys(palette).length} palette rules, ${composed.size} context values)`,
);
