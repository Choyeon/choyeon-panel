// 表格列宽守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次㊴ 留下的一类缺陷：scroll-x 换成 'auto' 之后，既没 width 也没 minWidth 的
// 弹性列拿到的是「总宽 − 固定列宽」，固定列一超过容器它就变成 0 —— 375px 实测 Apps 的
// 应用列、Files 的名称列、Backups 的任务列首格宽度都是 0，整列名字看不见。
// 这类问题只有真实浏览器 + 真实宽度才画得出来，typecheck/build/单测全都绿，所以在这里
// 用源码级检查兜住：新增列忘了写宽度，这条用例就会点名。
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

function scanFile(path) {
  const src = readFileSync(path, 'utf8');
  if (!src.includes('scroll-x') && !src.includes('scrollX')) return [];
  const lines = src.split('\n');
  const bad = [];
  for (const m of src.matchAll(ARR)) {
    const arrName = m[1];
    const start = src.slice(0, m.index).split('\n').length - 1;
    let depth = 0;
    let end = start;
    for (let j = start; j < lines.length; j++) {
      depth += (lines[j].match(/\[/g) || []).length - (lines[j].match(/\]/g) || []).length;
      if (depth <= 0 && j >= start) { end = j; break; }
    }
    const starts = [];
    for (let k = start; k <= end; k++) if (COL.test(lines[k])) starts.push(k);
    starts.forEach((k, n) => {
      const stop = n + 1 < starts.length ? starts[n + 1] : end + 1;
      const body = lines.slice(k, stop).join('\n');
      if (!/\b(width|minWidth)\s*:/.test(body)) {
        bad.push(`${basename(path)}:${k + 1} [${arrName}] ${lines[k].trim().slice(0, 60)}`);
      }
    });
  }
  return bad;
}

test('scroll-x 表格里的每一列都必须声明 width 或 minWidth', () => {
  const bad = readdirSync(VIEWS)
    .filter((f) => f.endsWith('.vue'))
    .flatMap((f) => scanFile(join(VIEWS, f)));
  assert.deepEqual(bad, [], `这些列没有宽度约束，窄屏会被挤成 0 宽：\n${bad.join('\n')}`);
});
