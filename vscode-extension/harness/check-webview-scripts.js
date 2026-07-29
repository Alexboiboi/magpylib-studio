/**
 * Syntax-checks the JavaScript inside every webview HTML the extension emits.
 *
 *   node harness/check-webview-scripts.js
 *
 * That code lives in TypeScript template literals, so tsc never parses it: an
 * escape that survives the outer literal (`\n` becoming a real newline inside
 * a quoted string) produces a webview that renders as a blank panel, with no
 * error anywhere the extension host can see. Cheap to check, so check it.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'src');
const SCRIPT_RE = /<script(?:\s+nonce="\$\{nonce\}"[^>]*)?>([\s\S]*?)<\/script>/g;

let failures = 0;
for (const file of fs.readdirSync(SRC).filter((f) => f.endsWith('.ts'))) {
  const source = fs.readFileSync(path.join(SRC, file), 'utf8');
  let index = 0;
  for (const match of source.matchAll(SCRIPT_RE)) {
    const body = match[1];
    if (!body.trim() || body.includes('src=')) {
      continue;
    }
    index += 1;
    // What the browser receives: the outer template literal has already
    // resolved its escapes and interpolations by then.
    const emitted = body
      .replace(/\\`/g, '`')
      .replace(/\\\$/g, '$')
      .replace(/\$\{[^}]*\}/g, '0')
      .replace(/\\([nrt'"\\])/g, (_, c) =>
        ({ n: '\n', r: '\r', t: '\t' })[c] ?? `\\${c}`,
      );
    const label = `${file} script #${index}`;
    try {
      new vm.Script(emitted, { filename: label });
      console.log(`ok    ${label} (${emitted.split('\n').length} lines)`);
    } catch (err) {
      failures += 1;
      console.log(`FAIL  ${label}: ${err.message}`);
      const line = Number(/:(\d+)$/.exec(err.stack?.split('\n')[0] ?? '')?.[1]);
      const at = err.stack?.split('\n').slice(0, 3).join('\n');
      console.log(at ?? '', line || '');
    }
  }
}
process.exit(failures ? 1 : 0);
