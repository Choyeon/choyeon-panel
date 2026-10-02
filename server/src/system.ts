import { readFileSync } from 'node:fs';
import * as os from 'node:os';
import { statfs } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const exec = promisify(execFile);

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

const PY_SCRIPT = fileURLToPath(new URL('../python/sys_helper.py', import.meta.url));
const PYTHON = process.env.CP_PYTHON || 'python3';
// python helper is preferred (per project policy: system ops via Python); fall back to Node parsing
let pythonHealthy = process.platform === 'linux';

interface RawSample {
  idle: number;
  total: number;
  memTotal: number;
  memAvailable: number;
  rx: number;
  tx: number;
  diskTotal: number;
  diskUsed: number;
  load: number[];
  uptime: number;
  source: 'python' | 'node';
}

async function pythonSnapshot(): Promise<RawSample> {
  const { stdout } = await exec(PYTHON, [PY_SCRIPT, 'snapshot'], { timeout: 5000 });
  const j = JSON.parse(stdout);
  if (j.error) throw new Error(j.error);
  return {
    idle: j.idle,
    total: j.total,
    memTotal: j.mem.total,
    memAvailable: j.mem.available,
    rx: j.net.rx,
    tx: j.net.tx,
    diskTotal: j.disk.total,
    diskUsed: j.disk.used,
    load: j.load,
    uptime: j.uptime,
    source: 'python',
  };
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

async function nodeSnapshot(): Promise<RawSample> {
  const idleTotal = (() => {
    const line = readFileSync('/proc/stat', 'utf8').split('\n')[0];
    const parts = line.trim().split(/\s+/).slice(1).map(Number);
    return { idle: parts[3] + parts[4], total: parts.reduce((a, b) => a + b, 0) };
  })();
  const mem = meminfo();
  const net = netBytes();
  const st = await statfs('/');
  const diskTotal = Number(st.blocks) * Number(st.bsize);
  return {
    ...idleTotal,
    memTotal: mem.total,
    memAvailable: mem.available,
    rx: net.rx,
    tx: net.tx,
    diskTotal,
    diskUsed: diskTotal - Number(st.bfree) * Number(st.bsize),
    load: os.loadavg().map((x) => Math.round(x * 100) / 100),
    uptime: os.uptime(),
    source: 'node',
  };
}

async function rawSnapshot(): Promise<RawSample> {
  if (pythonHealthy) {
    try {
      return await pythonSnapshot();
    } catch {
      pythonHealthy = false;
    }
  }
  return nodeSnapshot();
}

export async function snapshot() {
  const now = Date.now();
  const raw = await rawSnapshot();

  const cpu = (() => {
    if (!prevCpu) { prevCpu = { idle: raw.idle, total: raw.total }; return 0; }
    const dt = raw.total - prevCpu.total;
    const di = raw.idle - prevCpu.idle;
    prevCpu = { idle: raw.idle, total: raw.total };
    return dt > 0 ? Math.round((1 - di / dt) * 1000) / 10 : 0;
  })();

  let netRx = 0, netTx = 0;
  if (prevNet && prevAt) {
    const dt = (now - prevAt) / 1000;
    if (dt > 0) {
      netRx = Math.max(0, Math.round((raw.rx - prevNet.rx) / dt));
      netTx = Math.max(0, Math.round((raw.tx - prevNet.tx) / dt));
    }
  }
  prevNet = { rx: raw.rx, tx: raw.tx };
  prevAt = now;

  history.push({ t: now, cpu, memUsed: raw.memTotal - raw.memAvailable, memTotal: raw.memTotal, netRx, netTx });
  if (history.length > 240) history.shift();

  return {
    cpu,
    memUsed: raw.memTotal - raw.memAvailable,
    memTotal: raw.memTotal,
    diskUsed: raw.diskUsed,
    diskTotal: raw.diskTotal,
    netRx,
    netTx,
    load: raw.load,
    uptime: raw.uptime,
    source: raw.source,
    history,
  };
}

export async function info() {
  const cpus = os.cpus();
  const base = {
    hostname: os.hostname(),
    platform: `${os.type()} ${os.release()}`,
    arch: os.arch(),
    cpuModel: cpus[0]?.model?.trim() ?? 'unknown',
    cpuCores: cpus.length,
    node: process.version,
    uptime: os.uptime(),
    prettyName: null as string | null,
  };
  if (pythonHealthy) {
    try {
      const { stdout } = await exec(PYTHON, [PY_SCRIPT, 'info'], { timeout: 5000 });
      const j = JSON.parse(stdout);
      if (!j.error) return { ...base, ...j, prettyName: j.prettyName ?? null };
    } catch {}
  }
  return base;
}
