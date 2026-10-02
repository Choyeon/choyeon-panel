import Fastify from 'fastify';
import jwt from '@fastify/jwt';
import fastifyStatic from '@fastify/static';
import fastifyWebsocket from '@fastify/websocket';
import * as pty from 'node-pty';
import { randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { db, jwtSecret, audit, getSetting, setSetting } from './db.js';
import * as system from './system.js';
import { listServices, serviceAction, isActive } from './systemd.js';
import * as apps from './apps.js';
import { renewCerts, findAppConfigs, saveAppConfig, quickEditConfig } from './nginx.js';
import { spawnLines, isUnit, isName, run } from './util.js';
import * as pg from './pg.js';
import * as backups from './backups.js';
import * as files from './files.js';
import { startChecker, runChecks } from './alerts.js';

const app = Fastify({ logger: process.env.CP_LOG ? { level: 'info' } : false, bodyLimit: 50 * 1024 * 1024 });
app.addContentTypeParser('application/octet-stream', { parseAs: 'buffer' }, (_req, body, done) => done(null, body));
await app.register(jwt, { secret: jwtSecret() });
await app.register(fastifyWebsocket);

const PORT = Number(process.env.CP_PORT || 3210);
const HOST = process.env.CP_HOST || '127.0.0.1';

function hashPass(pass: string) {
  const salt = randomBytes(16).toString('hex');
  return `${salt}:${scryptSync(pass, salt, 64).toString('hex')}`;
}
function checkPass(pass: string, stored: string) {
  const [salt, key] = stored.split(':');
  const got = scryptSync(pass, salt, 64);
  const want = Buffer.from(key, 'hex');
  return got.length === want.length && timingSafeEqual(got, want);
}

const loginAttempts = new Map<string, { n: number; t: number }>();
function rateLimit(ip: string) {
  const rec = loginAttempts.get(ip);
  const now = Date.now();
  if (!rec || now - rec.t > 60_000) {
    loginAttempts.set(ip, { n: 1, t: now });
    return false;
  }
  rec.n += 1;
  return rec.n > 10;
}

// ---------- public auth routes ----------
app.get('/api/auth/status', async () => {
  const u = db.prepare('SELECT username FROM users LIMIT 1').get() as { username: string } | undefined;
  return { needsSetup: !u, username: u?.username ?? null };
});

app.post('/api/auth/setup', async (req, reply) => {
  const body = req.body as { username?: string; password?: string };
  const count = (db.prepare('SELECT COUNT(*) c FROM users').get() as { c: number }).c;
  if (count > 0) return reply.code(403).send({ error: '管理员已存在' });
  if (!body?.username || !/^[A-Za-z0-9_-]{3,32}$/.test(body.username)) return reply.code(400).send({ error: '用户名不合法' });
  if (!body.password || body.password.length < 8) return reply.code(400).send({ error: '密码至少 8 位' });
  db.prepare('INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)').run(body.username, hashPass(body.password), 'admin');
  audit(body.username, 'setup', '创建管理员');
  return { token: app.jwt.sign({ sub: body.username, role: 'admin' }, { expiresIn: '12h' }), role: 'admin' };
});

app.post('/api/auth/login', async (req, reply) => {
  if (rateLimit(req.ip)) return reply.code(429).send({ error: '尝试过多，请稍后再试' });
  const body = req.body as { username?: string; password?: string };
  const user = db.prepare('SELECT * FROM users WHERE username=?').get(body?.username ?? '') as
    | { username: string; pass_hash: string; role: string }
    | undefined;
  if (!user || !body.password || !checkPass(body.password, user.pass_hash)) {
    audit(body?.username, 'login_failed');
    return reply.code(401).send({ error: '用户名或密码错误' });
  }
  audit(user.username, 'login');
  const role = user.role || 'admin';
  return { token: app.jwt.sign({ sub: user.username, role }, { expiresIn: '12h' }), role };
});

// ---------- authenticated routes ----------
app.register(
  async (api) => {
    api.addHook('preHandler', async (req, reply) => {
      let token = (req.headers.authorization ?? '').replace(/^Bearer /, '');
      if (!token) token = (req.query as any).token ?? '';
      if (!token) return reply.code(401).send({ error: '未登录' });
      let decoded: any;
      try {
        decoded = api.jwt.verify(token);
      } catch {
        return reply.code(401).send({ error: '登录已过期' });
      }
      const role = decoded.role || 'admin';
      (req as any).cpRole = role;
      const mutation = ['POST', 'PATCH', 'PUT', 'DELETE'].includes(req.method);
      const selfService = req.url.startsWith('/api/auth/password');
      if (mutation && role !== 'admin' && !selfService) {
        return reply.code(403).send({ error: '只读账号不能执行写操作' });
      }
    });
    const whoami = (req: any) => {
      const token = (req.headers.authorization ?? '').replace(/^Bearer /, '') || (req.query as any).token;
      return (api.jwt.verify(token) as any).sub as string;
    };

    // system
    api.get('/api/system/info', async () => system.info());
    api.get('/api/system/stats', async () => await system.snapshot());
    api.get('/api/system/services', async () => await listServices());
    api.post<{ Params: { unit: string }; Body: { verb: string } }>('/api/system/services/:unit', async (req, reply) => {
      if (!isUnit(req.params.unit)) return reply.code(400).send({ error: 'unit 名不合法' });
      try {
        await serviceAction(req.params.unit, req.body.verb);
        audit(whoami(req), `service:${req.body.verb}`, req.params.unit);
        return { ok: true, active: await isActive(req.params.unit) };
      } catch (e: any) {
        return reply.code(500).send({ error: e.message });
      }
    });

    // SSE log streaming for any unit
    const logStream = (req: any, reply: any, unit: string) => {
      if (!isUnit(unit)) {
        reply.code(400);
        reply.send({ error: 'unit 名不合法' });
        return;
      }
      reply.hijack();
      reply.raw.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });
      const lines = Math.min(Number(req.query.lines) || 200, 2000);
      const child = spawnLines(
        'journalctl',
        ['-u', unit, '-n', String(lines), '--no-pager', '-o', 'short-iso', '-f'],
        (l) => reply.raw.write(`data: ${JSON.stringify(l)}\n\n`),
        () => reply.raw.end(),
      );
      const ping = setInterval(() => reply.raw.write(': ping\n\n'), 25000);
      req.raw.on('close', () => {
        clearInterval(ping);
        child.kill();
      });
    };
    api.get('/api/system/services/:unit/logs', (req: any, reply: any) => logStream(req, reply, req.params.unit));

    // apps
    api.get('/api/apps', async () => await apps.listApps());
    api.get<{ Params: { id: string } }>('/api/apps/:id', async (req, reply) => {
      const a = apps.getApp(Number(req.params.id));
      if (!a) return reply.code(404).send({ error: '应用不存在' });
      const unit = apps.unitName(a);
      const running = await isActive(unit);
      const port = a.port ?? (running ? await apps.detectPort(unit) : null);
      return { ...a, running, unit, port, portAuto: a.port == null && port != null };
    });
    api.post<{ Body: apps.CreateAppInput }>('/api/apps', async (req, reply) => {
      try {
        const a = apps.createApp(req.body);
        audit(whoami(req), 'app:create', a.name);
        return a;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.patch<{ Params: { id: string }; Body: Partial<apps.CreateAppInput> }>('/api/apps/:id', async (req, reply) => {
      try {
        const a = apps.updateApp(Number(req.params.id), req.body);
        audit(whoami(req), 'app:update', a.name);
        return a;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.delete<{ Params: { id: string }; Querystring: { purge?: string } }>('/api/apps/:id', async (req, reply) => {
      try {
        await apps.deleteApp(Number(req.params.id), req.query.purge === '1');
        audit(whoami(req), 'app:delete', req.params.id);
        return { ok: true };
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.post<{ Params: { id: string }; Body: { verb: string } }>('/api/apps/:id/action', async (req, reply) => {
      try {
        const ok = await apps.appAction(Number(req.params.id), req.body.verb as any);
        audit(whoami(req), `app:${req.body.verb}`, req.params.id);
        return { ok, active: ok };
      } catch (e: any) {
        return reply.code(500).send({ error: e.message });
      }
    });
    api.post<{ Params: { id: string } }>('/api/apps/:id/deploy', async (req, reply) => {
      try {
        const depId = await apps.deployApp(Number(req.params.id));
        audit(whoami(req), 'app:deploy', req.params.id);
        return { deployment: depId };
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get<{ Params: { id: string } }>('/api/apps/:id/deployments', async (req) =>
      db
        .prepare('SELECT id,app_id,status,started_at,finished_at,length(log) log_len FROM deployments WHERE app_id=? ORDER BY id DESC LIMIT 20')
        .all(Number(req.params.id)),
    );
    api.get<{ Params: { id: string; depId: string } }>('/api/apps/:id/deployments/:depId', async (req) =>
      db.prepare('SELECT id,status,started_at,finished_at,log FROM deployments WHERE id=? AND app_id=?').get(
        Number(req.params.depId),
        Number(req.params.id),
      ),
    );
    api.post<{ Params: { id: string }; Body: { email?: string } }>('/api/apps/:id/ssl', async (req, reply) => {
      try {
        const out = await apps.attachSsl(Number(req.params.id), req.body?.email);
        audit(whoami(req), 'app:ssl', req.params.id);
        return { ok: true, log: out };
      } catch (e: any) {
        return reply.code(500).send({ error: e.message });
      }
    });
    api.get<{ Params: { id: string } }>('/api/apps/:id/nginx', async (req, reply) => {
      const a = apps.getApp(Number(req.params.id));
      if (!a) return reply.code(404).send({ error: '应用不存在' });
      const running = await isActive(apps.unitName(a));
      const port = a.port ?? (running ? await apps.detectPort(apps.unitName(a)) : null);
      return { domain: a.domain, port, configs: findAppConfigs(port, a.domain) };
    });
    api.put<{ Params: { id: string }; Body: { file: string; content: string } }>('/api/apps/:id/nginx', async (req, reply) => {
      try {
        const r = await saveAppConfig(req.body.file, req.body.content);
        audit(whoami(req), 'app:nginx-save', req.body.file);
        return r;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.post<{ Params: { id: string }; Body: { file: string; kind: 'ws' | 'body' | 'redirect'; value?: string } }>('/api/apps/:id/nginx/quick', async (req, reply) => {
      try {
        const r = await quickEditConfig(req.body.file, req.body.kind, req.body.value);
        audit(whoami(req), `app:nginx-${req.body.kind}`, req.body.file);
        return r;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get<{ Params: { id: string } }>('/api/apps/:id/unit', async (req, reply) => {
      try {
        return apps.readUnit(Number(req.params.id));
      } catch (e: any) {
        return reply.code(404).send({ error: e.message });
      }
    });
    api.put<{ Params: { id: string }; Body: { content: string } }>('/api/apps/:id/unit', async (req, reply) => {
      try {
        const r = await apps.saveUnit(Number(req.params.id), req.body?.content ?? '');
        audit(whoami(req), 'app:unit', req.params.id);
        return r;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get<{ Params: { id: string } }>('/api/apps/:id/proc', async (req, reply) => {
      try {
        return await apps.appProc(Number(req.params.id));
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get('/api/apps/:id/logs', (req: any, reply: any) => {
      const a = apps.getApp(Number(req.params.id));
      if (!a) {
        reply.code(404);
        reply.send({ error: '应用不存在' });
        return;
      }
      logStream(req, reply, apps.unitName(a));
    });

    // databases
    api.get('/api/db/pg/databases', async () => {
      try {
        return await pg.listDbs();
      } catch (e: any) {
        return { error: e.message };
      }
    });
    api.get('/api/db/pg/roles', async () => {
      try {
        return await pg.listRoles();
      } catch (e: any) {
        return { error: e.message };
      }
    });
    api.post<{ Body: { action: string; name: string; owner?: string; password?: string; db?: string } }>(
      '/api/db/pg/manage',
      async (req, reply) => {
        try {
          const { action, name, owner, password, db: d } = req.body;
          if (action === 'createDb') await pg.createDb(name, owner);
          else if (action === 'dropDb') await pg.dropDb(name);
          else if (action === 'createRole') await pg.createRole(name, password || '');
          else if (action === 'setPassword') await pg.setRolePassword(name, password || '');
          else if (action === 'dropRole') await pg.dropRole(name);
          else if (action === 'grant') await pg.grant(name, d || '');
          else throw new Error('未知操作');
          audit(whoami(req), `db:${action}`, name);
          return { ok: true };
        } catch (e: any) {
          return reply.code(400).send({ error: e.message });
        }
      },
    );
    api.post<{ Body: { sql: string } }>('/api/db/pg/query', async (req, reply) => {
      try {
        const out = await pg.query(req.body.sql || '');
        audit(whoami(req), 'db:sql', (req.body.sql || '').slice(0, 80));
        return { result: out };
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get('/api/db/redis', async () => {
      try {
        return await pg.redisInfo();
      } catch (e: any) {
        return { error: e.message };
      }
    });
    api.post<{ Body: { password: string } }>('/api/db/redis/password', async (req) => {
      pg.setRedisPassword(req.body.password || '');
      return { ok: true };
    });

    // backups
    api.get('/api/backups', async () => backups.listBackups());
    api.post<{ Body: backups.CreateBackupInput }>('/api/backups', async (req, reply) => {
      try {
        const b = await backups.createBackup(req.body);
        audit(whoami(req), 'backup:create', `${req.body.kind}:${req.body.target}`);
        return b;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.patch<{ Params: { id: string }; Body: Partial<backups.CreateBackupInput> }>('/api/backups/:id', async (req, reply) => {
      try {
        return await backups.updateBackup(Number(req.params.id), req.body);
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.delete<{ Params: { id: string } }>('/api/backups/:id', async (req, reply) => {
      try {
        await backups.deleteBackup(Number(req.params.id));
        audit(whoami(req), 'backup:delete', req.params.id);
        return { ok: true };
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.post<{ Params: { id: string } }>('/api/backups/:id/run', async (req, reply) => {
      try {
        const r = await backups.runBackupNow(Number(req.params.id));
        return { code: r.code, log: r.out };
      } catch (e: any) {
        return reply.code(500).send({ error: e.message });
      }
    });

    // files
    api.get<{ Querystring: { path?: string } }>('/api/files', async (req, reply) => {
      try {
        return files.listDir(req.query.path || '/root/www');
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get<{ Querystring: { path?: string } }>('/api/files/content', async (req, reply) => {
      try {
        return files.readText(req.query.path || '');
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.put<{ Querystring: { path?: string } }>('/api/files/content', async (req, reply) => {
      try {
        const buf = req.body as unknown as Buffer;
        return files.writeText(req.query.path || '', Buffer.isBuffer(buf) ? buf.toString('utf8') : String(buf));
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.get<{ Querystring: { path?: string } }>('/api/files/download', (req: any, reply: any) => {
      try {
        return files.streamFile(req.query.path || '', reply);
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });
    api.post<{ Body: { action: string; path: string; path2?: string } }>('/api/files/action', async (req, reply) => {
      try {
        const r = files.fsAction(req.body.action, req.body.path, req.body.path2);
        audit(whoami(req), `file:${req.body.action}`, req.body.path);
        return r;
      } catch (e: any) {
        return reply.code(400).send({ error: e.message });
      }
    });

    // users (admin only)
    const forbid = (req: any, reply: any) => {
      if (req.cpRole !== 'admin') {
        reply.code(403).send({ error: '需要管理员权限' });
        return true;
      }
      return false;
    };
    api.get('/api/users', async (req, reply) => {
      if (forbid(req, reply)) return;
      return db.prepare('SELECT id,username,role,created_at FROM users ORDER BY id').all();
    });
    api.post<{ Body: { username: string; password: string; role: string } }>('/api/users', async (req, reply) => {
      if (forbid(req, reply)) return;
      if (!/^[A-Za-z0-9_-]{3,32}$/.test(req.body.username || '')) return reply.code(400).send({ error: '用户名不合法' });
      if (!req.body.password || req.body.password.length < 8) return reply.code(400).send({ error: '密码至少 8 位' });
      const role = req.body.role === 'viewer' ? 'viewer' : 'admin';
      try {
        db.prepare('INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)').run(req.body.username, hashPass(req.body.password), role);
      } catch {
        return reply.code(400).send({ error: '用户名已存在' });
      }
      audit(whoami(req), 'user:create', `${req.body.username}(${role})`);
      return { ok: true };
    });
    api.patch<{ Params: { id: string }; Body: { role?: string; password?: string } }>('/api/users/:id', async (req, reply) => {
      if (forbid(req, reply)) return;
      const u = db.prepare('SELECT * FROM users WHERE id=?').get(Number(req.params.id)) as any;
      if (!u) return reply.code(404).send({ error: '用户不存在' });
      if (req.body.role) {
        if (u.username === whoami(req)) return reply.code(400).send({ error: '不能修改自己的角色' });
        db.prepare('UPDATE users SET role=? WHERE id=?').run(req.body.role === 'viewer' ? 'viewer' : 'admin', u.id);
      }
      if (req.body.password) {
        if (req.body.password.length < 8) return reply.code(400).send({ error: '密码至少 8 位' });
        db.prepare('UPDATE users SET pass_hash=? WHERE id=?').run(hashPass(req.body.password), u.id);
      }
      audit(whoami(req), 'user:update', u.username);
      return { ok: true };
    });
    api.delete<{ Params: { id: string } }>('/api/users/:id', async (req, reply) => {
      if (forbid(req, reply)) return;
      const u = db.prepare('SELECT * FROM users WHERE id=?').get(Number(req.params.id)) as any;
      if (!u) return reply.code(404).send({ error: '用户不存在' });
      if (u.username === whoami(req)) return reply.code(400).send({ error: '不能删除自己' });
      db.prepare('DELETE FROM users WHERE id=?').run(u.id);
      audit(whoami(req), 'user:delete', u.username);
      return { ok: true };
    });

    // alert settings
    api.get('/api/settings/alerts', async () => {
      const keys = ['alert_telegram_bot', 'alert_telegram_chat', 'alert_webhook_url', 'alert_disk_pct', 'alert_ssl_days'];
      const out: Record<string, string> = {};
      for (const k of keys) out[k] = getSetting(k) || '';
      return out;
    });
    api.post<{ Body: Record<string, string> }>('/api/settings/alerts', async (req, reply) => {
      if (forbid(req, reply)) return;
      for (const [k, v] of Object.entries(req.body)) {
        if (/^alert_(telegram_bot|telegram_chat|webhook_url|disk_pct|ssl_days)$/.test(k)) setSetting(k, String(v ?? ''));
      }
      audit(whoami(req), 'settings:alerts', Object.keys(req.body).join(','));
      return { ok: true };
    });
    api.post('/api/settings/alerts/test', async (req, reply) => {
      if (forbid(req, reply)) return;
      const { notifyTest } = await import('./alerts.js');
      await notifyTest();
      return { ok: true };
    });
    api.post('/api/settings/alerts/run-checks', async (req, reply) => {
      if (forbid(req, reply)) return;
      await runChecks();
      return { ok: true };
    });

    // firewall (read-only status)
    api.get('/api/firewall', async () => {
      const has = await run('which', ['ufw']);
      if (has.code !== 0) return { tool: 'none', active: false, output: '未安装 ufw' };
      const st = await run('ufw', ['status', 'verbose']);
      const active = /Status: active/.test(st.out);
      return { tool: 'ufw', active, output: st.out.split('\n').slice(0, 80).join('\n') };
    });

    // web terminal (admin only)
    api.register(async (ws) => {
      ws.get('/api/terminal', { websocket: true }, (socket, req) => {
        if ((req as any).cpRole !== 'admin') {
          socket.close();
          return;
        }
        const ip = req.ip;
        audit((req.query as any).user ?? ip, 'terminal:open');
        const term = pty.spawn('/bin/bash', [], {
          name: 'xterm-256color',
          cols: 100,
          rows: 28,
          cwd: '/root',
          env: { ...(process.env as any), TERM: 'xterm-256color', PS1: '[choyeon-panel \\W]\\# ' },
        });
        term.onData((d) => {
          if (socket.readyState === 1) socket.send(JSON.stringify({ d: 'out', data: d }));
        });
        socket.on('message', (raw: Buffer | string) => {
          let msg: any;
          try {
            msg = JSON.parse(String(raw));
          } catch {
            return;
          }
          if (msg.d === 'input') term.write(String(msg.data).slice(0, 65536));
          else if (msg.d === 'resize') {
            try {
              term.resize(Math.min(Number(msg.cols) || 100, 400), Math.min(Number(msg.rows) || 28, 200));
            } catch {}
          } else if (msg.d === 'ping') socket.send(JSON.stringify({ d: 'pong' }));
        });
        const cleanup = () => term.kill();
        socket.on('close', cleanup);
        socket.on('error', cleanup);
        term.onExit(() => socket.close());
      });
    });

    // misc
    api.get('/api/audit', async (req) =>
      db.prepare('SELECT * FROM audit ORDER BY id DESC LIMIT ?').all(Math.min(Number((req.query as any).limit) || 100, 500)),
    );
    api.post('/api/system/certbot-renew', async (req) => {
      audit(whoami(req), 'certbot:renew');
      return { log: await renewCerts() };
    });
    api.post<{ Body: { old?: string; new?: string } }>('/api/auth/password', async (req, reply) => {
      const me = db.prepare('SELECT * FROM users WHERE username=?').get(whoami(req)) as { pass_hash: string; username: string };
      if (!req.body.old || !checkPass(req.body.old, me.pass_hash)) return reply.code(400).send({ error: '原密码错误' });
      if (!req.body.new || req.body.new.length < 8) return reply.code(400).send({ error: '新密码至少 8 位' });
      db.prepare('UPDATE users SET pass_hash=? WHERE username=?').run(hashPass(req.body.new), me.username);
      audit(me.username, 'password_change');
      return { ok: true };
    });
  },
  { prefix: '' },
);

// ---------- static frontend ----------
const dist = fileURLToPath(new URL('../../web/dist', import.meta.url));
if (existsSync(dist)) {
  await app.register(fastifyStatic, { root: dist, wildcard: false });
  app.setNotFoundHandler((req, reply) => {
    if (req.url.startsWith('/api')) return reply.code(404).send({ error: 'not found' });
    return reply.sendFile('index.html');
  });
}

await app.listen({ port: PORT, host: HOST });
startChecker();
console.log(`choyeon-panel listening on http://${HOST}:${PORT}`);
