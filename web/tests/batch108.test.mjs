// 批次108 契约守卫：速率「量不出」与「量到 0」是两件事，格子不能长成一个样。
//
// 后端这一轮把没有分母的读数从 0.0 改成了 null（首帧、同一秒连采两次、计数器回绕）。
// 前端原来一律 `x || 0` 再拼 `/s`，于是每次重启后的第一眼都是「这台机器 0 B/s、CPU 0%」——
// 一个从没发生过的测量。这里用真输入跑一遍 ui.ts 的两个读法，并把页面不再兜 0 钉住。
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
const { UNKNOWN_LABEL, rateNum, rateText } = await import('../src/ui.ts');

const VUE = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Dashboard.vue'), 'utf8');

test('rateNum：只有真的数字才算量到，0 仍然是读数', () => {
  assert.equal(rateNum(1234), 1234);
  assert.equal(rateNum(0), 0, '0 是「有分母且确实没涨」，不能一起当成没读到');
  assert.equal(rateNum(-5), -5, '负数由后端挡住；前端不额外把它改成 0');
  for (const bad of [null, undefined, NaN, Infinity, '1024', {}, true]) {
    assert.equal(rateNum(bad), null, `${String(bad)} 不是一个测量`);
  }
});

test('rateText：有分母带单位，没有分母只说未读到', () => {
  assert.equal(rateText(0), '0 B/s');
  assert.equal(rateText(1536), '1.5 KB/s');
  assert.equal(rateText(null), UNKNOWN_LABEL, '空格子不许自己拼一个 `/s` 出来');
  assert.equal(rateText(undefined), UNKNOWN_LABEL);
  assert.equal(rateText(NaN), UNKNOWN_LABEL);
  assert.ok(!rateText(null).includes('/s'), '「未读到/s」会被读成一种速率');
});

test('概览的速率格与曲线都不再兜 0', () => {
  assert.match(VUE, /rateText\(s\.ioRead\)/, '磁盘 IO 那一格读后端给的 null');
  assert.match(VUE, /rateText\(one\.tx\)/, '单网卡的上下行同理');
  assert.match(VUE, /p\.net\[monNic\.value\]\?\.\[i\] \?\? null/, '曲线缺一个点就断口，不贴底');
  for (const bad of ['fmtBytes(s.ioRead || 0)', 'fmtBytes(s.netTx || 0)', 'fmtBytes(s.netRx || 0)',
                     'fmtBytes(one.tx || 0)', '?.[i] ?? 0']) {
    assert.ok(!VUE.includes(bad), `「${bad}」又把没量到印成了 0`);
  }
});

test('仪表圆心是文字时不拼百分号', () => {
  const gauge = readFileSync(join(dirname(fileURLToPath(import.meta.url)),
    '..', 'src', 'components', 'StatGauge.vue'), 'utf8');
  assert.match(gauge, /v-if="unit && numeric !== null"/, '「未读到%」不是一个读数');
  assert.match(gauge, /numeric\.value === null \? '' : props\.unit/, '读屏与画面同一判法');
});
