import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);

export async function run(cmd: string, args: string[], opts: { cwd?: string; timeout?: number; env?: NodeJS.ProcessEnv } = {}) {
  try {
    const { stdout, stderr } = await exec(cmd, args, { cwd: opts.cwd, timeout: opts.timeout ?? 120000, maxBuffer: 20 * 1024 * 1024, env: opts.env });
    return { code: 0, out: `${stdout}\n${stderr}`.trim() };
  } catch (e: any) {
    return { code: e.code ?? 1, out: `${e.stdout ?? ''}\n${e.stderr ?? ''}\n${e.message ?? ''}`.trim() };
  }
}

export function spawnLines(cmd: string, args: string[], onLine: (l: string) => void, onClose: (code: number) => void) {
  const child = spawn(cmd, args);
  let buf = '';
  child.stdout.on('data', (d: Buffer) => {
    buf += d.toString();
    const lines = buf.split('\n');
    buf = lines.pop() ?? '';
    for (const l of lines) onLine(l);
  });
  child.stderr.on('data', (d: Buffer) => onLine(d.toString().trimEnd()));
  child.on('close', (code) => onClose(code ?? 1));
  return child;
}

const NAME_RE = /^[a-z][a-z0-9-]{1,38}$/;
const DOMAIN_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/;
const UNIT_RE = /^[A-Za-z0-9@:._-]{1,64}(\.service)?$/;

export function isName(s: string) { return NAME_RE.test(s); }
export function isDomain(s: string) { return DOMAIN_RE.test(s); }
export function isUnit(s: string) { return UNIT_RE.test(s); }
export function isPort(p: number) { return Number.isInteger(p) && p >= 1024 && p <= 65535; }
export function isBranch(s: string) { return /^[A-Za-z0-9._/-]{1,100}$/.test(s); }
export function isGitUrl(s: string) {
  return /^(https?:\/\/\S+|git@[\w.-]+:\S+)$/.test(s) || s.startsWith('/');
}
export function shEscape(s: string) { return `'${s.replace(/'/g, `'\\''`)}'`; }
