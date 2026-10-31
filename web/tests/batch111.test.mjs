// 批次111 契约守卫：绝对量（容量 / 占用 / 累计字节）一格读法。
//
// 概览的仪表在「这一轮没读到」时圆心已经写短名（批次106/108），可下面那行 detail 与网速卡的
// 「总发送 / 总接收」仍写成 `fmtBytes(x || 0)`：首屏 stats 初值与探测失败那两轮都画出「0 B / 0 B」，
// 读起来是「这台机器没有内存」「一个字节也没发过」——一格两句话。这里用真输入跑 bytesText，
// 并把页面不再兜 0 钉住。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { UNKNOWN_LABEL, bytesText } = await import('../src/ui.ts');

const VUE = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Dashboard.vue'), 'utf8');

test('bytesText：真读数照旧，缺数说短名', () => {
  assert.equal(bytesText(0), '0 B', '读到 0 就是 0，不许一并糊成未读到');
  assert.equal(bytesText(1073741824), '1.0 GB');
  assert.equal(bytesText(32768), '32.0 KB');
  for (const bad of [null, undefined, NaN, Infinity, '1024', {}, true]) {
    assert.equal(bytesText(bad), UNKNOWN_LABEL, `${String(bad)} 不是读到的容量`);
  }
  assert.equal(bytesText(-1), UNKNOWN_LABEL, '负容量是坏值，不是一格 0 B');
});

test('概览的容量行与累计收发都不再兜 0', () => {
  assert.match(VUE, /bytesText\(stat\.value\.memUsed\)} \/ \$\{bytesText\(stat\.value\.memTotal\)/);
  assert.match(VUE, /bytesText\(stat\.value\.diskUsed\)} \/ \$\{bytesText\(stat\.value\.diskTotal\)/);
  assert.match(VUE, /k: '总发送', v: bytesText\(s\.netTotalTx\)/, '「全部网卡」那一格同一个读者');
  assert.match(VUE, /k: '总发送', v: bytesText\(one\.totalTx\)/, '单网卡同理');
  for (const bad of ['fmtBytes(stat.value.memUsed || 0)', 'fmtBytes(stat.value.memTotal || 0)',
                     'fmtBytes(stat.value.diskUsed || 0)', 'fmtBytes(stat.value.diskTotal || 0)',
                     'fmtBytes(one.totalTx || 0)', 'fmtBytes(one.totalRx || 0)',
                     'fmtBytes(s.netTotalTx || 0)', 'fmtBytes(s.netTotalRx || 0)']) {
    assert.ok(!VUE.includes(bad), `「${bad}」又把没读到印成了 0 B`);
  }
});

test('短名与速率读法仍然只有一个出处', () => {
  const ui = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'ui.ts'), 'utf8');
  for (const decl of ['export const UNKNOWN_LABEL', 'export function bytesText']) {
    assert.equal(ui.split(decl).length - 1, 1, `${decl} 不许长第二份`);
  }
  assert.ok(!VUE.includes('未读到'), '页面不另写一份短名');
});
