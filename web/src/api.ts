import { ref } from 'vue';

const base = '/api';
const TOKEN_KEY = 'cp_token';
const ROLE_KEY = 'cp_role';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}
export function setToken(t: string) {
  t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY);
}
/**
 * 当前角色，未登录时按最小权限处理（只读），避免本地存储缺失时误判为管理员。
 * 必须是响应式的：Shell 只挂载一次，登录/登出/被降权都不会重跑它的 setup。
 * 用 localStorage 直读的话，切换账号后侧栏仍停留在旧角色的菜单项上
 * （只读账号看得见「终端」「文件」，点进去才收 403），必须刷新页面才对。
 */
const roleRef = ref(localStorage.getItem(ROLE_KEY) || 'viewer');

export function getRole() {
  return roleRef.value;
}
export function setRole(r: string) {
  roleRef.value = r;
  localStorage.setItem(ROLE_KEY, r);
}
export function clearSession() {
  roleRef.value = 'viewer';
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ROLE_KEY);
}

export const onUnauthorized: Array<() => void> = [];
let redirecting = false;

function notifyUnauthorized() {
  clearSession();
  if (redirecting) return;
  redirecting = true;
  onUnauthorized.forEach((fn) => fn());
  setTimeout(() => (redirecting = false), 500);
}

async function request(path: string, init: RequestInit, timeoutMs = 15000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(base + path, { ...init, signal: ctrl.signal });
  } catch (e: any) {
    if (e?.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw new Error('网络错误，无法连接面板');
  } finally {
    clearTimeout(timer);
  }
}

function authHeaders(extra: Record<string, string> = {}) {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}`, ...extra };
}

async function parse(res: Response) {
  const data = await res.json().catch(() => ({}) as any);
  if (res.status === 401 && !String(res.url).includes('/auth/')) notifyUnauthorized();
  if (!res.ok) throw new Error((data as any).error || `HTTP ${res.status}`);
  return data as any;
}

export async function req(path: string, opts: { method?: string; body?: any; timeout?: number } = {}) {
  const res = await request(
    path,
    {
      method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'),
      headers: authHeaders(),
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    },
    opts.timeout,
  );
  return parse(res);
}

export const api = {
  status: () => req('/auth/status'),
  ready: () => req('/ready'),
  doctor: () => req('/doctor'),
  templates: () => req('/templates'),
  setup: (username: string, password: string) => req('/auth/setup', { body: { username, password } }),
  login: (username: string, password: string) => req('/auth/login', { body: { username, password } }),
  changePassword: (oldp: string, newp: string) => req('/auth/password', { body: { old: oldp, new: newp } }),
  info: () => req('/system/info'),
  stats: () => req('/system/stats'),
  services: () => req('/system/services'),
  serviceAction: (unit: string, verb: string) => req(`/system/services/${encodeURIComponent(unit)}`, { body: { verb } }),
  // certbot 续期在服务器上最长可跑到几分钟，前端 15s 就 abort 会显示"请求超时"，
  // 而续期其实还在跑：用户重试 → 多个 certbot 并发抢同一张证书。给到 620s（略大于服务端上限）。
  certbotRenew: () => req('/system/certbot-renew', { body: {}, timeout: 620000 }),
  apps: () => req('/apps'),
  app: (id: number) => req(`/apps/${id}`),
  createApp: (a: any) => req('/apps', { body: a }),
  updateApp: (id: number, a: any) => req(`/apps/${id}`, { method: 'PATCH', body: a }),
  deleteApp: (id: number, purge: boolean) => req(`/apps/${id}${purge ? '?purge=1' : ''}`, { method: 'DELETE' }),
  appAction: (id: number, verb: string) => req(`/apps/${id}/action`, { body: { verb } }),
  deploy: (id: number) => req(`/apps/${id}/deploy`, { body: {} }),
  deployments: (id: number) => req(`/apps/${id}/deployments`),
  deployment: (id: number, depId: number) => req(`/apps/${id}/deployments/${depId}`),
  appSsl: (id: number, email?: string) => req(`/apps/${id}/ssl`, { body: { email } }),
  appNginx: (id: number) => req(`/apps/${id}/nginx`),
  saveNginx: (id: number, file: string, content: string) => req(`/apps/${id}/nginx`, { method: 'PUT', body: { file, content } }),
  quickNginx: (id: number, file: string, kind: string, value?: string) => req(`/apps/${id}/nginx/quick`, { body: { file, kind, value } }),
  appUnit: (id: number) => req(`/apps/${id}/unit`),
  saveUnit: (id: number, content: string) => req(`/apps/${id}/unit`, { method: 'PUT', body: { content } }),
  appProc: (id: number) => req(`/apps/${id}/proc`),
  audit: (limit = 100) => req(`/audit?limit=${limit}`),
  users: () => req('/users'),
  createUser: (username: string, password: string, role: string) => req('/users', { body: { username, password, role } }),
  updateUser: (id: number, body: any) => req(`/users/${id}`, { method: 'PATCH', body }),
  deleteUser: (id: number) => req(`/users/${id}`, { method: 'DELETE' }),
  alertSettings: () => req('/settings/alerts'),
  saveAlerts: (body: any) => req('/settings/alerts', { body }),
  alertTest: () => req('/settings/alerts/test', { body: {}, timeout: 25000 }),
  alertRunChecks: () => req('/settings/alerts/run-checks', { body: {}, timeout: 60000 }),
  firewall: () => req('/firewall'),
  pgDbs: () => req('/db/pg/databases'),
  pgRoles: () => req('/db/pg/roles'),
  pgManage: (body: any) => req('/db/pg/manage', { body }),
  // 服务端 psql 自己在 30s 放弃，前端必须比它慢一点：
  // 定成默认 15s 时用户看到的是"请求超时"，而真实结果（错误原因/成功）已经丢了。
  pgQuery: (sql: string) => req('/db/pg/query', { body: { sql }, timeout: 40000 }),
  redis: () => req('/db/redis'),
  redisPassword: (p: string) => req('/db/redis/password', { body: { password: p } }),
  backups: () => req('/backups'),
  createBackup: (b: any) => req('/backups', { body: b }),
  updateBackup: (id: number, b: any) => req(`/backups/${id}`, { method: 'PATCH', body: b }),
  deleteBackup: (id: number) => req(`/backups/${id}`, { method: 'DELETE' }),
  runBackup: (id: number) => req(`/backups/${id}/run`, { body: {}, timeout: 600000 }),
  files: (path: string) => req(`/files?path=${encodeURIComponent(path)}`),
  fileContent: (path: string) => req(`/files/content?path=${encodeURIComponent(path)}`),
  saveFile: (path: string, content: string | ArrayBuffer) =>
    request(
      `/files/content?path=${encodeURIComponent(path)}`,
      {
        method: 'PUT',
        headers: authHeaders({ 'Content-Type': 'application/octet-stream' }),
        body: content,
      },
      60000,
    ).then(parse),
  fileAction: (action: string, path: string, path2?: string) => req('/files/action', { body: { action, path, path2 } }),
};

export function apiUrl(path: string) {
  return `${base}${path}`;
}

export function logUrl(path: string, lines = 200) {
  return `${base}${path}?token=${encodeURIComponent(getToken())}&lines=${lines}`;
}
