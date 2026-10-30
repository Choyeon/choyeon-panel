// 批次67 契约守卫（静态源码断言）。
//
// AppDetail 的「保存后重启服务」按钮原来只调 act('restart')：textarea 里没点「保存 Unit」的
// 改动一个字都没落盘，服务按旧 unit 重启，用户却以为刚编辑的内容生效了。批次67 让它先真正
// 保存并通过 systemd-analyze verify，verify 失败绝不重启。这些断言钉住修复形态，改动被"顺手
// 简化"回去（按钮重新直接调 act('restart')、或 saveUnitFile 不再回报成败）时本用例先红。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('saveUnitFile 回报是否真正落盘（成功 true / 失败与并发 false）', () => {
  const s = read('views/AppDetail.vue');
  // 连点早退要回报 false，否则 saveAndRestart 会把"没保存"当成"已保存"接着重启
  assert.match(s, /if \(unitSaving\.value\) return false;/);
  // 成功路径在 loadUnit 之后明确 return true；catch 回报 false
  assert.match(s, /await loadUnit\(\);\s*return true;/);
  assert.match(s, /catch \(e: any\) \{\s*msg\.error\(e\.message\);\s*return false;/);
});

test('saveAndRestart 先保存、verify 失败不重启、成功后才 restart', () => {
  const s = read('views/AppDetail.vue');
  assert.match(s, /async function saveAndRestart\(\)/);
  assert.match(s, /if \(!\(await saveUnitFile\(\)\)\) return;/, '保存失败必须在这里断掉，不许接着重启');
  assert.match(s, /if \(!\(await saveUnitFile\(\)\)\) return;\s*await act\('restart'\);/);
});

test('「保存后重启服务」按钮绑到 saveAndRestart，不再直接 act(restart)', () => {
  const s = read('views/AppDetail.vue');
  // 找那颗按钮：标签仍是「保存后重启服务」，但点击处理已换成 saveAndRestart
  const btn = s.match(/<NButton[^>]*>保存后重启服务<\/NButton>/);
  assert.ok(btn, '按钮标签丢了');
  assert.ok(/@click="saveAndRestart"/.test(btn[0]), '按钮仍直接重启、没先保存：改动会被静默丢弃');
  assert.ok(!/act\('restart'\)/.test(btn[0]), '不许残留旧的 act(restart) 绑定');
  // 忙态同时跟着保存和重启两段走，保存那一程按钮也得锁住
  assert.match(btn[0], /:loading="unitSaving \|\| acting === 'restart'"/);
  // 批次76 在同一串条件后面追加了 unitUnreadable：unit 读不动时点保存，面板手里没有旧内容，
  // 失败回滚会把「没读到」当成「盘上没有」而 unlink 正在生效的那份，所以那一档也得锁住。
  assert.match(btn[0], /:disabled="!!acting \|\| unitSaving \|\| unitUnreadable"/);
});
