// 批次82 契约守卫：告警历史的「加载更早的记录」。
//
// 1. 游标取已加载里最小的 id（未确认行钉在最上面，最后一行不是最小 id）；
// 2. 换条件/刷新时清掉追加页，迟到响应不许拼进来；
// 3. 连点与失败都可控：条件不满足不发请求，失败留在原位可重试；
// 4. 计数行仍用服务端 matched，窗口边界改由追加页之后的 hasMore 决定。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('告警页：before 作为游标发给服务端', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /p\.set\('before', String\(before\)\);/);
  assert.match(s, /const before = Math\.min\(\.\.\.ids\);/);
});

test('告警页：条件一变就清掉追加页，迟到响应不许拼进来', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /watch\(data, \(d\) => \{\s*\n\s*older\.value = \[\];\s*\n\s*olderError\.value = '';\s*\n\s*moreAfter\.value = !!d\?\.hasMore;/);
  assert.match(s, /if \(q\.value !== mineQ \|\| level\.value !== mineLevel \|\| limit\.value !== mineLimit\) return;/);
});

test('告警页：加载更早走 id 游标，连点与失败都可控', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /if \(!canLoadOlder\.value\) return;/);
  assert.match(s, /const canLoadOlder = computed\(\(\) => moreAfter\.value && rows\.value\.length > 0 && !olderBusy\.value\);/);
  assert.match(s, /<NButton v-if="canLoadOlder" size="small" tertiary :loading="olderBusy" @click="loadOlder">/);
  assert.match(s, /重试加载更早的记录/);
  assert.match(s, /v-if="canLoadOlder \|\| olderError"/);
});

test('告警页：窗口边界由追加页之后的 hasMore 决定', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /const hasOlder = computed\(\(\) => moreAfter\.value\);/);
  assert.match(s, /\{\{ countText \}\}\{\{ hasOlder \? '；还有更早的记录未加载' : '' \}\}/);
  assert.match(s, /const matched = computed\(\(\) => data\.value\?\.matched \?\? null\);/);
});
