// 「SSH 爆破来源一键封禁」前端的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次㊻B 立下的四条口径，全都是"改坏了照样能构建"的那类：
// 1) 写操作的开关只认服务端那句 `ok`。列表读不到时页面若还让点，就是在一谁也说不清
//    现在挡着谁的表上再叠一条规则——后端 ban_service 为此专门把写锁死了，前端必须同步。
// 2) 每次写完都要回读 GET /api/bans，不许把本地状态当成"已经封上了"。
//    ufw 会打印 Aborted 却返回退出码 0，乐观状态在这里必然说谎。
// 3) 一次只允许一个写在飞。ufw 的规则编号会随任何一次删除整体前移，并发两条解封会让
//    第二次的签名比对落在一条已经挪了位置的规则上。
// 4) 前端不许自己判"这个地址能不能封"（内网、CIDR、域名一律由服务端的 _public_addr 说）。
//    页面抄一份判定就是第二个出处，两边迟早不一致。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Services.vue');
const API = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'api.ts');
const src = readFileSync(SRC, 'utf8');
const apiSrc = readFileSync(API, 'utf8');

test('封禁列表的读不到锁住全部写操作，且 reason 要落到画面上', () => {
  assert.ok(/const banCanWrite = computed\(\(\) => isAdmin && banRead\.value/.test(src),
    '写按钮的可用性必须同时认 admin 与服务端的 ok');
  assert.ok(/:disabled="!banCanWrite"/.test(src), '来源排行里的封禁按钮要跟着锁');
  assert.ok(/disabled: !banCanWrite\.value/.test(src), '已封禁表里的解除按钮要跟着锁');
  assert.ok(/d\.reason \|\| '未知原因'/.test(src), '读不到的原因只能用服务端那一句，不能页面自己编');
  assert.ok(/这不等于「没有封禁」/.test(src), '第三态要说破，空列表和没读到是两回事');
});

test('写完必须回读，且一次只允许一条写在飞', () => {
  const writes = src.match(/async function write(Ban|Unban)[\s\S]*?\n}/g) || [];
  assert.equal(writes.length, 2, '封禁与解除各一段写入函数');
  writes.forEach((body) => {
    assert.ok(/finally \{[\s\S]*refreshBans\(\)/.test(body),
      `写入后必须重新拉列表：${body.slice(0, 40)}`);
    assert.ok(/banBusy\.value = (t|r)\.ip;/.test(body), '写入前要把这一行标成在飞');
  });
  assert.ok(/banBusy\.value === ''/.test(src), '上一条没写完就不许发下一条');
  assert.ok(!/banList\.value\.push|bans\.value =/.test(src), '不许把本地拼的列表当成已封禁');
});

test('封禁的入参只有地址与次数，判定权在服务端', () => {
  assert.ok(/api\.ban\(t\.ip, Number\(t\.count\) \|\| 0\)/.test(src),
    'hits 只是备注那一栏，来自来源排行的次数');
  // 页面自己判内网/CIDR/域名的写法一个都不许出现——那是 _public_addr 的活
  assert.ok(!/startsWith\(['"]10\.|192\.168|isPrivate|\.isPrivate/.test(src),
    '前端不许抄一份地址可用性判定');
  assert.ok(!/\/3[0-9]\b|CIDR/.test(src), 'CIDR 的拒绝由服务端说，页面不预先翻译');
});

test('请求超时口径与服务端子进程一致，api.ts 三个动作为一对', () => {
  assert.ok(/bans: \(\) => req\('\/bans'\)/.test(apiSrc), 'GET 走默认超时即可');
  assert.ok((apiSrc.match(/timeout: 70000/g) || []).length === 2,
    '封禁与解除都要 70s：服务端 ufw 命令超时是 60s，前端比它快就会丢结果');
  assert.ok(/method: 'DELETE'/.test(apiSrc) && /expect=\$\{encodeURIComponent\(b\.sig \|\| ''\)\}/.test(apiSrc),
    '解封要带上列表读回的 num 与 sig，签名比对是防火墙层的事');
});

test('封禁与解除的按钮真的绑上了那一次写', () => {
  // 375px 实测踩过：按钮渲染了、灰不灰也对，但 @click 忘了写，点下去什么也不发生。
  assert.ok(/@click="askBan\(t\)"/.test(src), '来源排行里的封禁按钮必须绑 askBan');
  assert.ok(/run: \(\) => askUnban\(r\)/.test(src), '解除要经 CpRowActions 的 run 绑上 askUnban');
  assert.ok(/api: \(\) => writeBan\(t\)/.test(src) && /api: \(\) => writeUnban\(r\)/.test(src),
    '确认框点下去要真的执行那一次写，不是只弹个窗');
});

test('只读账号看不见写入口，admin 才拉这份表', () => {
  assert.ok(/watch\(tab, \(t\) => \{ if \(isAdmin && t === 'login'\)/.test(src),
    '进登录安全页签才读，且只给 admin：/api/bans 对只读账号是 403');
  assert.ok(/<template v-if="isAdmin">[\s\S]*?面板封禁的来源/.test(src),
    '已封禁那一块整块只给 admin，别摆一个必然失败的表格出来');
});
