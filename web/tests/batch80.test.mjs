// 批次80 契约守卫：操作审计的过滤与翻页。
//
// 这一批把「过滤」从已加载的那一屏挪到服务端，并补上「加载更早的记录」。
// 被钉住的形状：
// 1. 过滤词防抖后才发请求，且 q 交给服务端（客户端再 filter 一遍就是把老毛病藏回去）；
// 2. 条数文案用服务端给的 matched（整表匹配数），不是本页行数；
// 3. 换搜索词/换每页条数/刷新都会清掉追加的旧页，迟到响应也不许拼进来；
// 4. 「加载更早」用 before 游标（上一页最旧记录的 id），连点不能打两页；
// 5. 翻到没更多时按钮消失，失败时把原因留在原位可重试。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('api.ts：limit/q/before 三个参数都透传，不自带默认值', () => {
  const s = read('api.ts');
  assert.match(s, /audit: \(opts: \{ limit\?: number; q\?: string; before\?: number \} = \{\}\) => \{/);
  assert.match(s, /if \(opts\.q\) p\.set\('q', opts\.q\);/);
  assert.match(s, /if \(opts\.before\) p\.set\('before', String\(opts\.before\)\);/);
});

test('审计页：过滤词防抖后走服务端，客户端不再自己 filter', () => {
  const s = read('views/Audit.vue');
  assert.match(s, /debounce = window\.setTimeout\(\(\) => \{ q\.value = v\.trim\(\); \}, 300\);/);
  assert.match(s, /loader: \(\) => api\.audit\(\{ limit: limit\.value \?\? undefined, q: q\.value \|\| undefined \}\)/);
  assert.doesNotMatch(s, /const filtered = computed/, '客户端 filter 只会过滤已加载的这一屏');
});

test('审计页：条数文案用服务端的 matched，不说成加载了多少就是多少', () => {
  const s = read('views/Audit.vue');
  assert.match(s, /const matched = computed\(\(\) => data\.value\?\.matched \?\? null\);/);
  assert.match(s, /const head = q\.value \? `匹配 \$\{n\} 条` : `共 \$\{n\} 条`;/);
  assert.match(s, /还有更早的记录未加载/);
});

test('审计页：条件一变就清掉追加页，迟到响应不许拼进来', () => {
  const s = read('views/Audit.vue');
  assert.match(s, /watch\(data, \(d\) => \{\s*\n\s*older\.value = \[\];\s*\n\s*olderError\.value = '';\s*\n\s*moreAfter\.value = !!d\?\.hasMore;/);
  assert.match(s, /if \(q\.value !== mineQ \|\| limit\.value !== mineLimit\) return;/);
});

test('审计页：加载更早走 id 游标，连点与失败都可控', () => {
  const s = read('views/Audit.vue');
  assert.match(s, /before: last\.id/);
  assert.match(s, /if \(!canLoadOlder\.value\) return;/);
  assert.match(s, /const canLoadOlder = computed\(\(\) => moreAfter\.value && rows\.value\.length > 0 && !olderBusy\.value\);/);
  assert.match(s, /<NButton v-if="canLoadOlder" size="small" tertiary :loading="olderBusy" @click="loadOlder">/);
  assert.match(s, /重试加载更早的记录/);
  assert.match(s, /v-if="canLoadOlder \|\| olderError"/);
});

test('审计页：空态说清是哪个词没匹配，不说成「暂无」', () => {
  const s = read('views/Audit.vue');
  assert.match(s, /q \? `没有匹配「\$\{q\}」的记录` : '暂无操作记录'/);
});
