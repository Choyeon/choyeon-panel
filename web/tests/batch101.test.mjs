// 批次101 契约守卫（静态源码断言）。
//
// 这一处修的是同一类谎：后端已经在响应里写清「这一轮其实没做/没做全」，
// 界面却替它宣布「做完了」。这类缺陷构建、类型检查、接口都发现不了——
// 标志位是真的返回了，只是当面的那句 toast 没读它。
//
// - Alerts.vue / Settings.vue：巡检撞上「上一轮还在执行，本轮跳过」时后端只回
//   `skipped`，历史一条都没新增，界面不许再说「巡检完成 / 巡检已执行」。
// - api.ts：同一个巡检接口在前端只许有一个等待上限。以前告警页给 330s、设置页给 60s，
//   设置页那一份必然在这轮真跑完之前先报「请求超时」，用户再点一次只是撞进跳过分支。
// - Backups.vue：补推成功后 `prune_skipped` 说明远端一份都没少删（桶还在长）。
//   runner 的 stderr、审计与 CLI 都转述了它，界面那句「已推送 N 份」不许是第四个读者里唯一沉默的。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

/** 取 `async function name(` 到下一个顶层 `async function` / `// ---` 之间的那一段。 */
function fnBody(text, name) {
  const i = text.indexOf(`async function ${name}(`);
  assert.notEqual(i, -1, `读不到 ${name}()：函数名被改了，本用例得跟着改`);
  const rest = text.slice(i + name.length);
  const j = rest.search(/\n(?:async function|function |\/\/ -{10})/);
  return j < 0 ? rest : rest.slice(0, j);
}

test('Alerts.vue 巡检跳过时转述 skipped，不报「巡检完成」', () => {
  const s = fnBody(read('views/Alerts.vue'), 'runRound');
  assert.match(s, /if \(r\?\.skipped\) msg\.warning/, '跳过分支必须存在，否则「本轮没跑」被说成完成');
  assert.match(s, /这次没有执行巡检/, '判词要明说这一轮没跑，只画 warning 颜色没人读得出区别');
  // 顺序即正确性：成功那句必须在 skipped 判定之后
  assert.ok(s.indexOf('if (r?.skipped)') < s.indexOf('msg.success('), '成功句排在跳过判定前面等于永远绿');
});

test('Settings.vue 立即巡检转述 skipped，不再无条件报「已执行」', () => {
  const s = fnBody(read('views/Settings.vue'), 'runChecks');
  assert.match(s, /const r = await api\.alertRunChecks\(\);/);
  assert.match(s, /if \(r\?\.skipped\) msg\.warning/);
  assert.match(s, /这次没有执行巡检/);
  assert.ok(s.indexOf('if (r?.skipped)') < s.indexOf("msg.success('巡检已执行"), '成功句不许先于跳过判定');
  assert.ok(!/await api\.alertRunChecks\(\);\s*\n\s*msg\.success/.test(s), '又把响应丢掉了：无条件 success 回来了');
});

test('巡检接口在前端只有一个等待上限，且它大于告警页原来那份', () => {
  const api = read('api.ts');
  const line = api.split('\n').find((l) => l.includes("'/settings/alerts/run-checks'"));
  assert.ok(line, 'api.ts 里读不到 alertRunChecks 那一行');
  assert.match(line, /timeout:\s*330000/, '等待上限要沿用告页量过的那份，不是拍一个更小的数');
  // 页面不许再自己写一份超时数字打这个接口：两处各写各的，短的必然先假超时
  for (const f of ['views/Alerts.vue', 'views/Settings.vue']) {
    assert.ok(!/run-checks/.test(read(f)), `${f} 不该绕过 api.ts 直接打巡检接口`);
  }
  assert.ok(!/timeout:\s*330000/.test(fnBody(read('views/Alerts.vue'), 'runRound')),
            '告警页自己留了一份 330000：两处副本迟早再次分叉');
});

test('Backups.vue 补推转述 prune_skipped，远端没剪枝不许只报「已推送」', () => {
  const s = fnBody(read('views/Backups.vue'), 'pushNow');
  assert.match(s, /else if \(res\.prune_skipped\)\s*msg\.warning/, '跳过剪枝要出警示色，不能混在成功句里');
  assert.match(s, /远端没有按保留份数剪枝/);
  assert.ok(s.indexOf('res.prune_skipped') < s.indexOf('else msg.success'),
            '成功句必须先于剪枝缺失的判定，否则「已推送 N 份」把桶涨了盖掉');
});
