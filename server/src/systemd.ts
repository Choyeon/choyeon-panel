import { run } from './util.js';

export interface UnitInfo {
  unit: string;
  load: string;
  active: string;
  sub: string;
  desc: string;
}

export async function listServices(): Promise<UnitInfo[]> {
  const { out } = await run('systemctl', ['list-units', '--type=service', '--all', '--no-legend', '--plain', '--no-pager']);
  return out.split('\n').filter(Boolean).map((line) => {
    const f = line.trim().split(/\s+/);
    return { unit: f[0], load: f[1], active: f[2], sub: f[3], desc: f.slice(4).join(' ') };
  });
}

export async function isActive(unit: string) {
  const { code } = await run('systemctl', ['is-active', unit]);
  return code === 0;
}

const VERBS = new Set(['start', 'stop', 'restart', 'reload', 'enable', 'disable']);

export async function serviceAction(unit: string, verb: string) {
  if (!VERBS.has(verb)) throw new Error(`verb not allowed: ${verb}`);
  const r = await run('systemctl', [verb, unit], { timeout: 60000 });
  if (r.code !== 0) throw new Error(r.out || `systemctl ${verb} failed`);
  return r.out;
}
