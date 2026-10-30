// 批次79 契约守卫：设置页「面板入口」的读法与切换。
//
// 这一批把"面板自己对外用哪个域名"从只能改 nginx 文件变成页面上可读、可守卫式切换。
// 被钉住的形状：
// 1. 读不到（nginx -T 读不动 / 没配置目录）必须显示成「读不到」，不能画成"没有域名入口"；
// 2. 切换按钮只在真的能改时亮（有入口配置、文件读得到、证书归 certbot 管）；
// 3. 拒绝文案（证书没签、域名被别的文件占用、复核失败已回滚）连同 certbot 命令留在抽屉里，
//    只弹 toast 会让整段处置命令飘走；
// 4. 成功提示不能说成"已经能访问了"：回环复核绕过 DNS，公网解析是另一件事；
// 5. 连点不能并发两次"写配置 + reload"。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

test('api.ts：面板入口读写都挂上级口，切换给足时间', () => {
  const s = read('api.ts');
  assert.match(s, /panelEntry: \(\) => req\('\/system\/panel-entry'\)/);
  assert.match(s, /setPanelDomain: \(domain: string\) =>\s*req\('\/system\/panel-entry\/domain', \{ body: \{ domain \}, timeout: 120000 \}\)/);
});

test('设置页：面板入口按 admin 口径加载，读不到与「没有入口」分开显示', () => {
  const s = read('views/Settings.vue');
  assert.match(s, /if \(isAdmin\.value\) await grab\(\(\) => api\.panelEntry\(\)/);
  // 三态各有一条：请求失败 / 后端 unreadable / 确实没有反代到面板端口的配置
  assert.match(s, /<EmptyBox v-if="panelEntryError" text="面板入口这一轮没读到"/);
  assert.match(s, /v-else-if="panelEntry\?\.unreadable" type="warning"/);
  assert.match(s, /v-else-if="panelEntry && !panelEntry\.entries\.length"/);
  // "没有域名入口"不许长得像"读不到"
  assert.match(s, /return p\.domain \|\| '没有域名入口';/);
  assert.match(s, /if \(p\.unreadable\) return '入口没读到';/);
});

test('设置页：只有真能改时才亮切换按钮', () => {
  const s = read('views/Settings.vue');
  // 这一轮读失败时 panelEntry 里还留着旧读数：只看它就会拿旧数据给「能改」背书
  assert.match(s, /const canChangePe = computed\(\(\) => !!panelEntry\.value && !panelEntryError\.value/);
  assert.match(s, /!panelEntry\.value\.unreadable && panelEntry\.value\.changeable !== false/);
  assert.match(s, /panelEntry\.value\.changeable !== false\s*\n\s*&& \(panelEntry\.value\.entries \|\| \[\]\)\.length > 0/);
  assert.match(s, /:disabled="!canChangePe"/);
});

test('设置页：拒绝文案留在抽屉里，成功提示不说成公网已可达', () => {
  const s = read('views/Settings.vue');
  assert.match(s, /peMsg\.value = e\.message;/);
  assert.match(s, /<NAlert v-if="peMsg" type="error"/);
  // 换行与长串要能看全（命令要能整段复制）
  assert.match(s, /\.pe-alert \{ white-space: pre-wrap; word-break: break-word; \}/);
  assert.match(s, /公网解析请自行确认/);
  assert.ok(!/msg\.success\(`已切到 \$\{r\.domain\}`\)/.test(s), '成功提示不许暗示公网已经能访问');
});

test('设置页：连点不会并发写两次配置', () => {
  const s = read('views/Settings.vue');
  assert.match(s, /async function savePe\(\) \{\s*\/\/[\s\S]{0,200}?\n  if \(peSaving\.value\) return;/);
  assert.match(s, /:disabled="peSaving \|\| !peDraft\.trim\(\)"/);
});
