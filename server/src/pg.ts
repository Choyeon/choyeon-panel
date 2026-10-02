import { spawn } from 'node:child_process';
import { run } from './util.js';
import { getSetting, setSetting } from './db.js';
import { readFileSync, existsSync } from 'node:fs';

const IDENT_RE = /^[a-z_][a-z0-9_]{0,62}$/;
export function isIdent(s: string) {
  return IDENT_RE.test(s);
}
function quotePass(p: string) {
  if (!/^[\x20-\x7e]{1,128}$/.test(p)) throw new Error('密码只允许可见 ASCII 字符且不超过 128 位');
  return `'${p.replace(/'/g, `''`)}'`;
}

export function psql(sql: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn('su', ['-s', '/bin/sh', 'postgres', '-c', 'psql -X -A -t -F "|"']);
    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error('psql 超时'));
    }, 30000);
    child.stdout.on('data', (d: Buffer) => {
      out += d.toString();
      if (out.length > 512 * 1024) {
        child.kill();
        clearTimeout(timer);
        reject(new Error('输出过大（>512KB），请加 LIMIT'));
      }
    });
    child.stderr.on('data', (d: Buffer) => (err += d.toString()));
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve(out.trim());
      else reject(new Error(err.trim() || `psql exit ${code}`));
    });
    child.stdin.write(sql);
    child.stdin.end();
  });
}

export async function listDbs() {
  const out = await psql(
    `SELECT d.datname, pg_get_userbyid(d.datdba) owner, pg_size_pretty(pg_database_size(d.datname)) size,
        (SELECT count(*) FROM pg_stat_activity a WHERE a.datname=d.datname) conns
     FROM pg_database d WHERE d.datistemplate=false AND d.datallowconn=true ORDER BY datname;`,
  );
  return out.split('\n').map((l) => {
    const f = l.split('|');
    return { name: f[0], owner: f[1], size: f[2], conns: f[3] };
  });
}

export async function listRoles() {
  const out = await psql(
    `SELECT r.rolname, r.rolsuper, r.rolcanlogin, r.rolcreaterole, r.rolcreatedb
     FROM pg_roles r WHERE r.rolname NOT LIKE 'pg\\_%' ORDER BY rolname;`,
  );
  return out.split('\n').map((l) => {
    const f = l.split('|');
    return { name: f[0], super: f[1] === 't', login: f[2] === 't', createrole: f[3] === 't', createdb: f[4] === 't' };
  });
}

export async function createDb(name: string, owner?: string) {
  if (!isIdent(name)) throw new Error('数据库名只允许小写字母/数字/下划线');
  if (owner && !isIdent(owner)) throw new Error('owner 名不合法');
  await psql(`CREATE DATABASE ${name}${owner ? ` OWNER ${owner}` : ''};`);
}

export async function dropDb(name: string) {
  if (!isIdent(name)) throw new Error('数据库名不合法');
  if (name === 'postgres' || name === 'template0') throw new Error('禁止删除该库');
  await psql(`DROP DATABASE ${name};`);
}

export async function createRole(name: string, password: string) {
  if (!isIdent(name)) throw new Error('用户名不合法');
  await psql(`CREATE ROLE ${name} LOGIN PASSWORD ${quotePass(password)};`);
}

export async function setRolePassword(name: string, password: string) {
  if (!isIdent(name)) throw new Error('用户名不合法');
  await psql(`ALTER ROLE ${name} LOGIN PASSWORD ${quotePass(password)};`);
}

export async function dropRole(name: string) {
  if (!isIdent(name)) throw new Error('用户名不合法');
  if (name === 'postgres') throw new Error('禁止删除 postgres');
  await psql(`DROP OWNED BY ${name}; DROP ROLE ${name};`);
}

export async function grant(name: string, db: string) {
  if (!isIdent(name) || !isIdent(db)) throw new Error('参数不合法');
  await psql(`GRANT ALL PRIVILEGES ON DATABASE ${db} TO ${name};`);
}

export async function query(sql: string) {
  if (!sql.trim()) throw new Error('空查询');
  return psql(sql);
}

export function redisPassword(): string | null {
  const p = getSetting('redis_password');
  if (p) return p;
  if (existsSync('/etc/redis/redis.conf')) {
    const m = readFileSync('/etc/redis/redis.conf', 'utf8').match(/^requirepass\s+(\S+)/m);
    if (m) return m[1];
  }
  return null;
}

export async function redisInfo() {
  const pass = redisPassword();
  // REDISCLI_AUTH avoids exposing the password in the process list (argv of -a)
  const env = pass ? { ...process.env, REDISCLI_AUTH: pass } : process.env;
  const r = await run('redis-cli', ['INFO'], { env });
  if (r.code !== 0 || r.out.includes('NOAUTH')) throw new Error(r.out.includes('NOAUTH') ? 'Redis 需要密码，请在下方设置' : r.out);
  const info: Record<string, string> = {};
  for (const line of r.out.split('\n')) {
    const i = line.indexOf(':');
    if (i > 0 && !line.startsWith('#')) info[line.slice(0, i)] = line.slice(i + 1).trim();
  }
  const keys = await run('redis-cli', ['DBSIZE'], { env });
  return { info, dbsize: keys.out, authed: !!pass };
}

export function setRedisPassword(p: string) {
  if (p) setSetting('redis_password', p);
  return { ok: true };
}
