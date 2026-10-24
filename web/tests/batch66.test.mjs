// 批次66 契约守卫（静态源码断言）。
//
// 全站 toast 走查的结论：96 个用户触发的后端操作里，错误反馈 0 缺失，但有 9 个写操作
// 「只有出错才吭声、成功了什么都不说」，还有 1 个（退出登录吊销失败）把异常静默吞掉。
// 用户点完不知道成没成，只能盯着表格自己变——这类缺陷构建、类型检查、单测都发现不了。
// 这里钉住每一处补上的成功/告警回执，改动被「顺手简化」回去时本用例先红。
//
// 口径：成功用 msg.success，吊销失败这类「操作已降级但仍要继续」用 msg.warning，
// 错误一律 msg.error（原本就在，不在本批改动范围）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');
// 取一个顶层 `async function name(...) { ... }` 的函数体（右括号顶格）。
const fnBody = (s, name) => {
  const m = s.match(new RegExp(`async function ${name}\\([\\s\\S]*?\\n\\}`));
  assert.ok(m, `找不到函数 ${name}：入口被改名/删除了`);
  return m[0];
};

test('CronTasks.vue 引入 useMessage 并给新建/编辑/执行/启停都补成功回执', () => {
  const s = read('views/CronTasks.vue');
  assert.match(s, /from 'naive-ui'[\s\S]*?useMessage|useMessage[\s\S]*?from 'naive-ui'/, '没导入 useMessage');
  assert.match(s, /const msg = useMessage\(\)/);
  // 保存：区分新建与编辑，都带上任务名
  const save = fnBody(s, 'save');
  assert.match(save, /msg\.success\(editing\.value \? `任务「\$\{body\.name\}」已更新` : `任务「\$\{body\.name\}」已创建`\)/);
  // 手动执行：把这一轮的结论（ok/timeout/exit N）直接说出口，不用点开抽屉
  const run = fnBody(s, 'runNow');
  assert.match(run, /msg\.success\(`「\$\{r\.name\}」执行完成：\$\{res\?\.status \|\| '已运行'\}`\)/);
  // 启停开关
  const tg = fnBody(s, 'toggle');
  assert.match(tg, /msg\.success\(val \? `「\$\{r\.name\}」已启用` : `「\$\{r\.name\}」已停用`\)/);
});

test('Login.vue 登录/初始化成功后给一句回执（跨路由由 App 级 provider 承载）', () => {
  const s = read('views/Login.vue');
  assert.match(s, /import \{[^}]*useMessage[^}]*\} from 'naive-ui'/);
  assert.match(s, /const msg = useMessage\(\)/);
  const sub = fnBody(s, 'submit');
  assert.match(sub, /msg\.success\(needsSetup\.value \? '管理员已创建，已自动登录' : '登录成功，欢迎回来'\)/);
  // 回执必须在跳转之前发出，否则换页后这条 toast 未必挂得住
  assert.ok(sub.indexOf('msg.success') < sub.indexOf("router.replace('/dashboard')"), '成功回执要在 router.replace 之前');
});

test('Databases.vue SQL 控制台执行完成补成功回执（点明在哪个库）', () => {
  const s = read('views/Databases.vue');
  const run = fnBody(s, 'runSql');
  // 服务端 ON_ERROR_STOP 下语法/执行错会 throw 走 catch，能到这里就是真执行完了
  assert.match(run, /msg\.success\(`SQL 已在库「\$\{r\.db \|\| sqlDb\.value \|\| 'postgres'\}」执行完成`\)/);
  assert.match(run, /sqlOut\.value = r\.result \|\| '\(无返回\)'/);
});

test('AppDetail.vue 启停/重启按动词补成功回执', () => {
  const s = read('views/AppDetail.vue');
  const act = fnBody(s, 'act');
  assert.match(act, /msg\.success\(\{ start: '已启动服务', stop: '已停止服务', restart: '已重启服务' }\[verb\] \|\| `已执行 \$\{verb\}`\)/);
  assert.match(act, /catch \(e: any\) \{\s*msg\.error\(e\.message\)/);
});

test('Settings.vue 生成 2FA 密钥这一步补引导性成功回执', () => {
  const s = read('views/Settings.vue');
  const st = fnBody(s, 'startTwofa');
  // 只生成密钥、尚未开启，文案要说清下一步，避免用户以为扫完码就生效
  assert.match(st, /msg\.success\('验证密钥已生成，请在 App 里扫码后填入动态口令'\)/);
  // 真正开启/关闭的成功回执原本就有，别被一起删掉
  assert.match(fnBody(s, 'commitTwofa'), /msg\.success\('两步验证已开启/);
});

test('Backups.vue 任务与异地目标的启停用名补成功回执', () => {
  const s = read('views/Backups.vue');
  assert.match(fnBody(s, 'toggleEnabled'), /msg\.success\(`备份任务「\$\{r\.name\}」已\$\{r\.enabled \? '停用' : '启用'\}`\)/);
  assert.match(fnBody(s, 'toggleTarget'), /msg\.success\(`异地目标「\$\{r\.name\}」已\$\{r\.enabled \? '停用' : '启用'\}`\)/);
});

test('Shell.vue 退出登录吊销失败不再静默吞异常，改给告警', () => {
  const s = read('Shell.vue');
  const out = fnBody(s, 'doLogout');
  // 原来的空 catch 是「吞掉」：本地退了但服务端票据没吊销，用户却以为彻底失效了
  assert.ok(!/catch \{\s*\}/.test(out), 'doLogout 的 catch 又变回空的静默吞异常');
  assert.match(out, /msg\.warning\('已退出本机，但服务端未能吊销本次登录凭证/);
  // 无论吊销成败，本地清理与跳转照旧（不能把人锁在面板里）
  assert.match(out, /clearSession\(\);\s*router\.replace\('\/login'\)/);
});
