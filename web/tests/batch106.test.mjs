// 批次106 契约守卫：定时任务页「这一项本轮读不到」的说法只有一份。
//
// systemctl 读失败（bus 断了、超时、权限不够）和「unit 存在但没挂表」是两件事：前者
// 什么都不知道，后者是个确定答案。原本两个 helper 把退出码非 0 直接当成「没挂表、没跑过」
// 返回空表，界面于是理直气壮地写「未挂表」「未执行」。这里拿真输入把 ui.ts 的这两个读者
// 跑一遍——只 grep 源码的话，短名和长句会各说各话，「读不到」也会被人顺手改成「未知」。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { unknownNote, UNKNOWN_LABEL } = await import('../src/ui.ts');

test('unknownNote：读不到就说读不到，不替 systemd 下结论', () => {
  assert.equal(unknownNote('定时器状态'), '定时器状态本轮读不到');
  assert.doesNotMatch(unknownNote('定时器状态'), /未挂|停用|没跑|未执行/,
    '读不到不等于「定时器没挂」——把猜测写进结论就是批次101 禁的那件事');
  assert.doesNotMatch(unknownNote('执行记录'), /未知|异常/, '要给管理员一个能查的词，不是一个情绪词');
});

test('unknownNote：后端给了原因就把原因一起说', () => {
  const why = 'systemctl 退出码 1：Failed to connect to bus: No such file or directory';
  const out = unknownNote('执行记录', why);
  assert.ok(out.startsWith('执行记录本轮读不到：'), out);
  assert.ok(out.includes('退出码 1'), '只说"读不到"等于没告诉人去哪查，原因要跟着一起来');
  assert.ok(out.includes('Failed to connect to bus'));
  assert.equal(out.split('本轮读不到').length - 1, 1, '原因里不该再生一次「读不到」——措辞归这里，后端只给原因');
});

test('unknownNote：空原因不留下一个光秃秃的冒号', () => {
  assert.equal(unknownNote('重叠保护', ''), '重叠保护本轮读不到');
  assert.equal(unknownNote('重叠保护', undefined), '重叠保护本轮读不到');
});

test('UNKNOWN_LABEL：格子内的短名与长句是同一件事', () => {
  assert.equal(UNKNOWN_LABEL, '未读到');
  assert.ok(unknownNote('定时器状态').includes('读不到'), '长短两种说法要同一个动词，否则页面上一处「未读到」一处「未知」');
});
