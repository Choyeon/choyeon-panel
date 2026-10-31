// 批次109 契约守卫：应用详情「运行状态」这一张卡，一格一条读法。
//
// systemctl 查不成（超时、非 root、本机没有 systemd）时，后端原来把
// `NRestarts` 折成 0、把 `SubState` 兜成字符串 "unknown"，界面又把 state=unknown
// 的 PID 格写成「未运行」——三格一起把「这一次没查到」说成了三条已知事实，
// 而旁边的状态标签同时写着「状态未知」。现在读不到一律是 null，短名只有 UNKNOWN_LABEL 一个。
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
const { UNKNOWN_LABEL, cellText } = await import('../src/ui.ts');

const HERE = dirname(fileURLToPath(import.meta.url));
const VUE = readFileSync(join(HERE, '..', 'src', 'views', 'AppDetail.vue'), 'utf8');

test('cellText：0 与空串是两种答案', () => {
  assert.equal(cellText(0), '0', '「重启 0 次」是真读数，不能一并糊成未读到');
  assert.equal(cellText(5), '5');
  assert.equal(cellText('running'), 'running');
  for (const bad of [null, undefined, '']) {
    assert.equal(cellText(bad), UNKNOWN_LABEL, `${JSON.stringify(bad)} 是「这一次没读到」`);
  }
});

test('进程读数六格都不留空白、也不替 state 说话', () => {
  const start = VUE.indexOf('const resItems = computed(');
  assert.ok(start > 0, 'resItems 挪窝了，这条守卫跟着找新位置');
  const block = VUE.slice(start, VUE.indexOf('});', start));
  const lines = block.split('\n').filter((l) => l.includes('label:'));
  assert.equal(lines.length, 6, '这张卡还是六条事实');
  for (const line of lines) {
    assert.ok(!line.includes(": ''"), `空白格和「这个页面没有这个字段」同形：${line.trim()}`);
    assert.ok(!line.includes("|| ''"), `空白格和「这个页面没有这个字段」同形：${line.trim()}`);
  }
  assert.match(block, /cellText\(p\.restarts\)/);
  assert.match(block, /cellText\(p\.subState\)/);
  assert.match(block, /p\.cpuSeconds == null \? UNKNOWN_LABEL : `\$\{p\.cpuSeconds\}s`/);
});

test('PID 一格的「未运行」只在真读到判词时说出口', () => {
  assert.ok(!VUE.includes('p.running && p.pid'), 'PID 格不许在 state 之外另起一套存活口径');
  assert.match(VUE, /if \(p\.pid\) return String\(p\.pid\);/);
  assert.match(VUE, /p\.state === 'unknown' \? UNKNOWN_LABEL : '未运行'/);
  assert.ok(!VUE.includes('未读到'), '短名从 UNKNOWN_LABEL 来，别在页面里另写一份');
});
