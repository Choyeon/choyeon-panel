// 批次83 契约守卫：三处「静默截断」必须自己说出被截掉了多少。
//
// 截断本身合理（浮层、表格格子、JSON 预览都有尺寸上限），不合理的是切一半不吭声：
// 读的人会以为「本来就这么长 / 一共就这些」。说法收进 ui.ts 的两个共享读者
// （cutChars / cutCounted），这里拿真实输入把它们跑一遍——只 grep 源码的话，
// 总数写错一个字符在页面上是看不出来的。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

// ui.ts 顶层要摸浏览器全局（抽屉宽度读 window.innerWidth，naive-ui 摸 localStorage），
// 照 unitTag 的做法用真 DOM 补齐，而不是塞一个假对象。
const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { cutChars, cutCounted } = await import('../src/ui.ts');

test('cutChars：未到上限原样返回，超了就把总量和去处一起给', () => {
  const short = '{"a":1}';
  assert.equal(cutChars(short, 3000, '点开行详情查看完整内容'), short);
  // 正好等于上限不算截断：多写一句「已截断」同样是谎
  const exact = 'y'.repeat(3000);
  assert.equal(cutChars(exact, 3000, 'z'), exact);
  const out = cutChars('x'.repeat(3001), 3000, '点开行详情查看完整内容');
  assert.ok(out.startsWith('x'.repeat(3000)), '前 3000 字符原样留着');
  assert.match(out, /…（已截断，共 3001 字符，点开行详情查看完整内容）$/);
});

test('cutCounted：只列前 max 条时真实条数跟着走', () => {
  const show = (a) => a.id;
  assert.equal(cutCounted([], 8, show), '', '空列表什么都不该说');
  const five = Array.from({ length: 5 }, (_, i) => ({ id: `A${i}` }));
  assert.equal(cutCounted(five, 8, show), 'A0、A1、A2、A3、A4');
  const out = cutCounted(Array.from({ length: 12 }, (_, i) => ({ id: `RHSA-${i}` })), 8, show);
  assert.ok(out.startsWith('RHSA-0、RHSA-1、RHSA-2、RHSA-3、RHSA-4、RHSA-5、RHSA-6、RHSA-7'), '只列前 8 条');
  assert.match(out, / 等 12 条$/, '列了 8 条却说一共 12 条——数字来自整张表，不是这一屏');
});

test('三处截断都改走同一个读者，不再各写各的', () => {
  assert.match(read('views/Services.vue'),
    /advisoryLine = computed\(\(\) => cutCounted\(patches\.value\?\.advisories \|\| \[\], 8,/);
  assert.match(read('components/RedisKeyPanel.vue'),
    /const cut = cutChars\(s, VALUE_CELL_MAX, '完整值请用 CLI 读取'\);/);
  assert.match(read('views/Databases.vue'),
    /cutChars\(JSON\.stringify\(v, null, 2\), 3000, '点开行详情查看完整内容'\)/);
  // 本地另写一份截断法的痕迹要清零
  for (const rel of ['views/Services.vue', 'components/RedisKeyPanel.vue', 'views/Databases.vue'])
    assert.doesNotMatch(read(rel), /slice\(0, (8|500|3000)\)[^\n]*已截断|已截断[^\n]*slice\(0, (8|500|3000)\)/, rel);
});

test('Redis 值格仍给两个全文入口：点按浮层与 title，且没有 hover-only 的私设', () => {
  const s = read('components/RedisKeyPanel.vue');
  assert.match(s, /return h\('span', \{ class: 'mono-dim', title: cut \}, cut\);/);
  assert.match(s, /ellipsis: tapEllipsis/);
  assert.doesNotMatch(s, /tooltip: \{ trigger: 'hover' \}/);
});
