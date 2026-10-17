// 表格列形态守卫：`npm run test:unit`（node --test）。
//
// 钉的是两类只能靠真实浏览器 + 真实宽度才画得出来的缺陷，都在「列定义」这一层：
//
// ① 批次㊴：scroll-x 换成 'auto' 之后，既没 width 也没 minWidth 的弹性列拿到的是
//    「总宽 − 固定列宽」，固定列一超过容器它就变成 0 —— 375px 实测 Apps 的应用列、
//    Files 的名称列、Backups 的任务列首格宽度都是 0，整列名字看不见。
// ② 批次53A/53B：列的 render 里用 NSpace `wrap: false`。naive-ui 会把每个子项包进
//    `max-width:100%` 的 `.n-space__item`，nowrap 时放不下的尾部条目被压成 1ch 再逐字
//    断行（Apps 域名列实测「nginx 检测」竖成一排爬进状态列）；表格走 auto 布局时换了
//    一种表现——最小内容宽等于所有标签之和，于是整列被撑宽、挤掉别的列（证书「状态」列
//    150px 被撑到 202px）。两种都是同一句话：一列的宽度不该由装饰性标签的排列方式决定。
//
// 判定口径：只认写成 `title: X, key: Y`（同一行）的列定义；render 里的 `title:` 是
// HTML 提示属性，不会和 key 同行。动态拼出来的列（Files 的扩展属性列）由调用方自带宽度，
// 不在本用例覆盖范围内。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const VIEWS = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views');
const COL = /(?:\{\s*)?\btitle\s*:\s*[^,\n]+,\s*key\s*:/;
const ARR = /const\s+(\w*[Cc]olumn\w*)\s*(?::[^=]*)?=\s*\[/g;

// 返回视图里所有「列数组」的行区间；带 scroll-x 的表才管。
function columnArrays(src) {
  const lines = src.split('\n');
  const out = [];
  for (const m of src.matchAll(ARR)) {
    const start = src.slice(0, m.index).split('\n').length - 1;
    let depth = 0;
    let end = start;
    for (let j = start; j < lines.length; j++) {
      depth += (lines[j].match(/\[/g) || []).length - (lines[j].match(/\]/g) || []).length;
      if (depth <= 0 && j >= start) { end = j; break; }
    }
    out.push({ name: m[1], start, end, lines });
  }
  return out;
}

function scanFile(path) {
  const src = readFileSync(path, 'utf8');
  if (!src.includes('scroll-x') && !src.includes('scrollX')) return { wide: [], nowrap: [] };
  const wide = [];
  const nowrap = [];
  for (const { name, start, end, lines } of columnArrays(src)) {
    const starts = [];
    for (let k = start; k <= end; k++) if (COL.test(lines[k])) starts.push(k);
    starts.forEach((k, n) => {
      const stop = n + 1 < starts.length ? starts[n + 1] : end + 1;
      const body = lines.slice(k, stop).join('\n');
      if (!/\b(width|minWidth)\s*:/.test(body)) {
        wide.push(`${basename(path)}:${k + 1} [${name}] ${lines[k].trim().slice(0, 60)}`);
      }
    });
    for (let k = start; k <= end; k++) {
      const t = lines[k].trim();
      // 注释里提到 wrap:false（说明为什么不用它）不算违规，只管真写进代码的。
      if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) continue;
      if (/wrap\s*:\s*false/.test(lines[k])) {
        nowrap.push(`${basename(path)}:${k + 1} [${name}] ${t.slice(0, 60)}`);
      }
    }
  }
  return { wide, nowrap };
}

function scanAll() {
  const hits = readdirSync(VIEWS)
    .filter((f) => f.endsWith('.vue'))
    .map((f) => scanFile(join(VIEWS, f)));
  return {
    wide: hits.flatMap((h) => h.wide),
    nowrap: hits.flatMap((h) => h.nowrap),
  };
}

test('scroll-x 表格里的每一列都必须声明 width 或 minWidth', () => {
  const { wide } = scanAll();
  assert.deepEqual(wide, [], `这些列没有宽度约束，窄屏会被挤成 0 宽：\n${wide.join('\n')}`);
});

test('列的 render 里不许用 NSpace wrap:false（尾部标签会被压成 1ch 或把整列撑宽）', () => {
  const { nowrap } = scanAll();
  assert.deepEqual(
    nowrap,
    [],
    `这些列把标签行钉死成不换行，窄列下要么尾部标签逐字断行、要么整列被撑宽：\n${nowrap.join('\n')}`,
  );
});
