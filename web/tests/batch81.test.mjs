// 批次81 契约守卫：告警历史的关键字过滤。
//
// 这一批把「过滤来源/内容」从已加载的那一屏挪到服务端。被钉住的形状：
// 1. 关键字防抖后作为 q 发给服务端，客户端不再自己 filter；
// 2. 计数行用服务端给的 matched（整表匹配数），并写明窗口边界（还有更早的未加载）；
// 3. 空态点名所搜的词，不再用「没有符合筛选条件的告警记录」把窗口边界说成历史边界；
// 4. 级别、条数、关键字三者一起进缓存 key，换一个条件不会画回上一组结果。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('告警页：关键字防抖后走服务端，客户端不再自己 filter', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /debounce = window\.setTimeout\(\(\) => \{ q\.value = v\.trim\(\); \}, 300\);/);
  assert.match(s, /if \(q\.value\) p\.set\('q', q\.value\);/);
  assert.doesNotMatch(s, /const k = kw\.value\.trim\(\)\.toLowerCase\(\);/);
  assert.doesNotMatch(s, /没有符合筛选条件的告警记录/);
});

test('告警页：缓存 key 带上级别、条数与关键字', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /key: \(\) => `alerts:history:\$\{level\.value\}:\$\{limit\.value \?\? ''\}:\$\{q\.value\}`/);
  assert.match(s, /type HistoryResp = \{[^}]*matched: number \| null; hasMore: boolean/);
});

test('告警页：计数行用服务端 matched，并标出还有更早的没加载', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /const matched = computed\(\(\) => data\.value\?\.matched \?\? null\);/);
  // 批次82 起窗口边界由追加页之后的 hasMore 决定（加载更早之后要能收口）
  assert.match(s, /const hasOlder = computed\(\(\) => moreAfter\.value\);/);
  assert.match(s, /const head = q\.value \? `匹配 \$\{n\} 条` : \(level\.value \? `该级别共 \$\{n\} 条` : `共 \$\{n\} 条`\);/);
  assert.match(s, /还有更早的记录未加载/);
});

test('告警页：空态点名所搜的词', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /q \? `没有匹配「\$\{q\}」的告警记录`/);
});
