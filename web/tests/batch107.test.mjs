// 批次107 契约守卫：日志「来源清单」与「回看世代」被封顶时的说法只有一份。
//
// 两处封顶本来都在静默生效：来源扫到 MAX_LOG_FILES 就 break，下拉框只剩 60 条却和
// "这台机器只有 60 个日志"完全同形；世代读 [:31] 之后 len() 就是 31，按天趋势于是能写出
// "读了 31/31 份世代"这句恰好最像读完了的话。后端补了三个数之后，前端的措辞必须只有一个
// 出处——这里用真输入把 ui.ts 跑一遍，顺手把"没说清就不许画完整表"钉住。
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
const { SERIES_UNREADABLE_NOTE, seriesCappedNote, sourcesCappedNote } = await import('../src/ui.ts');

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'LogCenter.vue');
const src = readFileSync(SRC, 'utf8');

test('sourcesCappedNote：说清"序号不是全部"并把收窄范围的办法给出来', () => {
  const out = sourcesCappedNote(60);
  assert.ok(out.includes('60'), `上限要写进句子里，否则用户不知道被什么挡住：${out}`);
  assert.ok(out.includes('不代表全部'), '触顶的第一件事是"这份清单不是全部"');
  assert.ok(out.includes('CP_LOG_ROOTS'), '能做的处置只有把目录写进白名单，得一起说');
  assert.doesNotMatch(out, /没有更多|只有 60 个/, '不许把触顶念成"这台机器就这么多日志"');
});

test('seriesCappedNote：listed/total/limit 三个数都得露出来', () => {
  const out = seriesCappedNote(31, 45, 31);
  assert.ok(out.includes('31/45'), `要给出"翻到几份、盘上共几份"：${out}`);
  assert.ok(out.includes('上限 31'), '还要说明两者为什么不一样');
  assert.ok(out.includes('环比'), '触顶的真正代价是环比比了被削短的一段，不点出来就会被当噪声');
  assert.doesNotMatch(out, /没有历史|没有轮替/, '更早的世代确实在盘上，这句话不能说反');
});

test('SERIES_UNREADABLE_NOTE：列不出清单不等于没有历史', () => {
  assert.ok(SERIES_UNREADABLE_NOTE.includes('不等于'), SERIES_UNREADABLE_NOTE);
  assert.ok(/logrotate|没有读权限/.test(SERIES_UNREADABLE_NOTE), '要给管理员一个能查的方向');
  assert.doesNotMatch(SERIES_UNREADABLE_NOTE, /没人访问|流量为 ?0/,
    '"这一轮没看到"被念成"这个站点没人来"，人就去找流量而不是找权限');
});

test('三个页签都读服务端给的触顶标记，页面不自己数', () => {
  // 类型上挂同一个 SrcMeta，取值只认 filesCapped —— 页面拿 files.length 判触顶就是把 60 当全部
  assert.equal((src.match(/& SrcMeta/g) || []).length, 3, 'tail/统计/按天三种响应都要挂来源账');
  assert.ok(!/files\.length\s*>=?\s*\d+/.test(src), '不许在页面里用列表长度反推有没有触顶');
  ['来源清单', '世代清单'].forEach((k) => assert.ok(src.includes(k), `${k} 这一行要在口径里露出来`));
  assert.ok(/v-if="tailCap"/.test(src), 'tail 页签的触顶要单独一条提示，与"这份读得动读不动"分开');
  assert.ok(/dResult\.seriesUnreadable/.test(src), '按天空表要分清"没看到"和"没有"');
});

test('封顶判词只有一个出处：页面不许再抄一遍后端那两句', () => {
  ['来源清单只列到', '回看只统计了'].forEach((s) => assert.ok(!src.includes(s), `「${s}」是 ui.ts 的措辞`));
  assert.ok(src.includes("from '../ui'") || src.includes('from \'../ui\''),
    '两句都得从 ui.ts 引进来，而不是在页面里各写一份');
});
