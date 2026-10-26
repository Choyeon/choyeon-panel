// 批次68 契约守卫（静态源码断言）。
//
// 这一批是"读不到的第三态"和"并发/迟到响应竞态"两类缺陷的收口：
// 1) 列表读取失败时，EmptyBox 不能再把"没取到数据"显示成"暂无 XX，去点新建"——那是让
//    用户以为数据真的空了。改用一个 loadFailed/err 标志，失败时如实说"读取失败，点刷新重试"。
// 2) Backups 的启用/停用 toast 引用了备份任务表根本没有的 r.name，批次66 上线后当场弹
//    「undefined」；改用 displayName(r)。
// 3) Files/AppDetail/CronTasks 里"点一下发一次请求、响应回来写进共享状态"的地方缺单调序号：
//    快速切行时迟到的旧响应会画进已经换掉的行/抽屉；Files 的 extract/pack 还缺重入守卫，
//    删除在飞时点解压会让两个分钟级写操作并发。这些断言钉住修复形态，被"顺手简化"回去时先红。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('Backups 启用/停用 toast 用 displayName(r)，不再引用不存在的 r.name', () => {
  const s = read('views/Backups.vue');
  const fn = s.match(/async function toggleEnabled\(r: any\)[\s\S]*?\n\}/);
  assert.ok(fn, 'toggleEnabled 函数找不到');
  assert.ok(/msg\.success\(`备份任务「\$\{displayName\(r\)\}」/.test(fn[0]),
    'toast 又去读 r.name：备份任务表没有 name 列，会弹 undefined');
  assert.ok(!/\$\{r\.name\}/.test(fn[0]), '残留了 ${r.name}');
});

test('Apps 列表读取失败显示第三态，不再谎报"还没有应用"', () => {
  const s = read('views/Apps.vue');
  assert.match(s, /const loadFailed = ref\(''\);/);
  assert.match(s, /loadFailed\.value = e\.message \|\| '应用列表读取失败';/);
  assert.match(s, /loadFailed\.value = '';/, '成功路径必须清空 loadFailed');
  assert.ok(s.includes(`loadFailed ? '应用列表读取失败，点上方「刷新」重试'`),
    'EmptyBox 没接上读取失败第三态');
});

test('Users 列表读取失败显示第三态，复用 useCachedFetch 的 error', () => {
  const s = read('views/Users.vue');
  assert.ok(s.includes(`error ? '用户列表读取失败，点上方「刷新」重试' : '暂无用户'`),
    'EmptyBox 仍把读取失败显示成"暂无用户"');
});

test('Alerts 历史读取失败显示第三态，不与筛选空态混为一谈', () => {
  const s = read('views/Alerts.vue');
  assert.match(s, /const histFailed = computed\(\(\) => !data\.value && !!error\.value\);/);
  assert.ok(s.includes(`histFailed ? '告警历史读取失败，点「刷新」重试'`),
    'EmptyBox 没接上告警历史读取失败第三态');
});

test('BackupFiles 列表读取失败显示第三态', () => {
  const s = read('views/BackupFiles.vue');
  assert.match(s, /const loadFailed = ref\(''\);/);
  assert.match(s, /loadFailed\.value = e\.message \|\| '备份文件列表读取失败';/);
  assert.ok(s.includes(`loadFailed ? '备份文件列表读取失败，点上方「刷新」重试'`),
    'EmptyBox 没接上读取失败第三态');
});

test('AppDetail 部署记录读取失败与主数据隔离，副数据失败不吞整页', () => {
  const s = read('views/AppDetail.vue');
  assert.match(s, /const depsErr = ref\(''\);/);
  // /deployments 单独成功清空、单独 catch 记 depsErr，而不是走 loadErr 整页失败
  assert.match(s, /deps\.value = list;\s*depsErr\.value = '';/);
  assert.match(s, /depsErr\.value = e\?\.message \|\| '部署记录读取失败';/);
  assert.ok(s.includes(`:text="depsErr || '暂无部署记录'"`),
    '部署记录空态没接上 depsErr 第三态');
});

test('Files 打开内容迟到响应丢弃 + 解压/打包重入守卫', () => {
  const s = read('views/Files.vue');
  assert.match(s, /let openSeq = 0;/);
  assert.match(s, /const seq = \+\+openSeq;/);
  assert.ok((s.match(/if \(seq !== openSeq\) return;/g) || []).length >= 2,
    '成功与失败路径都要有迟到响应守卫');
  // extract/pack 各自的第一行重入守卫（紧贴 busyOp 赋值之前）
  assert.match(s, /if \(busyOp\.value\) return;\s*busyOp\.value = 'extract';/);
  assert.match(s, /async function pack\(\) \{\s*if \(busyOp\.value\) return;/,
    'pack 缺重入守卫：删除在飞时点打包会并发两个写操作');
  // 解压按钮（渲染函数形态）与打包按钮（模板形态）都要随 busyOp 锁住
  assert.ok(/type: 'primary', loading: busyOp\.value === 'extract', disabled: !!busyOp\.value/.test(s),
    '解压按钮没锁 busyOp');
  assert.ok(/:disabled="!!busyOp \|\| !checkedKeys\.length"/.test(s),
    '打包按钮没锁 busyOp');
});

test('CronTasks 日志抽屉迟到响应丢弃，不再把上一行画进当前抽屉', () => {
  const s = read('views/CronTasks.vue');
  assert.match(s, /let logSeq = 0;/);
  assert.match(s, /const seq = \+\+logSeq;/);
  const fn = s.match(/async function openLog\(r: Task\)[\s\S]*?\n\}/);
  assert.ok(fn, 'openLog 找不到');
  assert.ok(/if \(seq !== logSeq\) return;\s*logData\.value = data;/.test(fn[0]),
    '迟到响应仍会覆盖当前抽屉的 logData');
  assert.ok(/if \(seq !== logSeq\) return;/.test(fn[0]), 'catch 缺迟到守卫');
  assert.match(fn[0], /if \(seq === logSeq\) logLoading\.value = false;/,
    'finally 无条件熄灭 loading：迟到的旧请求会关掉新行的加载态');
});
