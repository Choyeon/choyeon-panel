// 样式形态守卫：`npm run test:unit`（node --test）。
//
// 钉的是三类"构建永远绿、浏览器里才错"的样式缺陷，都在批次64 实测过一次：
//
// ① 同特异度下媒体查询不加权，谁写在后面谁赢。Shell.vue 的 `.cp-title` 基准规则
//    （12px）原先排在 `@media (max-width:640px)` 那块**后面**，于是"窄屏标题大一号"
//    这档从来没生效过——375px 实测标题仍是 12px，只有 font-weight 落了下来。
//    线上反证：把基准规则重新追加到样式表末尾，13px 立刻掉回 12px；删掉又回 13px。
// ② 手机浏览器的 `100vh` 是"地址栏收起后"的大视口，面板是固定壳 + 内部 .cp-scroll
//    滚动，用 100vh 会把页脚和表格底部压在地址栏/home 指示条下面够不着。全站只留
//    style.css 里 `--viewport-h` 那一个兜底声明可以写 100vh，其余一律走这个变量。
// ③ `transition: all` 会把宽高内边距一起补间：断点切换或内容变化那一帧整块跟着抖，
//    而 hover 真正会改的往往只有颜色。
//
// 判定口径：只扫 `<style>` 块（.vue）与 .css 文件，注释先按等行数清空，
// 所以注释里举例写的 `100vh` 不算违规；①只比较**完全相同的 selector 文本**，
// `.x:hover` 与 `.x` 是两个 selector，那种"基准 + 状态"写法不会被误判。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (name.endsWith('.vue') || name.endsWith('.css')) out.push(p);
  }
  return out;
}

/** 注释换成同样行数的空白：行号不变，注释里的 `{` / selector / 100vh 也不会被当成代码 */
function blankComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '));
}

/** 一个文件里所有样式块，带上块首行号（.vue 的 <style> 不在第 1 行） */
function styleBlocks(file) {
  const src = readFileSync(file, 'utf8');
  if (file.endsWith('.css')) return [{ text: src, base: 0 }];
  const out = [];
  for (const m of src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
    out.push({ text: m[1], base: src.slice(0, m.index).split('\n').length });
  }
  return out;
}

/** 逐行扫一个样式块：返回 {inside: 媒体查询里的规则, outside: 媒体查询外的规则, lines} */
function scan(block) {
  const lines = blankComments(block.text).split('\n');
  const inside = new Map();
  const outside = new Map();
  let inMedia = false;
  let depth = 0;
  lines.forEach((raw, i) => {
    const line = raw;
    if (/@media|@supports/.test(line)) inMedia = true;
    const bi = line.indexOf('{');
    if (bi > 0) {
      const sel = line.slice(0, bi).trim();
      if (sel && !sel.startsWith('@') && !sel.startsWith('from') && !sel.startsWith('to') && !/^\d+%$/.test(sel)) {
        const at = block.base + i + 1;
        (inMedia ? inside : outside).set(sel, at);
      }
    }
    depth += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
    if (depth <= 0) { depth = 0; inMedia = false; }
  });
  return { inside, outside, lines };
}

const FILES = walk(SRC);

test('媒体查询里的覆盖不许被后面的基准规则吃掉', () => {
  const bad = [];
  for (const f of FILES) {
    for (const block of styleBlocks(f)) {
      const { inside, outside } = scan(block);
      for (const [sel, at] of inside) {
        const base = outside.get(sel);
        // 同特异度、同文件、基准写在覆盖后面 → 覆盖永远不生效
        if (base && base > at) bad.push(`${relative(SRC, f)}:${base} 的 ${sel} 盖掉了 :${at} 的媒体查询档`);
      }
    }
  }
  assert.deepEqual(bad, [], `这些基准规则排在媒体查询覆盖之后，覆盖是死的：\n${bad.join('\n')}`);
});

test('100vh 只允许 --viewport-h 那一处兜底声明', () => {
  const bad = [];
  for (const f of FILES) {
    const rel = relative(SRC, f);
    for (const block of styleBlocks(f)) {
      scan(block).lines.forEach((line, i) => {
        if (!/100vh/.test(line)) return;
        // 唯一的合法写法：--viewport-h 的兜底档（紧随其后的 @supports 会换成 100dvh）
        if (rel === 'style.css' && /--viewport-h:\s*100vh/.test(line)) return;
        bad.push(`${rel}:${block.base + i + 1} ${line.trim()}`);
      });
    }
  }
  assert.deepEqual(bad, [], `这些声明绕过了 --viewport-h（手机浏览器上 100vh 会超出可见高度）：\n${bad.join('\n')}`);
});

test('transition 不许写 all', () => {
  const bad = [];
  for (const f of FILES) {
    for (const block of styleBlocks(f)) {
      scan(block).lines.forEach((line, i) => {
        if (!/transition\s*:/.test(line)) return;
        if (!/transition\s*:[^;]*\ball\b/.test(line)) return;
        bad.push(`${relative(SRC, f)}:${block.base + i + 1} ${line.trim()}`);
      });
    }
  }
  assert.deepEqual(bad, [], `transition: all 会把布局属性一起补间，写明要动的属性：\n${bad.join('\n')}`);
});
