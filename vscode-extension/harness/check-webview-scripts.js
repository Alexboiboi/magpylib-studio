/**
 * Two guards on the webview code, run as part of `npm run compile`.
 *
 *   node harness/check-webview-scripts.js
 *
 * 1. Every media/*.js parses. They are loaded by URL, so a syntax error is
 *    reported nowhere the extension host can see it: the panel simply renders
 *    as the static HTML it starts as, blank and silent. That cost a day once.
 *
 * 2. No src/*.ts has grown a webview script back inside a template literal.
 *    That is where the escaping hazard lives — `\n` written singly is resolved
 *    by TypeScript into a real line break inside a quoted string — and where
 *    neither tsc nor eslint can see the code at all.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const EXT = path.join(__dirname, '..');
let failures = 0;

for (const file of fs.readdirSync(path.join(EXT, 'media')).filter((f) => f.endsWith('.js'))) {
  const source = fs.readFileSync(path.join(EXT, 'media', file), 'utf8');
  try {
    new vm.Script(source, { filename: file });
    console.log(`ok    media/${file} (${source.split('\n').length} lines)`);
  } catch (err) {
    failures += 1;
    console.log(`FAIL  media/${file}: ${err.message}`);
  }
}

// A <script> with a body, as opposed to one that names a file to load.
const INLINE = /<script(?:\s+nonce="\$\{nonce\}")?>\s*\n[\s\S]*?<\/script>/;
for (const file of fs.readdirSync(path.join(EXT, 'src')).filter((f) => f.endsWith('.ts'))) {
  const source = fs.readFileSync(path.join(EXT, 'src', file), 'utf8');
  if (INLINE.test(source)) {
    failures += 1;
    console.log(
      `FAIL  src/${file} embeds a webview script. Put it in media/ and load ` +
        `it with mediaUri(): inside a template literal nothing checks it.`,
    );
  }
}

process.exit(failures ? 1 : 0);
