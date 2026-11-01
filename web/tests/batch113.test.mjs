// 批次113 契约守卫：列表「这一轮没读到」与「确认是空的」在页头/计数格上必须是两句话。
//
// 应用页的页头是裸模板串，接口失败那两轮画「共 0 个项目 · 0 个服务运行中」，而列表其实留着上一帧；
// 数据库页三个 loader 的 catch 都把数组清成空，于是「共 0 张表 / 这个库里没有表」「0 行 · 0 列 /
// 这张表没有数据」「已加载 0 个 key」在同一次失败里各写一遍假结论（toast 会消失，格子上的留着）。
// Redis 运行时长那一格更直接把缺字段兜成 0，念出来是「这个 redis 刚起来」。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = (rel) => readFileSync(join(here, '..', 'src', rel), 'utf8');
const APPS = src('views/Apps.vue');
const DBS = src('views/Databases.vue');

test('应用页头：读不到与真的是空两套话，且带上一帧说明', () => {
  assert.match(APPS, /<PageHeader title="应用" :sub="headerSub" \/>/);
  assert.ok(!APPS.includes(':sub="`共 ${topLevel.length} 个项目'), '页头不许再裸念计数');
  assert.match(APPS, /import \{ NButton, unknownNote, unitTag \} from '\.\.\/ui';/);
  // 失败分支必须排在「共 0 个项目」之前，顺序反了读空时仍宣布没有应用
  const sub = APPS.slice(APPS.indexOf('const headerSub = computed'));
  assert.ok(sub.indexOf('unknownNote(\'应用清单\'') < sub.indexOf("'共 0 个项目 · 0 个服务运行中'"));
  // rows 失败时不清空，页头要说明下面是上一次读到的
  assert.match(sub, /loadFailed\.value \? `；\$\{unknownNote\('本轮刷新', loadFailed\.value\)\}，下面是上一次读到的`/);
  assert.ok(!APPS.includes('未读到'), '短名只有 ui.ts 一份');
  // 批次68 的空态判据仍然在同一条线上
  assert.match(APPS, /loadFailed \? '应用列表读取失败，点上方「刷新」重试'/);
});

test('数据库页三处计数各自先判失败再报数', () => {
  for (const [decl, flag, ok] of [
    ['tablesCountText', 'tablesFailed.value', '`共 ${tables.value.length} 张表`'],
    ['rowsCountText', 'tFailed.value', '`${tTotal.value} 行 · ${tCols.value.length} 列`'],
    ['keysCountText', 'rFailed.value', '`已加载 ${rKeys.value.length} 个 key`'],
  ]) {
    assert.match(DBS, new RegExp(`const ${decl} = computed\\(\\(\\) => \\(${flag}\\n\\s*\\? unknownNote`));
    const body = DBS.slice(DBS.indexOf(`const ${decl}`), DBS.indexOf(`const ${decl}`) + 200);
    assert.ok(body.indexOf('unknownNote') < body.indexOf(ok), `${decl} 把失败分支写在了报数后面`);
  }
  assert.match(DBS, /import \{ NButton, cutChars, unknownNote \} from '\.\.\/ui';/);
  assert.ok(!DBS.includes('未读到'), '短名只有 ui.ts 一份');
});

test('计数的读者只有一个：模板只念 computed，空态先问失败旗标', () => {
  assert.match(DBS, /\{\{ tablesCountText \}\}/);
  assert.match(DBS, /\{\{ rowsCountText \}\} · 点击任意行/);
  assert.match(DBS, /\{\{ keysCountText \}\}/);
  for (const bad of ['共 {{ tables.length }} 张表', '{{ tTotal }} 行 · {{ tCols.length }} 列',
                    '已加载 {{ rKeys.length }} 个 key']) {
    assert.ok(!DBS.includes(bad), `「${bad}」又把没读到印成了确认空`);
  }
  // 两处空态：失败短名排在「这个库里没有表 / 这张表没有数据」之前
  assert.match(DBS, /tablesFailed \? unknownNote\('表清单', tablesFailed\) : '这个库里没有表'/);
  assert.match(DBS, /:text="tFailed \? unknownNote\('表数据', tFailed\) : '这张表没有数据'"/);
  // 三个失败旗标都在读取前置空，只有读成功的那一轮才留下计数
  for (const f of ["tablesFailed.value = '';", "tFailed.value = '';", "rFailed.value = '';"]) {
    assert.ok(DBS.includes(f), `缺了 ${f}：上一轮的失败会一直压住新读数`);
  }
  // 续页失败时屏幕上那批键名是真读到的，不许作废整份清单
  assert.match(DBS, /if \(!more\) \{ rKeys\.value = \[\]; rFailed\.value = e\.message \|\| 'key 扫描失败'; \}/);
});

test('Redis 运行时长：缺字段不是 0 小时', () => {
  assert.match(DBS, /\{\{ uptimeText\(redis\.info\) \}\}/);
  assert.ok(!DBS.includes("redis.info?.uptime_in_seconds || 0"), '兜 0 把「没读到」念成「刚起来」');
  const fn = DBS.slice(DBS.indexOf('function uptimeText'), DBS.indexOf('function uptimeText') + 420);
  // INFO 字段是字符串（同 hitRate），而 Number(null) / Number('') 都是 0，必须先挡缺字段
  assert.match(fn, /if \(raw === null \|\| raw === undefined \|\| raw === ''\) return '—';/);
  assert.match(fn, /const v = Number\(raw\);/);
  assert.match(fn, /Number\.isFinite\(v\) && v >= 0 \? `\$\{Math\.round\(v \/ 3600\)\} 小时` : '—'/);
  // 同排其它格子早就如实，判法不许分叉
  assert.match(DBS, /redis\.info\?\.connected_clients \?\? '—'/);
  assert.match(DBS, /redis\.dbsize \?\? '—'/);
});
