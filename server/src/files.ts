import {
  realpathSync, readdirSync, statSync, readFileSync, writeFileSync, unlinkSync, rmSync, mkdirSync, renameSync, createReadStream, existsSync,
} from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { FastifyReply } from 'fastify';

const ROOTS = (process.env.CP_FILE_ROOTS || '/root/www,/etc/nginx,/root/backups/panel').split(',');

export function safePath(p: string): string {
  const abs = resolve(p);
  let probe = abs;
  while (!existsSync(probe)) probe = dirname(probe);
  const real = realpathSync(probe);
  const rest = abs.slice(probe.length);
  const full = resolve(real + rest);
  if (!ROOTS.some((r) => full === r || full.startsWith(r + '/'))) throw new Error('路径超出允许范围');
  if (full.split('/').includes('..')) throw new Error('非法路径');
  return full;
}

export function listDir(p: string) {
  const abs = safePath(p || '/root/www');
  const st = statSync(abs);
  if (!st.isDirectory()) throw new Error('不是目录');
  const entries = readdirSync(abs, { withFileTypes: true }).map((e) => {
    let size = 0;
    let mtime = '';
    let isDir = e.isDirectory();
    try {
      const s = statSync(join(abs, e.name));
      size = s.size;
      mtime = s.mtime.toISOString();
    } catch {}
    return { name: e.name, isDir, size, mtime };
  });
  entries.sort((a, b) => (Number(b.isDir) - Number(a.isDir)) || a.name.localeCompare(b.name));
  return { path: abs, entries };
}

export function readText(p: string) {
  const abs = safePath(p);
  const st = statSync(abs);
  if (st.size > 1024 * 1024) throw new Error('文件超过 1MB，请用下载');
  return { path: abs, content: readFileSync(abs, 'utf8') };
}

export function writeText(p: string, content: string) {
  const abs = safePath(p);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, content);
  return { path: abs, size: Buffer.byteLength(content) };
}

export function streamFile(p: string, reply: FastifyReply) {
  const abs = safePath(p);
  const st = statSync(abs);
  if (!st.isFile()) throw new Error('不是文件');
  reply.header('Content-Type', 'application/octet-stream');
  reply.header('Content-Disposition', `attachment; filename="${encodeURIComponent(abs.split('/').pop() || 'file')}"`);
  return reply.send(createReadStream(abs));
}

export function fsAction(action: string, p: string, p2?: string) {
  const abs = safePath(p);
  if (action === 'mkdir') mkdirSync(abs, { recursive: true });
  else if (action === 'delete') {
    if (ROOTS.includes(abs)) throw new Error('禁止删除根目录');
    rmSync(abs, { recursive: true, force: true });
  } else if (action === 'rename') {
    const to = safePath(p2 || '');
    if (existsSync(to)) throw new Error('目标已存在');
    renameSync(abs, to);
  } else throw new Error('未知操作');
  return { ok: true };
}
