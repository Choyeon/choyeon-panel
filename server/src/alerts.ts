import { statfs } from 'node:fs/promises';
import * as os from 'node:os';
import { run } from './util.js';
import { getSetting, setSetting } from './db.js';

function now() {
  return new Date().toISOString().slice(0, 10);
}

async function notify(text: string) {
  const bot = getSetting('alert_telegram_bot');
  const chat = getSetting('alert_telegram_chat');
  const hook = getSetting('alert_webhook_url');
  if (bot && chat) {
    try {
      await fetch(`https://api.telegram.org/bot${bot}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chat, text: `[choyeon-panel] ${text}` }),
      });
    } catch (e: any) {
      console.error('telegram notify failed:', e.message);
    }
  }
  if (hook) {
    try {
      await fetch(hook, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    } catch (e: any) {
      console.error('webhook notify failed:', e.message);
    }
  }
}

async function check(key: string, trigger: boolean, message: string) {
  if (!trigger) return;
  if (getSetting('sent:' + key) === now()) return;
  setSetting('sent:' + key, now());
  await notify(message);
}

export async function runChecks() {
  const st = await statfs('/');
  const total = Number(st.blocks) * Number(st.bsize);
  const usedPct = Math.round(((total - Number(st.bfree) * Number(st.bsize)) / total) * 100);
  const diskPct = Number(getSetting('alert_disk_pct') || 85);
  await check(`disk-${Math.floor(usedPct / 5)}`, usedPct >= diskPct, `磁盘使用率 ${usedPct}%（阈值 ${diskPct}%）于 ${os.hostname()}`);

  const failed = await run('systemctl', ['list-units', '--state=failed', '--no-legend', '--plain']);
  const units = failed.out.split('\n').filter(Boolean).map((l) => l.trim().split(/\s+/)[0]);
  await check('failed-units', units.length > 0, `存在 failed 状态服务: ${units.join(', ')}`);

  const sslDays = Number(getSetting('alert_ssl_days') || 14);
  if (sslDays > 0) {
    const certs = await run('certbot', ['certificates']);
    const re = /Certificate Name: (\S+)[\s\S]*?Expiry Date: [^\n]*?(\(invalid\)|[^)]*)/g;
    const blocks = certs.out.split('-------------------------------------------------------------------------------').filter((b) => b.includes('Certificate Name'));
    for (const b of blocks) {
      const name = b.match(/Certificate Name: (\S+)/)?.[1];
      const days = b.match(/Days Left: (\d+)/)?.[1];
      if (!name) continue;
      const invalid = /EXPIRED|invalid/i.test(b);
      const low = days != null && Number(days) <= sslDays;
      await check(`ssl-${name}`, invalid || low, invalid ? `证书 ${name} 已过期或无效！` : `证书 ${name} 将在 ${days} 天后到期（阈值 ${sslDays} 天）`);
    }
  }
}

export async function notifyTest() {
  await notify(`测试通知：来自 ${os.hostname()} 的 choyeon panel，通道工作正常 ✅`);
}

let timer: NodeJS.Timeout | null = null;
export function startChecker() {
  if (timer) return;
  setTimeout(() => void runChecks(), 60_000);
  timer = setInterval(() => void runChecks(), 30 * 60 * 1000);
}
