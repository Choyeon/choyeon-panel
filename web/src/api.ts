const base = '/api';
export function getToken() {
  return localStorage.getItem('cp_token') || '';
}
export function setToken(t: string) {
  t ? localStorage.setItem('cp_token', t) : localStorage.removeItem('cp_token');
}
export function getRole() {
  return localStorage.getItem('cp_role') || 'admin';
}
export function setRole(r: string) {
  localStorage.setItem('cp_role', r);
}

export async function req(path: string, opts: { method?: string; body?: any } = {}) {
  const res = await fetch(base + path, {
    method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'),
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith('/auth/')) {
    setToken('');
    location.hash = '#/login';
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export const api = {
  status: () => req('/auth/status'),
  setup: (username: string, password: string) => req('/auth/setup', { body: { username, password } }),
  login: (username: string, password: string) => req('/auth/login', { body: { username, password } }),
  changePassword: (oldp: string, newp: string) => req('/auth/password', { body: { old: oldp, new: newp } }),
  info: () => req('/system/info'),
  stats: () => req('/system/stats'),
  services: () => req('/system/services'),
  serviceAction: (unit: string, verb: string) => req(`/system/services/${encodeURIComponent(unit)}`, { body: { verb } }),
  certbotRenew: () => req('/system/certbot-renew', { body: {} }),
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
  // phase 4
  users: () => req('/users'),
  createUser: (username: string, password: string, role: string) => req('/users', { body: { username, password, role } }),
  updateUser: (id: number, body: any) => req(`/users/${id}`, { method: 'PATCH', body }),
  deleteUser: (id: number) => req(`/users/${id}`, { method: 'DELETE' }),
  alertSettings: () => req('/settings/alerts'),
  saveAlerts: (body: any) => req('/settings/alerts', { body }),
  alertTest: () => req('/settings/alerts/test', { body: {} }),
  alertRunChecks: () => req('/settings/alerts/run-checks', { body: {} }),
  firewall: () => req('/firewall'),
  // phase 3
  pgDbs: () => req('/db/pg/databases'),
  pgRoles: () => req('/db/pg/roles'),
  pgManage: (body: any) => req('/db/pg/manage', { body }),
  pgQuery: (sql: string) => req('/db/pg/query', { body: { sql } }),
  redis: () => req('/db/redis'),
  redisPassword: (p: string) => req('/db/redis/password', { body: { password: p } }),
  backups: () => req('/backups'),
  createBackup: (b: any) => req('/backups', { body: b }),
  updateBackup: (id: number, b: any) => req(`/backups/${id}`, { method: 'PATCH', body: b }),
  deleteBackup: (id: number) => req(`/backups/${id}`, { method: 'DELETE' }),
  runBackup: (id: number) => req(`/backups/${id}/run`, { body: {} }),
  files: (path: string) => req(`/files?path=${encodeURIComponent(path)}`),
  fileContent: (path: string) => req(`/files/content?path=${encodeURIComponent(path)}`),
  saveFile: (path: string, content: string) =>
    fetch(`/api/files/content?path=${encodeURIComponent(path)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream', Authorization: `Bearer ${getToken()}` },
      body: content,
    }).then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((d as any).error || '保存失败');
      return d;
    }),
  fileAction: (action: string, path: string, path2?: string) => req('/files/action', { body: { action, path, path2 } }),
};

export function logUrl(path: string, lines = 200) {
  return `${base}${path}?token=${encodeURIComponent(getToken())}&lines=${lines}`;
}
