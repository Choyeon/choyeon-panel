// 批次105 契约守卫：巡检「本轮有 N 节压根没查成」这句话的说法只有一份。
//
// 后端每个巡检入口的成功回应原本只说「巡检完成，新增 N 条历史」。systemctl 不可用、
// 系统指标读失败、应用列表查失败时历史可以一行都不新增——那既不是"查完都没事"，也不该
// 被界面说成"查完了"（批次101 立的规矩）。这里拿真输入把 ui.ts 的那个读者跑一遍：
// 只 grep 源码的话，条数写错、第四节的账丢了，在页面上都看不出来。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { uncheckedNote } = await import('../src/ui.ts');

test('uncheckedNote：全都查成了就闭嘴', () => {
  assert.equal(uncheckedNote(undefined), '', '接口没带这个字段（老响应/全绿）不该凭空造出一节');
  assert.equal(uncheckedNote([]), '');
  assert.equal(uncheckedNote(null), '');
  assert.equal(uncheckedNote('磁盘使用率阈值本轮没查成'), '', '后端结构变了也不许把字符串当数组数');
});

test('uncheckedNote：说了哪几节、说了几节', () => {
  const one = ['磁盘使用率阈值本轮没查成（/root/www 所在分区读不到占用）'];
  assert.match(uncheckedNote(one), /^本轮有 1 节没查成：磁盘使用率阈值本轮没查成（/);
  const two = ['failed 状态服务清单本轮没查成（systemctl 退出码 1）',
    '证书到期天数本轮没查成（certbot 退出码 127）'];
  const out = uncheckedNote(two);
  assert.ok(out.startsWith('本轮有 2 节没查成：'), out);
  assert.ok(out.includes('systemctl 退出码 1'), '原因要跟节名一起说，只说"没查成"等于没告诉人去哪查');
  assert.ok(out.includes('certbot 退出码 127'));
  assert.ok(!out.includes('另有'), '两节都列出来了还写"另有"，数字就成了假的');
});

test('uncheckedNote：装不下的节数如实说还有几节，不冒充全清单', () => {
  const five = Array.from({ length: 5 }, (_, i) => `第${i + 1}节本轮没查成（原因）`);
  const out = uncheckedNote(five);
  assert.ok(out.startsWith('本轮有 5 节没查成：第1节'), out);
  assert.ok(out.includes('第3节'), '提示条只列前 3 节');
  assert.ok(!out.includes('第4节'), '第 4 节起不该挤进这一条 toast');
  assert.match(out, /另有 2 节详见面板日志$/, '列了 3 节就要说清另外 2 节在哪读');
});
