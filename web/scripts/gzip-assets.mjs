// 构建期预压缩：给 dist/assets 下的文本产物同目录产出 <file>.gz。
// 为什么不在运行时压缩：面板唯一的日志接口是 text/event-stream，任何 gzip 中间件
// 都会把流式响应攒成一块（日志中心表现为"卡住不动"）；预压缩只在构建时花 CPU，
// 请求路径上只是换个文件读，SSE 完全不受影响。
import { gzipSync } from 'node:zlib';
import { readdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const dir = fileURLToPath(new URL('../dist/assets/', import.meta.url));
const TEXT_EXT = /\.(?:js|css|svg|json)$/;

let files;
try {
  files = readdirSync(dir).filter((f) => TEXT_EXT.test(f) && !f.endsWith('.gz'));
} catch {
  console.log('[gzip-assets] 没有 dist/assets，跳过');
  process.exit(0);
}

let raw = 0;
let packed = 0;
let n = 0;
for (const f of files) {
  const src = join(dir, f);
  const buf = readFileSync(src);
  // 1KB 以下的文件收益抵不过多一轮协商；压不动的（已经压缩过的）不产出副本。
  if (buf.length < 1024) continue;
  const gz = gzipSync(buf, { level: 9 });
  if (gz.length >= buf.length * 0.9) continue;
  writeFileSync(`${src}.gz.tmp`, gz);
  renameSync(`${src}.gz.tmp`, `${src}.gz`); // 原子替换，避免并发请求读到半截文件
  raw += buf.length;
  packed += gz.length;
  n += 1;
}

console.log(
  `[gzip-assets] ${n} 个文件 ${(raw / 1024).toFixed(0)}KB → ${(packed / 1024).toFixed(0)}KB` +
    `（-${raw ? (100 - (packed / raw) * 100).toFixed(1) : '0.0'}%）`,
);
