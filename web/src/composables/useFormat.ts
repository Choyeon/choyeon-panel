/**
 * 全站格式化的唯一出处。此前 Files / Backups / BackupFiles / Dashboard 各写了一份
 * 字节格式化，日期与百分比又是散落的 `replace('T',' ').slice(0,19)` / `Math.round(...)`，
 * 单位与精度极易走偏。这里的输出与原来各页保持一致（含 1 位小数、`B/KB/MB/GB/TB`）。
 */

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

/** 十进制字节：0 → `0 B`，1536 → `1.5 KB`；非数字/负数/null（探测没读回来）→ `—` */
export function fmtBytes(n: number | null | undefined): string {
  // null 必须先挡住：Number(null) 是 0，会让「systemd 没报 MemoryCurrent」「异地大小读不到」
  // 这些没值的地方画成「0 B」，看着像一个真的用量。
  if (n === null || n === undefined) return '—';
  let v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '—';
  let i = 0;
  while (v >= 1024 && i < BYTE_UNITS.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${BYTE_UNITS[i]}`;
}

/** 文件列表用：目录不显示大小 */
export function fmtSize(n: number | null | undefined, isDir?: boolean): string {
  return isDir ? '—' : fmtBytes(n);
}

/**
 * `2026-10-05T12:34:56.789Z` → `2026-10-05 12:34:56`。
 * 后端两种时间串都出现过（SQLite 的 `datetime('now')` 是空格分隔且无时区，
 * 文件 mtime 是带毫秒的 ISO），这里只做展示截断，不做时区换算——
 * 面板里这些值统一按 UTC 展示，表头也已经写明 UTC，改成 localtime 会和审计对不上。
 */
export function fmtDateTime(v: unknown): string {
  const s = String(v ?? '');
  if (!s) return '—';
  return s.replace('T', ' ').slice(0, 19);
}

/** 只到日期：`2026-10-05` */
export function fmtDate(v: unknown): string {
  return String(v ?? '').slice(0, 10) || '—';
}

/** ISO / SQLite 空格格式都按 UTC 解析；无法解析返回 NaN */
export function toTime(v: unknown): number {
  const s = String(v ?? '').trim();
  if (!s) return NaN;
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/.test(s);
  const norm = s.replace(' ', 'T') + (hasTz ? '' : 'Z');
  const t = Date.parse(norm);
  return Number.isFinite(t) ? t : Date.parse(s);
}

/** 相对时间：列表里"多久之前"比整串时间戳好读；超过 7 天回落到绝对时间 */
export function fmtAgo(v: unknown, now = Date.now()): string {
  const t = toTime(v);
  if (!Number.isFinite(t)) return fmtDateTime(v);
  const sec = Math.max(0, Math.round((now - t) / 1000));
  if (sec < 45) return '刚刚';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day} 天前`;
  return fmtDateTime(v);
}

/** 占比取整（0-100 的数值，不带百分号）：分母为 0 时给 0 而不是 NaN */
export function pctOf(part: number, total: number): number {
  const p = Number(part);
  const t = Number(total);
  if (!Number.isFinite(p) || !Number.isFinite(t) || t <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((p / t) * 100)));
}

/** `0.953` → `95%`；已是百分数值时直接取整 */
export function fmtPct(v: number | null | undefined, digits = 0): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${n.toFixed(digits)}%`;
}

/** 保留位数的普通数字（负载、命中率之类） */
export function fmtNum(v: number | null | undefined, digits = 1): string {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : '—';
}

/** 时长：秒 → `2 天 3 小时` / `5 分钟`；uptime 这类字段用 */
export function fmtDuration(sec: number | null | undefined): string {
  const s = Math.floor(Number(sec));
  if (!Number.isFinite(s) || s < 0) return '—';
  if (s < 60) return `${s} 秒`;
  const u: [number, string][] = [[86400, '天'], [3600, '小时'], [60, '分钟']];
  const parts: string[] = [];
  let rest = s;
  for (const [unit, label] of u) {
    const q = Math.floor(rest / unit);
    if (q > 0) {
      parts.push(`${q} ${label}`);
      rest -= q * unit;
    }
    if (parts.length === 2) break;
  }
  return parts.join(' ') || '0 分钟';
}

export const format = { fmtBytes, fmtSize, fmtDateTime, fmtDate, fmtAgo, pctOf, fmtPct, fmtNum, fmtDuration };
