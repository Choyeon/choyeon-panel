// 批次61B 契约守卫（静态源码断言）。
//
// 这一批修的是六个"错误被粉饰成正常"的前端缺陷。它们的共同点：构建、类型检查、
// 甚至单测都发现不了——缺陷本身就长在"失败分支写成了成功分支"的位置上。
// 这里钉住每处修复的形态，改动一旦被"顺手简化"回去，本用例先红。
//
// - Users.vue：行内动作从单槽 {id,verb} 换成按行号的 map（同 Apps.vue）。单槽 +
//   finally 无条件清空 = B 行顶掉 A 行的在飞状态，两行按钮同时提前恢复。
// - Alerts.vue：/alerts/open 读取失败时汇总卡画「—」并点名失败，不是「未确认 0 条」。
// - Doctor.vue：重新检查失败仍留着旧报告时，必须标成"上一次的结果"并给出读取时间。
// - BackupFiles.vue：恢复响应按轮次序号认领目标，迟到的一份不许写进别的文件弹窗。
// - Terminal.vue：断线时 xterm 按键本地回显 + 每个断线片段点名一次，不再静默丢弃。
// - Backups.vue：PG 库列表读取失败与"实例上没有库"分开表达（pgReadError）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('Users.vue 行内动作按行号记账，不再用单槽 rowOp', () => {
  const s = read('views/Users.vue');
  assert.ok(!/rowOp/.test(s), '单槽 rowOp 回来了：B 行会顶掉 A 行的在飞状态，finally 又会清掉 B 的');
  assert.match(s, /const rowBusyMap = ref<Record<number, string>>\(\{\}\)/);
  // finally 只清自己那一行：endRowOp(id)，不许再出现"清空整个状态"的写法
  assert.match(s, /finally \{\s*endRowOp\(/);
  assert.ok(!/finally \{\s*rowBusyMap\.value = \{\}/.test(s), 'finally 整表清空会把别的行在飞的动作一起放行');
});

test('Alerts.vue 未确认统计读不到时画「—」，不折成 0 条绿灯', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /const openFailed = computed\(\(\) => !openData\.value && !!openError\.value\);/);
  assert.ok(s.includes('未确认 —（未确认统计读取失败）'), '失败态必须点名"读取失败"，只画 — 会被读成 0');
  assert.match(s, /:disabled="openFailed \|\| !openTotal/, '读不到未确认统计时「全部确认」要禁用');
});

test('Doctor.vue 失败后保留的旧报告标成上一次结果并带读取时间', () => {
  const s = read('views/Doctor.vue');
  assert.match(s, /const stale = ref\(false\)/);
  assert.match(s, /const checkedAt = ref\(''\)/);
  // 成功才清 stale、并记下这次读取的时刻；失败时只要还有旧报告就要标 stale
  assert.match(s, /stale\.value = false;/);
  assert.match(s, /stale\.value = !!report\.value;/);
  assert.ok(s.includes('下面的汇总与条目是上一次的报告'), '要明说旧报告不代表当前状态');
});

test('BackupFiles.vue 恢复响应按轮次认领目标，关闭弹窗也作废在飞轮次', () => {
  const s = read('views/BackupFiles.vue');
  assert.match(s, /let restoreSeq = 0/);
  assert.match(s, /const seq = restoreSeq;/);
  assert.match(s, /if \(seq !== restoreSeq\) return;/, '迟到的成功响应不许写进已经换目标的弹窗');
  assert.match(s, /if \(seq === restoreSeq\) \{\s*restoreErr\.value/, '迟到的失败响应同样不许落到别的文件上');
  assert.match(s, /watch\(showRestore, \(v\) => \{\s*\/\/[\s\S]*?if \(!v\) restoreSeq \+= 1;/, '关闭弹窗要作废在飞轮次');
  // 忙位跟着请求走，不跟着弹窗走：迟到轮次的 finally 也必须释放 restoring，
  // 否则换过目标后那个标志永远挂着，任何文件都点不动「恢复覆盖」。
  assert.match(s, /finally \{[\s\S]{0,240}?\n\s{4}restoring\.value = false;\s*\}/);
  assert.ok(!/if \(seq === restoreSeq\) restoring\.value = false/.test(s), '按轮次清忙位会把在飞的锁留在屏上');
});

test('Terminal.vue 断线按键本地回显并每片段点名一次', () => {
  const s = read('views/Terminal.vue');
  assert.match(s, /term\.onData\(\(data\) => \{[\s\S]*?if \(sendInput\(data\)\) return;[\s\S]*?term\?\.write\(data\);/);
  assert.match(s, /if \(!inputLostNoticed\)/, '提示每个断线片段只写一次，不能每敲一键刷一行');
  assert.match(s, /inputLostNoticed = false;/, '重连成功后要允许下一次断线重新点名');
});

test('Backups.vue PG 库列表读不到时与「没有库」分开表达', () => {
  const s = read('views/Backups.vue');
  assert.match(s, /const pgReadError = ref\(''\)/);
  // catch 里除了清空列表还要记下失败原因；成功要清空失败原因
  assert.match(s, /pgReadError\.value = e\?\.message/);
  assert.match(s, /pgDbs\.value = await api\.pgDbs\(\);\s*pgReadError\.value = '';/);
  assert.ok(s.includes('不代表这台实例上没有其它库'), '失败说明句要明说"只有 all"是读取失败的表象');
});
