// 批次84 契约守卫：接口自己切过的清单，页面必须说真总数而不是数数组长度。
//
// 服务端为了响应体大小把 packages 切到 300、advisories 切到 60。这两个数字是**传输上限**，
// 不是测量结果，可此前它们切完不留痕：页面只能数数组长度，于是 87 条通告的机器显示
// 「等 60 条」，412 个待装的机器表格里少了 112 行而一个字都不提。
// 现在总数与上限都随响应走（advisory_total / list_limits），这里把真输入喂给共享读者跑一遍。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { cutCounted } = await import('../src/ui.ts');

const show = (a) => a.id;
const ids = (n, from = 0) => Array.from({ length: n }, (_, i) => ({ id: `RHSA-${from + i}` }));

test('cutCounted 收到服务端真总数时，说的是真总数而不是数组长度', () => {
  // 服务端把 87 条切成 60 条送过来，页面若数数组就永远报「等 60 条」
  const out = cutCounted(ids(60), 8, show, 87);
  assert.ok(out.startsWith('RHSA-0、RHSA-1、RHSA-2、RHSA-3、RHSA-4、RHSA-5、RHSA-6、RHSA-7'), '仍只列前 8 条');
  assert.match(out, / 等 87 条$/, '真实条数必须跟着走');
});

test('数组没被切过或总数缺失时不许凭空造出截断', () => {
  assert.equal(cutCounted(ids(5), 8, show), 'RHSA-0、RHSA-1、RHSA-2、RHSA-3、RHSA-4');
  assert.equal(cutCounted(ids(5), 8, show, 5), 'RHSA-0、RHSA-1、RHSA-2、RHSA-3、RHSA-4');
  // 总数读不到（旧缓存 / 别的发行版没这一路）时退回数组长度，不写成 0 也不省略标记
  const out = cutCounted(ids(12), 8, show, undefined);
  assert.match(out, / 等 12 条$/);
});

test('真总数比数组还小也不许把显示条数说少', () => {
  // 接口给脏数据（total < 数组长度）时，宁可报数组长度也不能少报已经列出来的条目
  const out = cutCounted(ids(10), 8, show, 3);
  assert.match(out, / 等 10 条$/, '等 N 条里的 N 不能小于实际摆在面前的条数');
});

test('补丁页签读接口的总数与上限，不自己抄 300/60', () => {
  const s = read('views/Services.vue');
  assert.match(s, /advisoryLine = computed\(\(\) => cutCounted\([\s\S]{0,80}advisory_total\)\);/);
  assert.match(s, /list_limits\?\.packages/);
  assert.match(s, /<NText v-if="patchListCut"[^>]*>\{\{ patchListCut \}\}<\/NText>/);
  for (const bad of ['> 300', '> 60', 'length > 300'])
    assert.ok(!s.includes(bad) || !new RegExp(`pending_total[^\\n]*${bad}`).test(s), `不该出现抄来的上限：${bad}`);
});

test('cutChars 的三处调用点仍走同一个读者（批次83 的约定不许回退）', () => {
  assert.match(read('components/RedisKeyPanel.vue'), /cutChars\(s, VALUE_CELL_MAX,/);
  assert.match(read('views/Databases.vue'), /cutChars\(JSON\.stringify\(v, null, 2\), 3000,/);
});
