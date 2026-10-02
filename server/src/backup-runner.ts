import { spawn } from 'node:child_process';
import { mkdirSync, readdirSync, unlinkSync } from 'node:fs';
import { db, audit } from './db.js';
import { BACKUP_DIR } from './backups.js';

const id = Number(process.argv[2]);
if (!id) {
  console.error('usage: backup-runner <backupId>');
  process.exit(2);
}

const row = db.prepare('SELECT * FROM backups WHERE id=?').get(id) as
  | { id: number; kind: string; target: string; keep: number }
  | undefined;
if (!row) {
  console.error('backup not found');
  process.exit(2);
}
const bk = row;
mkdirSync(BACKUP_DIR, { recursive: true });
const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 16);

function sh(cmd: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn('/bin/sh', ['-c', cmd], { stdio: ['ignore', 'ignore', 'inherit'] });
    child.on('close', (code) => (code === 0 ? resolve() : reject(new Error(`exit ${code}`))));
  });
}

async function main() {
  if (bk.kind === 'pg') {
    if (bk.target === 'all') {
      const file = `${BACKUP_DIR}/bk${bk.id}-${ts}.sql.gz`;
      await sh(`su -s /bin/sh postgres -c 'pg_dumpall' | gzip > '${file}'`);
    } else {
      if (!/^[a-z_][a-z0-9_]{0,62}$/.test(bk.target)) throw new Error('库名不合法');
      const file = `${BACKUP_DIR}/bk${bk.id}-${ts}.dump.gz`;
      await sh(`su -s /bin/sh postgres -c 'pg_dump -Fc ${bk.target}' | gzip > '${file}'`);
    }
    audit('system', 'backup:pg', bk.target);
  } else if (bk.kind === 'app') {
    const app = db.prepare('SELECT path FROM apps WHERE name=?').get(bk.target) as { path: string } | undefined;
    if (!app) throw new Error(`应用 ${bk.target} 不存在`);
    const file = `${BACKUP_DIR}/bk${bk.id}-${ts}.tar.gz`;
    await sh(
      `tar czf '${file}' --exclude=node_modules --exclude=.venv --exclude=.next --exclude=.output -C '${app.path}' .`,
    );
    audit('system', 'backup:app', bk.target);
  }
  prune();
  console.log('backup ok');
}

function prune() {
  const files = readdirSync(BACKUP_DIR)
    .filter((f) => f.startsWith(`bk${bk.id}-`))
    .sort();
  while (files.length > bk.keep) {
    const f = files.shift()!;
    try {
      unlinkSync(`${BACKUP_DIR}/${f}`);
      console.log(`pruned ${f}`);
    } catch {}
  }
}

main().catch((e) => {
  console.error(`backup failed: ${e.message}`);
  process.exit(1);
});
