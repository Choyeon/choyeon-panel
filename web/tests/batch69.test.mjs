// 批次69 契约守卫（静态源码断言）。
//
// 应用的环境变量存在面板数据库里，落盘只发生在「保存配置」与「部署」那两步，unit 写的又是
// `EnvironmentFile=-<目录>/.panel.env`（开头的 - 表示文件不存在也不报错）。安装目录被手工换过
// 之后那份文件就没了：systemd 静默跳过，进程用的是别处的值，界面却照常显示数据库这一份——
// 线上真的照着界面输口令登了半小时后台。这一批补的是"界面必须承认这两份可能已经分家"：
// 1) 详情页环境变量页要按后端 envState 摆出漂移横幅，值一个都不显示，只说状态和键数；
// 2) 修复入口复用既有 save()（PATCH 会重写 .panel.env 并在服务在跑时重启），不能另起一个
//    会改值的写接口，也不能让 viewer 看见按钮（它改不动）；
// 3) 没在跑与在跑要分开说，否则"配置已写入"会被读成"新值已经在服务了"。
// 把任一处改回旧形态，下面对应用例即红。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

const vue = read('views/AppDetail.vue');

test('envDrift 只读后端 envState，一致时返回 null 不摆横幅', () => {
  const fn = vue.match(/const envDrift = computed(?:<[^)]*>)?\(\(\) => \{[\s\S]*?\n\}\);/);
  assert.ok(fn, 'envDrift computed 找不到');
  const body = fn[0];
  assert.match(body, /app\.value\?\.envState/, '横幅不去读后端那份对账结果，就还是凭界面猜');
  assert.match(body, /if \(!s\) return null;/, '老后端读不到 envState 时必须什么都不说');
  assert.match(body, /s\.disk === 'match' && \(!n \|\| s\.unitReads\)\) return null;/,
    '两份一致（含没有变量、没人读这两种无事状态）不该报错');
});

test('漂移横幅按五态给不同判词，且区分服务在不在跑', () => {
  const body = vue.match(/const envDrift = computed(?:<[^)]*>)?\(\(\) => \{[\s\S]*?\n\}\);/)[0];
  for (const disk of ['no-dir', 'unreadable', 'missing', 'differs']) {
    assert.ok(body.includes(`'${disk}'`), `横幅少了 ${disk} 这一档的判词`);
  }
  assert.match(body, /type: 'warning'.*安装目录还不存在/s, '没部署过不该算失败');
  assert.match(body, /type: 'warning'/, 'no-dir 应是 warning');
  assert.match(body, /type: 'error'/, '漂移应是 error');
  assert.ok(body.includes('app.value?.running'), '没区分在跑/没在跑，用户会把"已写入"读成"已生效"');
});

test('横幅把三种漂移说成"界面显示的值和生效的不是同一份"', () => {
  const body = vue.match(/const envDrift = computed(?:<[^)]*>)?\(\(\) => \{[\s\S]*?\n\}\);/)[0];
  assert.match(body, /面板显示的值和进程实际读到的不是同一份/);
  assert.match(body, /这 \$\{n\} 个变量/, '判词必须点名是几个变量，好让人判断影响面');
});

test('修复按钮复用 save()，只在管理员下出现，并按在跑改标签', () => {
  const block = vue.match(/<NAlert v-if="envDrift"[\s\S]*?<\/NAlert>/);
  assert.ok(block, '环境变量页没有漂移横幅');
  const tag = block[0];
  assert.match(tag, /role="alert"/, '读屏要立刻读到这条漂移');
  assert.match(tag, /v-if="isAdmin"/, 'viewer 写不动配置，不该看到修复按钮');
  assert.match(tag, /@click="save"/, '修复必须复用 PATCH /api/apps/{id}：它才真的写 .panel.env 并重启');
  assert.match(tag, /:loading="saving"[^>]*:disabled="saving"/, '保存中要锁住，连点会并发写 unit');
  assert.match(tag, /app\.running \? '重新写入并重启' : '重新写入'/,
    '服务没在跑时不能说"并重启"，那等于承诺了一件不会做的事');
});

test('判词只靠 envState，不去读含值的 env', () => {
  const body = vue.match(/const envDrift = computed(?:<[^)]*>)?\(\(\) => \{[\s\S]*?\n\}\);/)[0];
  assert.doesNotMatch(body, /env\.value/, '横幅不该去读那份含值的 env，判词只靠 envState');
  assert.doesNotMatch(body, /x\.k|x\.v/, '判词里不许出现任何键名或值');
});
