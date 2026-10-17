// 备份任务「执行留痕」的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次55 立下的四条口径：
// 1) 留痕的三个字段（last_run / last_status / last_note）由 runner 自己写库、由列表接口
//    原样下发。页面若从 `files[]` 的最新 mtime 反推"最近一次是不是成功"，就有了第二个
//    判据：一条每晚 SystemExit 的任务照样有别的归档留在目录里，页面会显示绿色。
// 2) 状态词不翻译。ok / timeout / exit N 是执行器写的账，定时任务页与告警正文用的也是
//    这一串；备份页另起一套「成功/失败」，同一个 #3 在两页读出来是两个名字。
// 3) 「未执行」要显式写。新建的计划一次都没跑过（last_status 是空串），空格会被读成
//    "接口没返回"，而它其实是"还没到点"。
// 4) 失败原因必须折行在格子里，不能只挂在 title 上（批次㉕：触屏没有 hover）。
//    `.cell-lastrun` 也只有一个出处：它在 style.css 里，两个页面共用。
// 5) 两页同形：「未执行」写在时间列，结论列只放执行器留下的 token。位置一互换，
//    同一个 #3 在两页就要读成两种意思。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const VIEWS = join(HERE, '..', 'src', 'views');
const src = readFileSync(join(VIEWS, 'Backups.vue'), 'utf8');
const script = src.slice(0, src.indexOf('</script>'));
const cron = readFileSync(join(VIEWS, 'CronTasks.vue'), 'utf8');
const css = readFileSync(join(HERE, '..', 'src', 'style.css'), 'utf8');

test('两列读服务端留痕，不从产物时间反推结果', () => {
  assert.match(script, /title: '上次执行', key: 'last_run', width: \d+, render: lastRunCell/);
  assert.match(script, /title: '结果', key: 'last_status', minWidth: \d+, render: runResultCell/);
  assert.match(script, /String\(r\.last_status \|\| ''\)/);
  assert.match(script, /fmtDateTime\(r\.last_run\)/);
  // 产物列可以读 mtime，但执行状态不行：那是两个事实
  assert.ok(!/last_status[^}]*files\[0\]/.test(script), '执行结果不得由 files[] 反推');
});

test('状态词原样显示，不另发明一套中文标签', () => {
  const cell = script.slice(script.indexOf('function runResultCell'), script.indexOf('const columns'));
  assert.match(cell, /\{ default: \(\) => s \}/, '标签正文必须是执行器写的那个词');
  // 只拦"引号里的中文状态词"——注释里提到失败原因是正当的，代码里映射才是第二个口径
  for (const bad of ["'成功'", "'失败'", '"成功"', '"失败"'])
    assert.ok(!cell.includes(bad), `${bad}：翻译一遍就有第二个口径，告警正文里写的还是 ok/exit N`);
});

test('从没跑过的任务显示「未执行」而不是空白', () => {
  assert.match(script, /:\s*'未执行'\)/, '时间格要显式写未执行');
  assert.match(script, /if \(!s\) return h\('span', \{ class: 'cell-sub' \}, '—'\);/, '结果格用破折号，不给红tag');
  assert.match(script, /title: r\.last_run \? `\$\{r\.last_run\} \(本地\)` : '这条任务还没有执行记录'/);
});

test('失败原因折行在格子里，不是只挂在 hover 上', () => {
  const cell = script.slice(script.indexOf('function runResultCell'), script.indexOf('const columns'));
  assert.match(cell, /class: 'cell-sub cp-prose', title: r\.last_note/, 'cp-prose 折行 + title 给全文');
  assert.match(cell, /if \(s === 'ok' \|\| !r\.last_note\) return tag;/, '没有原因时不给空的第二行');
});

test('.cell-lastrun 只在 style.css 定义一次，两个页面共用', () => {
  assert.match(css, /^\.cell-lastrun \{[^}]*display: flex/m, '全局要有这条规则');
  assert.ok(!cron.includes(':deep(.cell-lastrun)'), '定时任务页的 scoped 副本要删掉，否则两处会漂移');
  assert.match(cron, /class: 'cell-lastrun'/, '定时任务页仍然用同一个类');
});

test('两页把「未执行」写在同一列，结论列只放执行器的 token', () => {
  const cronCell = cron.slice(cron.indexOf('function statusTag'), cron.indexOf('const cols'));
  assert.match(cronCell, /if \(!s\) return h\('span', \{ class: 'cell-sub' \}, '—'\);/,
    '定时任务页的结果格要写破折号，"从没跑过"归时间列说');
  assert.match(cronCell, /:\s*'未执行';/, '定时任务页的时间格要显式写未执行');
  // 着色档位也得同源：两页各判一次，迟早变成「这页红、那页橙」
  const TYPE = "const type = s === 'ok' ? 'success' : s === 'timeout' ? 'error' : 'warning';";
  assert.ok(script.includes(TYPE), '备份页按 ok / timeout / 其它三档着色');
  assert.ok(cronCell.includes(TYPE), '定时任务页必须是同一串三档');
});
