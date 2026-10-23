// unitTag 的行为回归：应用列表那一格的状态标签，是这套判词唯一的出处。
//
// 批次65 的线上现场：choyeon-book 由外部 unit `choyeon-book.service` 服务，面板那条记录里
// 没有 unit_override，于是它按派生名 panel-choyeon-book.service 去问 systemd——一个从来
// 不存在的名字。这一格此前是红色「单元不存在」，而屏幕上唯一的处置建议是「请重新立即部署」，
// 照做会由 render_unit 写出第二个 unit，ExecStart 指向同一个目录、同一个 3400，和正在服务的
// 那个外部 unit 抢端口。所以「同名 unit 就在 systemd 里」必须是一个读出来的事实，并且说出
// 不一样的话：琥珀色「未纳管」，指向配置页的「已有 unit」。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

// ui.ts 在模块顶层读了一次 window.innerWidth（抽屉宽度那个响应式值），而它 import 的 naive-ui
// 更早就在模块顶层引用 `Image`，所以要用真 DOM 把全局补齐再引进来，而不是拿一个
// {innerWidth:1440} 的假对象——假对象能让 import 过去，却测不出「这个模块在浏览器里能不能起来」。
// 逐个列必漏：naive-ui 顶层用到的浏览器全局按整张 window 属性表搬，已有的（node 自带的
// navigator/performance）不覆盖。url 必须给：naive-ui 顶层就要摸 localStorage，
// jsdom 默认是不透明源，不给地址直接 SecurityError 把 import 打断。
const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { pretendToBeVisual: true, url: 'http://127.0.0.1:3210/' });
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!(k in globalThis)) globalThis[k] = dom.window[k];
}
const { unitTag } = await import('../src/ui.ts');

test('有同名外部 unit：判成「未纳管」而不是红色「不存在」', () => {
  const t = unitTag({ unitState: 'notfound', unitMissing: true, suggestUnit: 'choyeon-book.service' });
  assert.equal(t.status, 'conflict', 'status 必须是 conflict：CpStatusTag 的 WARN 表认这个值，才会画成琥珀色');
  assert.equal(t.text, '未纳管');
  assert.equal(t.tone, 'warn');
});

test('真的没有同名 unit：原话一条不减', () => {
  const t = unitTag({ unitState: 'notfound', unitMissing: true });
  assert.equal(t.status, 'notfound');
  assert.equal(t.text, '单元不存在');
  assert.equal(t.tone, 'err');
});

test('A/B：suggestUnit 是空串时不许冒充「未纳管」', () => {
  // 后端在没有建议时给的是 null，但接口经过 JSON 与视图层展开之后空串是同一个意思，
  // 判词不能因为一个假值就从红转黄——那会把「从来没部署过」说成「纳管没填」。
  assert.equal(unitTag({ unitMissing: true, suggestUnit: '' }).text, '单元不存在');
});

test('unit 存在时建议不介入，照旧按 systemd 状态说话', () => {
  assert.deepEqual(unitTag({ unitState: 'active', suggestUnit: 'x.service' }),
    { status: 'running', text: '运行中', tone: 'ok' });
  assert.equal(unitTag({ unitState: 'failed' }).text, '启动失败');
  assert.equal(unitTag({ unitState: 'activating' }).text, '正在启动');
  assert.equal(unitTag({}).text, '状态未知');
});
