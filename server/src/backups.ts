import { existsSync, readdirSync, statSync, unlinkSync, readFileSync, writeFileSync } from 'node:fs';
import { db } from './db.js';
import { run, isName } from './util.js';
import { isIdent } from './pg.js';

export const BACKUP_DIR = process.env.CP_BACKUP_DIR || '/root/backups/panel';

export interface BackupRow {
  id: number;
  kind: 'pg' | 'app';
  target: string;
  schedule: 'manual' | 'daily' | 'weekly';
  hour: number;
  minute: number;
  keep: number;
  enabled: number;
}

export function listBackups() {
  const rows = db.prepare('SELECT * FROM backups ORDER BY id').all() as BackupRow[];
  return rows.map((b) => {
    const files = existsSync(BACKUP_DIR)
      ? readdirSync(BACKUP_DIR)
          .filter((f) => f.startsWith(`bk${b.id}-`))
          .sort()
          .reverse()
          .slice(0, 30)
          .map((f) => {
            const st = statSync(`${BACKUP_DIR}/${f}`);
            return { name: f, size: st.size, mtime: st.mtime.toISOString() };
          })
      : [];
    return { ...b, files };
  });
}

function unitBase(id: number) {
  return `panel-backup-${id}`;
}

async function syncTimer(b: BackupRow) {
  const u = unitBase(b.id);
  if (b.schedule === 'manual' || !b.enabled) {
    await run('systemctl', ['disable', '--now', `${u}.timer`], { timeout: 30000 });
    for (const f of [`/etc/systemd/system/${u}.timer`, `/etc/systemd/system/${u}.service`]) if (existsSync(f)) unlinkSync(f);
    await run('systemctl', ['daemon-reload']);
    return;
  }
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const cal = `${b.schedule === 'weekly' ? 'Mon ' : ''}${String(b.hour).padStart(2, '0')}:${String(b.minute).padStart(2, '0')} ${tz}`;
  const { resolve } = await import('node:path');
  const runner = resolve(import.meta.dirname, 'backup-runner.js');
  const data = process.env.CP_DATA_DIR || resolve(import.meta.dirname, '../../data');
  writeIfChanged(
    `/etc/systemd/system/${u}.service`,
    `[Unit]
Description=choyeon-panel backup #${b.id}
[Service]
Type=oneshot
ExecStart=/usr/local/bin/node ${runner} ${b.id}
Environment=CP_DATA_DIR=${data}
`,
  );
  writeIfChanged(
    `/etc/systemd/system/${u}.timer`,
    `[Unit]
Description=choyeon-panel backup timer #${b.id}
[Timer]
OnCalendar=${cal}
Persistent=true
[Install]
WantedBy=timers.target
`,
  );
  await run('systemctl', ['daemon-reload']);
  const r = await run('systemctl', ['enable', '--now', `${u}.timer`], { timeout: 30000 });
  if (b.enabled && r.code !== 0) throw new Error(r.out);
}

function writeIfChanged(path: string, content: string) {
  if (existsSync(path) && readFileSync(path, 'utf8') === content) return;
  writeFileSync(path, content);
}

export interface CreateBackupInput {
  kind: 'pg' | 'app';
  target: string;
  schedule: 'manual' | 'daily' | 'weekly';
  hour?: number;
  minute?: number;
  keep?: number;
  enabled?: boolean;
}

export async function createBackup(i: CreateBackupInput) {
  if (!['pg', 'app'].includes(i.kind)) throw new Error('kind 必须是 pg 或 app');
  if (i.kind === 'pg' && i.target !== 'all' && !isIdent(i.target)) throw new Error('PG 库名不合法');
  if (i.kind === 'app' && !isName(i.target)) throw new Error('应用名不合法');
  const res = db
    .prepare('INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES(?,?,?,?,?,?,?)')
    .run(i.kind, i.target, i.schedule || 'daily', i.hour ?? 3, i.minute ?? 30, i.keep ?? 7, i.enabled === false ? 0 : 1);
  const row = db.prepare('SELECT * FROM backups WHERE id=?').get(res.lastInsertRowid) as BackupRow;
  await syncTimer(row);
  return row;
}

export async function updateBackup(id: number, i: Partial<CreateBackupInput>) {
  const cur = db.prepare('SELECT * FROM backups WHERE id=?').get(id) as BackupRow | undefined;
  if (!cur) throw new Error('备份任务不存在');
  db.prepare(
    `UPDATE backups SET kind=?,target=?,schedule=?,hour=?,minute=?,keep=?,enabled=? WHERE id=?`,
  ).run(
    i.kind ?? cur.kind,
    i.target ?? cur.target,
    i.schedule ?? cur.schedule,
    i.hour ?? cur.hour,
    i.minute ?? cur.minute,
    i.keep ?? cur.keep,
    i.enabled === undefined ? cur.enabled : i.enabled ? 1 : 0,
    id,
  );
  const row = db.prepare('SELECT * FROM backups WHERE id=?').get(id) as BackupRow;
  await syncTimer(row);
  return row;
}

export async function deleteBackup(id: number) {
  const u = unitBase(id);
  await run('systemctl', ['disable', '--now', `${u}.timer`], { timeout: 30000 });
  for (const f of [`/etc/systemd/system/${u}.timer`, `/etc/systemd/system/${u}.service`]) if (existsSync(f)) unlinkSync(f);
  await run('systemctl', ['daemon-reload']);
  db.prepare('DELETE FROM backups WHERE id=?').run(id);
}

export async function runBackupNow(id: number) {
  const { resolve } = await import('node:path');
  const runner = resolve(import.meta.dirname, 'backup-runner.js');
  const r = await run('/usr/local/bin/node', [runner, String(id)], { timeout: 1800000 });
  return { code: r.code, out: r.out };
}
