// 「按天流量与环比」前端的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次㊺B 立下的三条口径，它们都属于"改坏了也照样能构建"的那类：
// 1) 天数档位只能从 /system/log-options 的 stats_days 拿（后端 access_stats.DAY_LADDER）。
//    页面抄一份 3/7/14/30，服务端放宽上限后下拉里还是老档位、校验早已放行，失真很安静。
// 2) 环比的结论只能照抄服务端的 comparable / previous.reason。页面若拿 rows 自己算涨跌，
//    就会在"上一周期只读到一半"时照样画出一个百分比——那正是㊺A 花最多力气避免的假数字。
// 3) 按天回看不许自动跑：它读的是轮替出来的世代（最多 31 份、每份解压后 32MB），
//    进页签就打一发，等于把「看趋势」变成一次没人要求的整盘扫描。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'LogCenter.vue');
const src = readFileSync(SRC, 'utf8');

test('按天的天数档位只从 log-options 下发，页面不抄数字', () => {
  assert.ok(src.includes('stats_days'), 'dayLadder 必须读 /system/log-options 的 stats_days');
  assert.ok(!/\[\s*3\s*,\s*7\s*,\s*14\s*,\s*30\s*\]/.test(src), '不要在页面里抄 DAY_LADDER 的字面值');
  assert.ok(/df\.value\.days\s*=\s*v/.test(src), '默认天数要由档位回填，不写死');
});

test('环比判词只照抄服务端的 comparable 与 previous.reason', () => {
  assert.ok(/d\.comparable/.test(src), '涨跌与否要认服务端给的 comparable');
  assert.ok(/previous\?\.reason/.test(src), '不做环比的原因要认服务端给的 reason');
  // 页面自己算百分比的那几种写法都不许出现（分子分母来自 rows/current/previous 也算）
  assert.ok(!/deltaPvPct\s*[+\-*/]/.test(src.replace(/Number\(d\.deltaPvPct\)/g, '')),
    'deltaPvPct 只能显示，不能参与页面侧的再计算');
  assert.ok(!/current\.pv\s*-\s*previous|previous\.pv\s*\)/.test(src),
    '页面不许用 current/previous 自己拼环比');
});

test('按天回看是手点动作，不进页签不自动打', () => {
  assert.ok(/nginx-daily/.test(src), '要真的打 /system/nginx-daily');
  assert.ok(/:disabled="dLoading \|\| !hasAccessSource"/.test(src),
    '来源未知时按钮该是灰的，而不是点下去报 400');
  const autoRun = src.match(/watch\(tab,[^\n]*\n?/g) || [];
  autoRun.forEach((line) => assert.ok(!line.includes('runDaily'),
    `进页签的自动执行里不该出现按天扫描：${line.trim()}`));
});

test('按天的诚实字段要落到画面上', () => {
  // 封顶、被截断的世代、这次读不到的世代、窗口外的行——少点一条就变成"看着像全量"
  ['uvFloored', 'truncatedFiles', 'unreadableFiles', 'outside', 'malformed', 'historyFrom']
    .forEach((k) => assert.ok(src.includes(k), `${k} 要在按天的口径里露出来`));
  assert.ok(/d\.file === sf\.value\.file/.test(src),
    '换了来源但没重取时必须说明"这份还是上一个来源的数"');
});
