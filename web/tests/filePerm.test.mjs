// 文件页「权限与属主」的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次54 立下的四条口径：
// 1) 权限串与属主由服务端一次算好下发（perm / octal / owner / group 四个字段）。
//    页面自己拼 `rwxr-xr-x` 或 `mode.toString(8)` 就是第二个出处：服务层改了格式，
//    列表与弹窗各说各话，而"这个文件到底几权限"恰恰是排查应用起不来时唯一要信的信息。
// 2) 只提交改动过的那一项，一个都没改就不发请求。整份回传会把用户没动的属主
//    也重新 chown 一遍，还把"没有要修改的内容"这条服务端红线变成弹窗里的报错。
// 3) 写操作照旧锁 busyOp：确定连点、回车 + 点击各一次会写两遍。
// 4) 弹窗必须写明"只改这一个条目，不递归"。哪天有人加个「应用到子目录」却没有
//    服务端的递归上限，一次误点就能把整棵树的执行位清掉。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Files.vue');
const src = readFileSync(SRC, 'utf8');
const script = src.slice(0, src.indexOf('</script>'));
const tpl = src.slice(src.indexOf('<template>'));

test('权限列读服务端四个字段，页面不自己格式化', () => {
  assert.match(script, /title: '权限\/属主', key: 'perm', width: \d+,/);
  assert.match(script, /e\.perm \? `\$\{e\.perm\}（\$\{e\.octal\}）`/, 'title 里要给出八进制全文');
  assert.match(script, /e\.owner \? `\$\{e\.owner\}:\$\{e\.group\}`/);
  for (const bad of ['toString(8)', 'padStart(', 'filemode'])
    assert.ok(!script.includes(bad), `${bad}：格式化权限是第二个出处，服务层已经给了 perm/octal`);
});

test('弹窗预填当前值，三个字段都来自被点的那一行', () => {
  assert.match(script, /function openPerm\(e: any\) \{[\s\S]{0,300}permMode\.value = e\.octal/,
    '预填服务端的 octal，而不是留空让用户猜');
  assert.match(script, /permOwner\.value = e\.owner \|\| '';/);
  assert.match(script, /permGroup\.value = e\.group \|\| '';/);
});

test('只提交改动项，没改动就关弹窗不发请求', () => {
  assert.match(script, /if \(m && m !== \(t\.octal \|\| ''\)\) body\.mode = m;/);
  assert.match(script, /if \(o && o !== \(t\.owner \|\| ''\)\) body\.owner = o;/);
  assert.match(script, /if \(g && g !== \(t\.group \|\| ''\)\) body\.group = g;/);
  assert.match(script, /if \(Object\.keys\(body\)\.length === 1\) return \(showPerm\.value = false\);/,
    'body 里除了 path 一个字段都没有时必须就地关掉');
});

test('保存是受控写：忙态锁住、成功后作废缓存并回源', () => {
  assert.match(script, /if \(!t \|\| busyOp\.value\) return;/);
  assert.match(script, /busyOp\.value = 'perm';[\s\S]{0,120}postJson<any>\('\/files\/perm', body\)/);
  assert.match(script, /showPerm\.value = false;\n {4}reload\(\);/);
  assert.match(tpl, /:loading="busyOp === 'perm'" :disabled="!!busyOp"/);
});

test('说破「只改这一个条目，不递归」', () => {
  assert.match(tpl, /只改这一个条目，不递归到子文件/);
  assert.ok(!/recursive/i.test(src), '递归 chmod 没有实现，接口与页面都不许出现这个参数');
  assert.ok(!tpl.includes('应用到子目录'), '没有服务端递归校验之前，画面上不给出现这个入口');
});
