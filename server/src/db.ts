import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { randomBytes } from 'node:crypto';

const dir = process.env.CP_DATA_DIR || new URL('../../data', import.meta.url).pathname;
mkdirSync(dir, { recursive: true });

export const db = new Database(`${dir}/panel.db`);
db.pragma('journal_mode = WAL');

db.exec(`
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pass_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('node','python')),
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  path TEXT NOT NULL,
  port INTEGER,
  domain TEXT,
  install_cmd TEXT,
  start_cmd TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT '[]',
  unit_override TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  log TEXT NOT NULL DEFAULT '',
  started_at TEXT DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS backups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('pg','app')),
  target TEXT NOT NULL,
  schedule TEXT NOT NULL DEFAULT 'daily',
  hour INTEGER NOT NULL DEFAULT 3,
  minute INTEGER NOT NULL DEFAULT 30,
  keep INTEGER NOT NULL DEFAULT 7,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
`);

const cols = (db.pragma('table_info(users)') as { name: string }[]).map((c) => c.name);
if (!cols.includes('role')) db.exec(`ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'`);
const acols = (db.pragma('table_info(apps)') as { name: string }[]).map((c) => c.name);
if (!acols.includes('db_names')) db.exec(`ALTER TABLE apps ADD COLUMN db_names TEXT`);
if (!acols.includes('unit_template')) db.exec(`ALTER TABLE apps ADD COLUMN unit_template TEXT`);

export function getSetting(key: string): string | null {
  const row = db.prepare('SELECT value FROM settings WHERE key=?').get(key) as { value: string } | undefined;
  return row?.value ?? null;
}

export function setSetting(key: string, value: string) {
  db.prepare('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, value);
}

export function jwtSecret() {
  let s = getSetting('jwt_secret');
  if (!s) {
    s = randomBytes(32).toString('hex');
    setSetting('jwt_secret', s);
  }
  return s;
}

export function audit(username: string | undefined | null, action: string, detail?: string) {
  db.prepare('INSERT INTO audit(username,action,detail) VALUES(?,?,?)').run(username ?? null, action, detail ?? null);
}
