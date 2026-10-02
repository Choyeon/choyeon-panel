import { readFileSync } from 'node:fs';
import * as os from 'node:os';
import { statfs } from 'node:fs/promises';

export interface Sample {
  t: number;
  cpu: number;
  memUsed: number;
  memTotal: number;
  netRx: number;
  netTx: number;
}

const history: Sample[] = [];

let prevCpu: { idle: number; total: number } | null = null;
let prevNet: { rx: number; tx: number } | null = null;
let prevAt = 0;

function cpuPercent() {
  const line = readFileSync('/proc/stat', 'utf8').split('\n')[0];
  const parts = line.trim().split(/\s+/).slice(1).map(Number);
  const idle = parts[3] + parts[4];
  const total = parts.reduce((a, b) => a + b, 0);
  if (!prevCpu) { prevCpu = { idle, total }; return 0; }
  const dt = total - prevCpu.total;
  const di = idle - prevCpu.idle;
  prevCpu = { idle, total };
  return dt > 0 ? Math.round((1 - di / dt) * 1000) / 10 : 0;
}

function netBytes() {
  const lines = readFileSync('/proc/net/dev', 'utf8').split('\n').slice(2);
  let rx = 0, tx = 0;
  for (const l of lines) {
    const [name, rest] = l.split(':');
    if (!rest || name.trim() === 'lo') continue;
    const f = rest.trim().split(/\s+/).map(Number);
    rx += f[0]; tx += f[8];
  }
  return { rx, tx };
}

function meminfo() {
  const m: Record<string, number> = {};
  for (const l of readFileSync('/proc/meminfo', 'utf8').split('\n')) {
    const [k, v] = l.split(':');
    m[k.trim()] = parseInt(v, 10) * 1024;
  }
  return { total: m.MemTotal, available: m.MemAvailable };
}

export async function snapshot() {
  const now = Date.now();
  const cpu = cpuPercent();
  const mem = meminfo();
  const net = netBytes();
  let netRx = 0, netTx = 0;
  if (prevNet && prevAt) {
    const dt = (now - prevAt) / 1000;
    if (dt > 0) {
      netRx = Math.max(0, Math.round((net.rx - prevNet.rx) / dt));
      netTx = Math.max(0, Math.round((net.tx - prevNet.tx) / dt));
    }
  }
  prevNet = net; prevAt = now;
  history.push({ t: now, cpu, memUsed: mem.total - mem.available, memTotal: mem.total, netRx, netTx });
  if (history.length > 240) history.shift();

  const st = await statfs('/');
  const diskTotal = Number(st.blocks) * Number(st.bsize);
  const diskUsed = diskTotal - Number(st.bfree) * Number(st.bsize);
  return {
    cpu,
    memUsed: mem.total - mem.available,
    memTotal: mem.total,
    diskUsed,
    diskTotal,
    netRx,
    netTx,
    load: os.loadavg().map((x) => Math.round(x * 100) / 100),
    uptime: os.uptime(),
    history,
  };
}

export function info() {
  const cpus = os.cpus();
  return {
    hostname: os.hostname(),
    platform: `${os.type()} ${os.release()}`,
    arch: os.arch(),
    cpuModel: cpus[0]?.model?.trim() ?? 'unknown',
    cpuCores: cpus.length,
    node: process.version,
    uptime: os.uptime(),
  };
}
