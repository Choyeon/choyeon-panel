// 文件页「复制 / 剪切 / 粘贴」剪贴板的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次㊽B 立下的四条口径，都是"改坏了照样能构建、上线点一下才露馅"的那类：
// 1) 剪贴板存的是勾选那一刻的**绝对路径**。存名字的话，翻进另一个目录再粘贴，
//    搬的就是新目录里的同名条目——按钮看起来一模一样，文件却搬错了地方。
// 2) 冲突、子目录、跨分区、落盘余量一律由后端判（files_service 是唯一出处）。
//    页面抄一份判断，就是第二个出处：改了阈值两边分叉，而且页面看不到真实 stat。
// 3) 粘贴逐项串行，失败项点名；一次粘贴清空剪贴板。留着会让成功过的项再搬一遍。
// 4) 目的地在动手前定死。中途点面包屑换目录，剩下几项跟着落进新目录，
//    一次操作两个去处比整批留在原处难解释得多。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Files.vue');
const src = readFileSync(SRC, 'utf8');
const script = src.slice(0, src.indexOf('</script>'));
const tpl = src.slice(src.indexOf('<template>'));

test('剪贴板存绝对路径，且只在页面内存活', () => {
  assert.match(script, /const clip = ref<\{ mode: 'copy' \| 'move'; items: string\[\] \} \| null>\(null\)/);
  assert.match(script, /clip\.value = \{ mode, items: names\.map\(join\) \}/,
    '必须用 join() 存成绝对路径；存名字的话换目录后粘贴会搬错文件');
  for (const bad of ['localStorage', 'sessionStorage'])
    assert.ok(!script.includes(bad), `${bad}：剪贴板一旦持久化，换个账号/换个目录都会拿到上一个会话的路径`);
});

test('复制 / 剪切 / 粘贴三颗按钮的启用条件绑死在选中项与忙态上', () => {
  assert.match(tpl, /:disabled="!checkedKeys\.length \|\| !!busyOp"\s*\n\s*:icon="icon\('CopyOutline', 14\)" @click="setClip\('copy'\)"/);
  assert.match(tpl, /:disabled="!checkedKeys\.length \|\| !!busyOp"\s*\n\s*:icon="icon\('CutOutline', 14\)" @click="setClip\('move'\)"/);
  assert.match(tpl, /v-if="clip"[\s\S]{0,200}@click="pasteClip"/, '粘贴只在剪贴板非空时出现，免得点一颗注定报错的按钮');
  assert.match(tpl, /:loading="busyOp === 'paste'" :disabled="!!busyOp"/,
    '粘贴中途必须锁住：复制/剪切这时改的是剪贴板内容，而按钮已经按下去了');
  assert.match(script, /function setClip\(mode: 'copy' \| 'move'\) \{\n {2}if \(busyOp\.value\) return;/);
  assert.match(script, /if \(!c \|\| busyOp\.value\) return;/, '没勾选（clip 为空）或上一笔没落定就不许再按');
});

test('目的地在动手前定死，逐项串行，失败项点名', () => {
  assert.match(script, /const dest = path\.value;\n {2}busyOp\.value = 'paste';/);
  assert.match(script, /for \(const src of c\.items\) \{[\s\S]{0,400}await postJson\('\/files\/action', \{ action: c\.mode, path: src, path2: dest \}\);/);
  assert.ok(!/Promise\.all\(\[?\s*\.\.\.?c\.items/.test(script), '并行粘贴会让失败项分不清落没落下');
  assert.match(script, /failed\.push\(`\$\{src\.split\('\/'\)\.pop\(\) \|\| src\}：\$\{e\.message\}`\)/,
    '报错要点名是哪一项，后端那一句原样转述');
  assert.match(script, /项没成：\$\{failed\.join\('；'\)\}/);
});

test('一次粘贴清空剪贴板并回源列表', () => {
  assert.match(script, /\} finally \{\n[\s\S]{0,200}busyOp\.value = '';\n {4}clip\.value = null;\n {2}\}/);
  const tail = script.slice(script.indexOf('async function pasteClip'));
  assert.match(tail.slice(0, tail.indexOf('const columns')), /\n {2}reload\(\);\n\}/,
    '粘贴后必须回源：目录条目变了，缓存里的旧列表会让人以为没搬成');
  assert.ok(!/if \(failed\.length\)[\s\S]{0,160}clip\.value = null/.test(script),
    '清空不能只看失败与否——部分失败时留着剪贴板会重搬成功过的项（清空必须在 finally 里，不在判词分支里）');
});

test('页面不判断冲突/大小/子目录，也不碰第二套搬运实现', () => {
  const seg = script.slice(script.indexOf('function setClip'), script.indexOf('const columns'));
  for (const bad of ['proxy_pass', 'os.rename', 'fs.promises', 'Math.', 'Date.parse',
    'assert_write_budget', 'maxUploadBytes', 'st_dev', 'EXDEV', 'split(\'/\').length']) {
    assert.ok(!seg.includes(bad), `${bad}：搬运判据在 files_service，页面复制一份就是第二个出处`);
  }
  // 走的是页面自己的 postJson（120s），不是 api.fileAction 的 15s：跨分区搬整目录会超时
  assert.ok(!seg.includes('api.fileAction'), '粘贴必须用长超时那条路');
  assert.match(script, /async function postJson<T = any>\(path: string, body: unknown, timeout = 120000\)/);
});

test('后端动词只有 copy / move 两种，前端不许再造第三个', () => {
  assert.match(script, /const verb = c\.mode === 'move' \? '移动' : '复制';/);
  assert.match(script, /const clipVerb = computed\(\(\) => \(clip\.value\?\.mode === 'move' \? '移动' : '复制'\)\)/);
  assert.ok(!/'rename'/.test(script.slice(script.indexOf('function setClip'), script.indexOf('const columns'))),
    '跨目录不许退回 rename：改名只在同目录内，搬运只有 copy/move 一条路');
});

test('勾选列是 naive 的 selection 列，绑的是 checked-row-keys', () => {
  assert.match(src, /\{ type: 'selection' \},/,
    'NDataTable 的勾选是一列（type: selection）；原来绑的那个属性在 naive 上根本不存在，会被整块忽略，'
    + '于是「打包选中 / 复制选中 / 剪切选中」只剩按钮，页面上没有任何框可勾（㊽B 线上实测 .n-checkbox 为 0）');
  assert.ok(!/row-selection\s*=/.test(src),
    '不许把勾选态绑回那个不存在的属性：它不报错，只是静默不出勾选框');
  assert.match(tpl, /:checked-row-keys="checkedKeys" @update:checked-row-keys="onChecked"/);
  assert.match(script, /function onChecked\(keys: Array<string \| number>\) \{\n {2}checkedKeys\.value = keys\.map\(String\);/,
    'naive 回传 RowKey[]（string|number），勾选态要收成 string，因为本表 row-key 就是条目名');
});
