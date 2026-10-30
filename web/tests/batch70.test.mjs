// 批次70 契约守卫（静态源码断言）。
//
// 两处"点一下发一次请求、响应回来写进共享的抽屉/弹窗状态"的迟到响应竞态，上一批只把单调序号
// 加在了读取函数上（CronTasks.openLog、Files.open），却没加进同样会写这块状态的执行/配置函数：
//  - CronTasks.runNow：手动执行最长 660s，跑完直接把它塞进抽屉。这十几分钟里用户点了别的任务的
//    「输出」（openLog 会 ++logSeq），抽屉早换到 B；A 跑完会连标题一起把 B 换成 A。
//  - Sites.openConfig：点 A 配置→关→点 B，A 的慢响应覆盖 cfg（后到赢而非最后点赢），弹窗显示着
//    B 却画 A 的 vhost；A 失败分支的 showConfig=false 关掉用户正开着的 B，A 的 finally 熄灭 B 的加载态。
// 修复是同一把单调序号（runNow 与 openLog 共用 logSeq、openConfig 用 cfgSeq），迟到的直接丢弃。
// 把任一处改回"无守卫的裸赋值"时，本用例会红。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');
const fnBody = (s, name) => {
  const m = s.match(new RegExp(`async function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(m, `${name} 函数找不到`);
  return m[0];
};

test('CronTasks.runNow 与 openLog 共用 logSeq，迟到的执行结果不覆盖当前抽屉', () => {
  const s = read('views/CronTasks.vue');
  const run = fnBody(s, 'runNow');
  assert.match(run, /const seq = \+\+logSeq;/, 'runNow 未认领 logSeq');
  // 成功写抽屉前只认领最后一次：seq 变了说明用户已切去看别的任务（守卫与赋值之间允许注释行）
  assert.match(run, /if \(seq !== logSeq\) return;[^\n]*\n(\s*\/\/[^\n]*\n)*\s*const row = rows\.value\.find/,
    '迟到响应仍会覆盖 logData/logRow');
  assert.match(run, /catch \(e: any\) \{\s*if \(seq !== logSeq\) return;\s*err\.value = e\.message/,
    'catch 缺迟到守卫');
  // 回执 toast 仍在（执行确实完成了，只是不再乱动抽屉）
  assert.match(run, /msg\.success\(`「\$\{r\.name\}」执行完成/);
});

test('Sites.openConfig 用 cfgSeq 守卫，迟到的 A 不覆盖当前 B 也不关掉它的弹窗', () => {
  const s = read('views/Sites.vue');
  const fn = fnBody(s, 'openConfig');
  assert.match(s, /let cfgSeq = 0;/);
  assert.match(fn, /const seq = \+\+cfgSeq;/);
  assert.match(fn, /if \(seq !== cfgSeq\) return;\s*cfg\.value = data;/,
    '迟到响应仍会覆盖 cfg');
  assert.match(fn, /catch \(e: any\) \{\s*if \(seq !== cfgSeq\) return;\s*msg\.error\(e\.message\);\s*showConfig\.value = false;/,
    'A 的失败分支会关掉用户正开着的 B 弹窗');
  assert.match(fn, /finally \{\s*if \(seq === cfgSeq\) cfgLoading\.value = false;\s*\}/,
    'A 的 finally 会熄灭 B 的加载态');
});
