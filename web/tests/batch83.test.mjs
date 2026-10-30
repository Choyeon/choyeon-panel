// 批次83 契约守卫：三处「静默截断」必须自己说出被截掉了多少。
//
// 截断本身是合理的（提示浮层、表格单元格、预览都有尺寸上限），
// 但截到一半不吭声，读的人会以为「本来就这么长 / 一共就这些」。
// 三处都要：真实的原始总量 + 全文去哪儿看。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('安全通告：只列前 8 条时要带上真实总数', () => {
  const s = read('views/Services.vue');
  assert.match(s, /const all = patches\.value\?\.advisories \|\| \[\];/);
  assert.match(s, /all\.length > 8 \? `\$\{ids\} 等 \$\{all\.length\} 条` : ids/);
  // 数字只能来自同一个 all，不许再 slice 一次去数
  assert.doesNotMatch(s, /advisories \|\| \[\]\)\.slice\(0, 8\)\.length/);
});

test('Redis 值格：截断写进单元格文本本身（浮层拿的就是这份文本）', () => {
  const s = read('components/RedisKeyPanel.vue');
  assert.match(s, /const VALUE_CELL_MAX = 500;/);
  assert.match(s, /const cut = s\.length <= VALUE_CELL_MAX/);
  assert.match(s, /已截断，共 \$\{s\.length\} 字符，完整值请用 CLI 读取/);
  assert.match(s, /return h\('span', \{ class: 'mono-dim', title: cut \}, cut\);/);
  // 全文入口仍是共享触发器，不许在本文件里另起一个 hover-only 的 tooltip
  assert.match(s, /ellipsis: tapEllipsis/);
  assert.doesNotMatch(s, /tooltip: \{ trigger: 'hover' \}/);
});

test('JSON 预览：截断要说出来，并指向行详情', () => {
  const s = read('views/Databases.vue');
  assert.match(s, /const text = JSON\.stringify\(v, null, 2\);/);
  assert.match(s, /text\.length > 3000/);
  assert.match(s, /预览已截断，共 \$\{text\.length\} 字符；点开行详情查看完整内容/);
  // 序列化只做一次：截断判断与展示必须基于同一份字符串
  assert.doesNotMatch(s, /JSON\.stringify\(v, null, 2\)\.slice\(0, 3000\)/);
});
