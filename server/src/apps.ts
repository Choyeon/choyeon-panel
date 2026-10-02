import { writeFileSync, unlinkSync, existsSync, rmSync, readdirSync, readFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { db } from './db.js';
import { run, isName, isPort, isDomain, isBranch, isGitUrl } from './util.js';
import { applyVhost, removeVhost, domainsByPort } from './nginx.js';
import { serviceAction, isActive } from './systemd.js';

const APP_ROOT = process.env.CP_APP_ROOT || '/root/www';

export interface EnvVar { k: string; v: string }

export interface AppRow {
  id: number;
  name: string;
  type: 'node' | 'python';
  repo_url: string;
  branch: string;
  path: string;
  port: number | null;
  domain: string | null;
  install_cmd: string | null;
  start_cmd: string;
  env: string;
  unit_override: string | null;
  db_names: string | null;
  unit_template: string | null;
  created_at: string;
  updated_at: string;
}

export function unitName(app: AppRow) {
  return app.unit_override || `panel-${app.name}.service`;
}

export const UNIT_TEMPLATE_HELPERS = {
  '{{name}}': (a: AppRow) => a.name,
  '{{path}}': (a: AppRow) => a.path,
  '{{start_cmd}}': (a: AppRow) => JSON.stringify(a.start_cmd),
  '{{port}}': (a: AppRow) => String(a.port ?? ''),
};

export function renderUnitFrom(tpl: string, app: AppRow): string {
  let out = tpl;
  for (const [k, f] of Object.entries(UNIT_TEMPLATE_HELPERS)) out = out.split(k).join(f(app));
  return out;
}

function renderUnit(app: AppRow) {
  if (app.unit_template) return renderUnitFrom(app.unit_template, app);
  return `[Unit]
Description=choyeon-panel app ${app.name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${app.path}
EnvironmentFile=-${app.path}/.panel.env
ExecStart=/bin/bash -lc ${JSON.stringify(app.start_cmd)}
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
`;
}

function writeEnvFile(app: AppRow) {
  const vars = JSON.parse(app.env || '[]') as EnvVar[];
  const body = vars
    .filter((x) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(x.k))
    .map((x) => `${x.k}="${x.v.replace(/"/g, '\\"').replace(/\n/g, ' ')}"`)
    .join('\n');
  writeFileSync(`${app.path}/.panel.env`, body + '\n');
}

export async function ensureUnit(app: AppRow) {
  if (app.unit_override) return;
  writeFileSync(`/etc/systemd/system/${unitName(app)}`, renderUnit(app));
  const r = await run('systemctl', ['daemon-reload']);
  if (r.code !== 0) throw new Error(r.out);
}

async function removeUnit(app: AppRow) {
  if (app.unit_override) return;
  await run('systemctl', ['disable', '--now', unitName(app)], { timeout: 30000 });
  const p = `/etc/systemd/system/${unitName(app)}`;
  if (existsSync(p)) unlinkSync(p);
  await run('systemctl', ['daemon-reload']);
}

let ssCache: { t: number; lines: string[] } | null = null;
function listenLines(): string[] {
  if (ssCache && Date.now() - ssCache.t < 5000) return ssCache.lines;
  try {
    ssCache = { t: Date.now(), lines: execSync('ss -tlnp', { encoding: 'utf8', timeout: 4000 }).split('\n') };
  } catch {
    ssCache = { t: Date.now(), lines: [] };
  }
  return ssCache.lines;
}

export function detectPort(unit: string): Promise<number | null> {
  return (async () => {
    const r = await run('systemctl', ['show', unit, '-p', 'MainPID', '--value'], { timeout: 5000 });
    const main = parseInt(r.out.trim()) || 0;
    if (!main) return null;
    const pids = new Set<number>([main]);
    const queue = [main];
    while (queue.length) {
      const p = queue.shift()!;
      try {
        for (const t of readdirSync(`/proc/${p}/task`)) {
          for (const k of readFileSync(`/proc/${p}/task/${t}/children`, 'utf8').trim().split(/\s+/).filter(Boolean).map(Number)) {
            if (k && !pids.has(k)) { pids.add(k); queue.push(k); }
          }
        }
      } catch {}
    }
    const ports = new Set<number>();
    for (const line of listenLines()) {
      const pm = line.match(/pid=(\d+)/);
      if (!pm || !pids.has(+pm[1])) continue;
      const addr = line.trim().split(/\s+/)[3] || '';
      const port = addr.match(/:(\d+)$/);
      if (port) ports.add(+port[1]);
    }
    return ports.size ? Math.min(...ports) : null;
  })();
}

export async function listApps() {
  const rows = db.prepare('SELECT * FROM apps ORDER BY name').all() as AppRow[];
  const domMap = domainsByPort();
  return Promise.all(
    rows.map(async (a) => {
      const running = await isActive(unitName(a));
      const detected = a.port == null && running ? await detectPort(unitName(a)) : null;
      const port = a.port ?? detected;
      const domains = a.domain ? [a.domain] : port != null ? domMap.get(port) || [] : [];
      return {
        ...a,
        env: undefined,
        running,
        unit: unitName(a),
        port,
        portAuto: a.port == null && detected != null,
        domains,
        deploying: !!db.prepare(`SELECT id FROM deployments WHERE app_id=? AND status='running'`).get(a.id),
      };
    }),
  );
}

export function getApp(id: number) {
  return db.prepare('SELECT * FROM apps WHERE id=?').get(id) as AppRow | undefined;
}

export interface CreateAppInput {
  name: string;
  type: 'node' | 'python';
  repo_url: string;
  branch?: string;
  path?: string;
  port?: number | null;
  domain?: string | null;
  install_cmd?: string | null;
  start_cmd: string;
  env?: EnvVar[];
  unit_override?: string | null;
  db_names?: string | null;
  unit_template?: string | null;
}

export function validateAppInput(i: CreateAppInput, ignoreId?: number) {
  if (!isName(i.name)) throw new Error('名称只能包含小写字母、数字、连字符，且以字母开头');
  if (!['node', 'python'].includes(i.type)) throw new Error('类型必须是 node 或 python');
  if (i.repo_url && !isGitUrl(i.repo_url)) throw new Error('仓库地址不合法');
  if (i.branch && !isBranch(i.branch)) throw new Error('分支名不合法');
  if (i.port != null && !isPort(i.port)) throw new Error('端口必须在 1024-65535');
  if (i.domain && !isDomain(i.domain)) throw new Error('域名不合法');
  if (!i.start_cmd || i.start_cmd.length > 500) throw new Error('启动命令必填且不超过 500 字符');
  if (i.path && !/^\/[A-Za-z0-9._/-]{2,200}$/.test(i.path)) throw new Error('安装目录必须是绝对路径且字符合法');
  if (i.unit_template) {
    if (i.unit_template.length > 4000) throw new Error('unit 模板过长');
    if (/\$\{|\$\(|`/.test(i.unit_template)) throw new Error('unit 模板不允许 shell 替换语法');
  }
  if (i.unit_override && !/^[A-Za-z0-9@:._-]{1,64}\.service$/.test(i.unit_override)) throw new Error('unit 名不合法');
  if (i.db_names) {
    for (const d of i.db_names.split(',')) if (d && !/^[a-z0-9_]{1,63}$/.test(d)) throw new Error(`关联数据库名不合法: ${d}`);
  }
  const dup = db
    .prepare('SELECT id FROM apps WHERE (name=? OR path=? OR (domain IS NOT NULL AND domain=? AND domain!=\x27\x27)) AND id IS NOT ?')
    .get(i.name, i.path ?? `${APP_ROOT}/${i.name}`, i.domain ?? '', ignoreId ?? -1);
  if (dup) throw new Error('名称/路径/域名已被其他应用占用');
  const busy = db.prepare(`SELECT id FROM apps WHERE port=? AND id IS NOT ?`).get(i.port ?? -1, ignoreId ?? -1) as { id: number } | undefined;
  if (busy && i.port) throw new Error(`端口 ${i.port} 已被应用 #${busy.id} 使用`);
}

export function createApp(i: CreateAppInput) {
  validateAppInput(i);
  const path = i.path ?? `${APP_ROOT}/${i.name}`;
  const res = db
    .prepare(
      `INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,start_cmd,env,unit_override,db_names,unit_template)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    )
    .run(
      i.name,
      i.type,
      i.repo_url,
      i.branch || 'main',
      path,
      i.port ?? null,
      i.domain || null,
      i.install_cmd || null,
      i.start_cmd,
      JSON.stringify(i.env ?? []),
      i.unit_override || null,
      i.db_names || null,
      i.unit_template || null,
    );
  return getApp(res.lastInsertRowid as number)!;
}

export function updateApp(id: number, i: Partial<CreateAppInput>) {
  const cur = getApp(id);
  if (!cur) throw new Error('应用不存在');
  const merged: CreateAppInput = { ...cur, ...i, env: undefined as any, ...(i.env ? { env: i.env } : {}) } as CreateAppInput;
  validateAppInput(merged, id);
  const vars = i.env ?? JSON.parse(cur.env || '[]');
  db.prepare(
    `UPDATE apps SET name=?,type=?,repo_url=?,branch=?,path=?,port=?,domain=?,install_cmd=?,start_cmd=?,env=?,unit_override=?,db_names=?,unit_template=?,updated_at=datetime('now') WHERE id=?`,
  ).run(
    merged.name,
    merged.type,
    merged.repo_url ?? '',
    merged.branch || 'main',
    merged.path ?? cur.path,
    merged.port ?? null,
    merged.domain || null,
    merged.install_cmd || null,
    merged.start_cmd,
    JSON.stringify(vars),
    merged.unit_override || null,
    merged.db_names || null,
    merged.unit_template || null,
    id,
  );
  return getApp(id)!;
}

export async function deleteApp(id: number, purge: boolean) {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  const running = db.prepare(`SELECT id FROM deployments WHERE app_id=? AND status='running'`).get(id);
  if (running) throw new Error('部署进行中，不能删除');
  if (!app.unit_override) {
    await run('systemctl', ['disable', '--now', unitName(app)], { timeout: 30000 });
  }
  await removeUnit(app);
  if (app.domain) await removeVhost(app.name);
  db.prepare('DELETE FROM apps WHERE id=?').run(id);
  if (purge && app.path.startsWith(APP_ROOT) && existsSync(app.path)) rmSync(app.path, { recursive: true, force: true });
}

export async function appAction(id: number, verb: 'start' | 'stop' | 'restart') {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  await serviceAction(unitName(app), verb);
  return await isActive(unitName(app));
}

function appendLog(depId: number, text: string) {
  db.prepare('UPDATE deployments SET log = log || ? WHERE id=?').run(`\n${text}\n`, depId);
}

export async function deployApp(id: number): Promise<number> {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  if (db.prepare(`SELECT id FROM deployments WHERE app_id=? AND status='running'`).get(id)) {
    throw new Error('该应用已有部署任务在运行');
  }
  const dep = db.prepare(`INSERT INTO deployments(app_id,status) VALUES(?,'running')`).run(id);
  const depId = dep.lastInsertRowid as number;
  void runDeployment(app, depId);
  return depId;
}

async function runDeployment(app: AppRow, depId: number) {
  const finish = (ok: boolean) => {
    db.prepare(`UPDATE deployments SET status=?, finished_at=datetime('now') WHERE id=?`).run(ok ? 'success' : 'failed', depId);
    db.prepare(`UPDATE apps SET updated_at=datetime('now') WHERE id=?`).run(app.id);
  };
  try {
    if (app.repo_url) {
      if (existsSync(`${app.path}/.git`)) {
        appendLog(depId, `==> git fetch/checkout ${app.branch}`);
        let r = await run('git', ['fetch', 'origin', app.branch], { cwd: app.path, timeout: 300000 });
        if (r.code !== 0) throw new Error(r.out);
        r = await run('git', ['checkout', app.branch], { cwd: app.path });
        if (r.code !== 0) throw new Error(r.out);
        r = await run('git', ['reset', '--hard', `origin/${app.branch}`], { cwd: app.path });
        if (r.code !== 0) throw new Error(r.out);
        appendLog(depId, r.out);
      } else {
        appendLog(depId, `==> git clone -b ${app.branch} ${app.repo_url} -> ${app.path}`);
        const r = await run('git', ['clone', '--depth', '1', '-b', app.branch, app.repo_url, app.path], { timeout: 600000 });
        if (r.code !== 0) throw new Error(r.out);
        appendLog(depId, r.out);
      }
    } else {
      appendLog(depId, `==> 跳过拉取（未配置仓库，使用现有目录 ${app.path}）`);
    }
    if (!existsSync(app.path)) throw new Error(`目录不存在: ${app.path}`);

    appendLog(depId, `==> 安装依赖 (${app.type})`);
    const install =
      app.install_cmd ||
      (app.type === 'node' ? 'npm install' : 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt');
    const ir = await run('/bin/bash', ['-lc', install], { cwd: app.path, timeout: 1800000 });
    appendLog(depId, ir.out || '(no output)');
    if (ir.code !== 0) throw new Error(`安装依赖失败 (exit ${ir.code})`);

    appendLog(depId, '==> 写入环境变量与 systemd unit');
    writeEnvFile(app);
    await ensureUnit(app);

    if (app.domain && app.port) {
      appendLog(depId, `==> 应用 nginx 反代 ${app.domain} -> 127.0.0.1:${app.port}`);
      await applyVhost({ name: app.name, domain: app.domain, port: app.port });
    }

    appendLog(depId, `==> 重启服务 ${unitName(app)}`);
    await serviceAction(unitName(app), 'restart');
    appendLog(depId, '==> 部署完成');
    finish(true);
  } catch (e: any) {
    appendLog(depId, `!!! 部署失败: ${e.message}`);
    finish(false);
  }
}

export async function attachSsl(id: number, email?: string) {
  const app = getApp(id);
  if (!app?.domain) throw new Error('应用未配置域名');
  const { issueCert } = await import('./nginx.js');
  const out = await issueCert(app.domain, email);
  await applyVhost({ name: app.name, domain: app.domain, port: app.port! });
  return out;
}

const UNIT_DIR = '/etc/systemd/system';

export function readUnit(id: number) {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  const unit = unitName(app);
  const path = `${UNIT_DIR}/${unit}`;
  const managed = !app.unit_override;
  let content = '';
  if (existsSync(path)) content = readFileSync(path, 'utf8');
  return {
    unit,
    path,
    managed,
    content,
    template: app.unit_template,
    defaultTemplate: renderUnit({ ...app, unit_template: null }),
  };
}

export async function saveUnit(id: number, content: string) {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  if (app.unit_override) throw new Error('纳管模式：unit 由外部管理，面板不写文件');
  if (!content.trim()) throw new Error('unit 内容不能为空');
  if (content.length > 8000) throw new Error('unit 内容过长');
  if (/\$\{|\$\(|`/.test(content)) throw new Error('unit 不允许 shell 替换语法');
  if (!/\[Service\]/.test(content)) throw new Error('unit 必须包含 [Service] 段');
  if (!/^ExecStart=/m.test(content)) throw new Error('unit 必须包含 ExecStart=');
  const unit = unitName(app);
  const path = `${UNIT_DIR}/${unit}`;
  const old = existsSync(path) ? readFileSync(path, 'utf8') : null;
  writeFileSync(path, content.endsWith('\n') ? content : content + '\n');
  const verify = await run('systemd-analyze', ['verify', path], { timeout: 30000 });
  if (verify.code !== 0) {
    if (old != null) writeFileSync(path, old);
    throw new Error(`systemd-analyze verify 失败，已回滚：\n${verify.out}`);
  }
  const reload = await run('systemctl', ['daemon-reload']);
  if (reload.code !== 0) {
    if (old != null) writeFileSync(path, old);
    await run('systemctl', ['daemon-reload']);
    throw new Error(`daemon-reload 失败，已回滚：\n${reload.out}`);
  }
  db.prepare(`UPDATE apps SET unit_template=?,updated_at=datetime('now') WHERE id=?`).run(content + '\n', id);
  return { ok: true, unit, restartRequired: true };
}

export async function appProc(id: number) {
  const app = getApp(id);
  if (!app) throw new Error('应用不存在');
  const unit = unitName(app);
  const fields = [
    'MainPID',
    'MemoryCurrent',
    'NRestarts',
    'ActiveEnterTimestamp',
    'CPUUsageNSec',
    'ExecMainStartTimestamp',
    'SubState',
  ];
  const args = ['show', unit, ...fields.map((f) => `-p${f}`)];
  const r = await run('systemctl', args, { timeout: 8000 });
  const got: Record<string, string> = {};
  for (const line of r.out.split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) got[line.slice(0, i)] = line.slice(i + 1).trim();
  }
  const pid = parseInt(got.MainPID || '0') || 0;
  const mem = parseInt(got.MemoryCurrent || '');
  const cpuNs = parseInt(got.CPUUsageNSec || '');
  return {
    unit,
    subState: got.SubState || 'unknown',
    running: pid > 0,
    pid: pid || null,
    memoryBytes: Number.isFinite(mem) ? mem : null,
    restarts: parseInt(got.NRestarts || '0') || 0,
    activeSince: got.ActiveEnterTimestamp && got.ActiveEnterTimestamp !== 'n/a' ? got.ActiveEnterTimestamp : null,
    execStartAt: got.ExecMainStartTimestamp && got.ExecMainStartTimestamp !== 'n/a' ? got.ExecMainStartTimestamp : null,
    cpuSeconds: Number.isFinite(cpuNs) ? Math.round(cpuNs / 1e6) / 1000 : null,
  };
}
