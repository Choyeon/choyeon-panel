// 批次78 契约守卫（含一条真代码行为用例）。
//
// 这一批改的是"页面之间/页签之间的账不对"：
// A. BackupFiles 只在 onMounted 读 ?job=，同路由换 query 时 Vue 复用实例（router-view 没按
//    fullPath 加 key），连点两个备份任务的「N 份」，筛选还停在第一次点进去的那个；
// B. 应用增删改名不失效 'apps' 缓存，备份页的下拉在最长一分钟内列已删应用、选不到新建应用；
// C. 日志中心的「跟随」切到别的页签还在每 5 秒 fork 一个 journalctl，失败又盖到页面顶部的
//    共用 err 横幅（人在访问统计，看到的是系统日志的报错）；
// D. fmtBytes(null) 因为 Number(null) === 0 画成「0 B」，systemd 没报 MemoryCurrent 时
//    详情页的内存就写成 0 B——把"没读到"说成一个真的用量。
// 这些形状被"顺手简化"回去时，本用例先红。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fmtBytes } from '../src/composables/useFormat.ts';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('fmtBytes：null 是没读到，不是 0 B', () => {
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtBytes(undefined), '—');
  // 0 是一个真实读数（进程确实占着 0 字节也要画得出来），不许跟着一起改成 —
  assert.equal(fmtBytes(0), '0 B');
  assert.equal(fmtBytes(1536), '1.5 KB');
  assert.equal(fmtBytes(-1), '—');
});

test('应用详情页的内存不再把空读数画成 0 B', () => {
  const s = read('views/AppDetail.vue');
  assert.match(s, /\{ label: '内存', value: fmtBytes\(p\.memoryBytes\), mono: true \}/);
  // 契约在共享格式化里（同一个出处），这里只钉"没有本地兜底把 null 折成 0"
  assert.ok(!/memoryBytes \?\? 0/.test(s), 'memoryBytes 不许兜底成 0');
  assert.ok(!/memoryBytes \|\| 0/.test(s), 'memoryBytes 不许兜底成 0');
});

test('BackupFiles 同路由换 ?job= 时跟着换筛选并重查', () => {
  const s = read('views/BackupFiles.vue');
  assert.match(s, /function applyJobQuery\(\): boolean/);
  assert.match(s, /watch\(\(\) => route\.query\.job, \(\) => \{/);
  assert.match(s, /if \(applyJobQuery\(\)\) void load\(\);/);
  // onMounted 里那份手写读取要收进同一个函数，否则两处判定会各说各话
  assert.ok(!/onMounted\(\(\) => \{\s*const q = route\.query\.job/.test(s), 'onMounted 仍自己抄一份 query 解析');
});

test('应用增删改名都作废备份页那份 apps 缓存', () => {
  const apps = read('views/Apps.vue');
  assert.match(apps, /import \{ invalidateCache \} from '\.\.\/composables\/useCachedFetch';/);
  // 删配置、单项目创建、前后端分离创建：三处各一次
  assert.match(apps, /await api\.deleteApp\(r\.id, false\);\s*invalidateCache\('apps'\);\s*load\(\);/);
  assert.match(apps, /msg\.success\('前后端项目已创建'\);\s*invalidateCache\('apps'\);/);
  assert.match(apps, /msg\.success\('创建成功'\);\s*invalidateCache\('apps'\);/);

  const det = read('views/AppDetail.vue');
  assert.match(det, /import \{ invalidateCache \} from '\.\.\/composables\/useCachedFetch';/);
  // 改名走的是 save()（PATCH 带 name），彻底删除走 purge
  assert.match(det, /invalidateCache\('apps'\);\s*\/\/ 「已保存」和「已生效」是两件事/);
  assert.match(det, /await api\.deleteApp\(target, true\);\s*invalidateCache\('apps'\);\s*router\.push\('\/apps'\);/);
});

test('日志中心：跟随只在系统日志页签跑，失败不顶到页级横幅', () => {
  const s = read('views/LogCenter.vue');
  assert.match(s, /const followErr = ref\(''\);/);
  assert.match(s, /watch\(tab, \(t\) => \{\s*if \(t !== 'journal'\) followPoll\.stop\(\);\s*else if \(follow\.value\) followPoll\.start\(\);/);
  // 后台补拉既不抹掉上一次检索的错误横幅，也不往上写
  assert.match(s, /if \(!opts\.silent\) err\.value = '';/);
  assert.match(s, /if \(opts\.silent\) followErr\.value = e\.message \|\| String\(e\);\s*else \{ err\.value = e\.message \|\| String\(e\); followErr\.value = '';\s*\}/);
  // 跟随的错误要在系统日志页签里就地看得见
  assert.match(s, /<NAlert v-if="followErr"[^>]*>\s*跟随的这一轮没取回日志/);
});
