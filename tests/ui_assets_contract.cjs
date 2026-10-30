/* Verify the browser and desktop load the same existing local assets in order. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ui = path.resolve(__dirname, '../ui');
const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
const attr = (tag, name) => {
  const match = tag.match(new RegExp(`\\b${name}\\s*=\\s*(["'])(.*?)\\1`, 'i'));
  return match ? match[2] : null;
};

function localFile(base, ref) {
  assert.ok(ref && !/^(?:[a-z][a-z\d+.-]*:|\/|\\)/i.test(ref), `Expected a relative asset: ${ref}`);
  const resolved = path.resolve(base, decodeURIComponent(ref.split(/[?#]/, 1)[0]));
  const relative = path.relative(ui, resolved);
  assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative), `Asset leaves ui/: ${ref}`);
  assert.ok(fs.statSync(resolved).isFile(), `Missing asset: ${resolved}`);
  return resolved;
}

const styles = [], scripts = [];
for (const match of html.matchAll(/<(?:link|script)\b[^>]*>/gi)) {
  const tag = match[0];
  if (/^<script\b/i.test(tag)) {
    const file = localFile(ui, attr(tag, 'src'));
    assert.match(tag, /\bdefer\b/i, `Script must preserve deferred order: ${tag}`);
    scripts.push(file);
  } else {
    const href = attr(tag, 'href');
    const rel = attr(tag, 'rel');
    if (href) {
      const file = localFile(ui, href);
      if (rel && rel.toLowerCase() === 'stylesheet') styles.push(file);
    }
  }
}

assert.deepEqual(styles.map(file => path.basename(file)), [
  'fonts.css', 'styles.css', 'daily.css', 'models.css', 'workspace-features.css',
]);
assert.deepEqual(scripts.map(file => path.basename(file)), [
  'bridge.js', 'app.js', 'workbench.js', 'subscription_monitor.js',
  'codex_subscription.js', 'zcode_subscription.js', 'workspace-features.js', 'skills-bulk.js',
]);

for (const stylesheet of styles) {
  const css = fs.readFileSync(stylesheet, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const match of css.matchAll(/url\(\s*(?:(["'])(.*?)\1|([^'")]*))\s*\)/gi)) {
    const ref = (match[2] ?? match[3]).trim();
    if (ref && !/^(?:data:|#)/i.test(ref)) localFile(path.dirname(stylesheet), ref);
  }
}
console.log(`PASS ${styles.length} stylesheets, ${scripts.length} deferred scripts, and local CSS assets`);
