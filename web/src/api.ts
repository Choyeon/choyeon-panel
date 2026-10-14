import { ref } from 'vue';
import { clearCache } from './composables/useCachedFetch';
import { beginRequest, endRequest } from './composables/useAppProgress';

const base = '/api';
const TOKEN_KEY = 'cp_token';
const ROLE_KEY = 'cp_role';
const NICK_KEY = 'cp_nickname';
const AVA_KEY = 'cp_avatar';
const USER_KEY = 'cp_username';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}
export function setToken(t: string) {
  t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY);
}
export function getUsername() {
  return localStorage.getItem(USER_KEY) || '';
}
export function getNickname() {
  return localStorage.getItem(NICK_KEY) || '';
}
export function setNickname(n: string) {
  localStorage.setItem(NICK_KEY, n);
}
export function getAvatar() {
  return localStorage.getItem(AVA_KEY) || '';
}
export function setAvatar(a: string) {
  localStorage.setItem(AVA_KEY, a);
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
  localStorage.removeItem(NICK_KEY);
  localStorage.removeItem(AVA_KEY);
  localStorage.removeItem(USER_KEY);
  // 缓存里可能装着刚才那个账号（管理员）拉到的用户表/审计/文件目录，
  // 换会话时必须一起丢掉，否则新登录的只读账号能短暂看见旧数据。
  // 放在这里而不是放在调用方：手动退出曾经只调了 clearSession，
  // 于是"退出→换只读账号登录"后各页直接命中旧缓存，连请求都不发。
  clearCache();
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
  beginRequest();
  let ok = true;
  try {
    const res = await fetch(base + path, { ...init, signal: ctrl.signal });
    // 401 的会话过期由 parse() 统一处理；进度条只对非 2xx 亮红，
    // 否则登录失效那一刻会同时出现"跳登录 + 顶部报错条"两重提示。
    ok = res.ok || res.status === 401;
    return res;
  } catch (e: any) {
    ok = false;
    if (e?.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw new Error('网络错误，无法连接面板');
  } finally {
    clearTimeout(timer);
    endRequest(ok);
  }
}

function authHeaders(extra: Record<string, string> = {}) {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}`, ...extra };
}

async function parse(res: Response) {
  const data = await res.json().catch(() => ({}) as any);
  // 401 的唯一含义是"这个会话已经不被认了"，必须登出——除了 /auth/login：
  // 它的 401 是"这一次用户名或密码错了"，人还登录着（改密走 400，profile/avatar 的
  // 401 只可能是 token 过期）。过去用 `includes('/auth/')` 一刀切，结果编辑资料/上传头像
  // 撞上过期令牌时既不登出也不跳登录，用户卡在假登录态里反复收 401。
  const isExpired = res.status === 401 && !/\/auth\/(login|setup)(\/|$|\?)/.test(String(res.url));
  if (isExpired) notifyUnauthorized();
  // /auth/avatar 的响应体只有 {"detail": ...}（FastAPI 默认字段名，不是面板统一的 error），
  // 不加这条特例界面会显示 "HTTP 413" 这种空话。
  if (!res.ok && res.url.includes('/auth/avatar')) {
    throw new Error((data as any).error || (data as any).detail || `HTTP ${res.status}`);
  }
  if (isExpired) throw new Error('登录已过期，请重新登录');
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
  // 系统补丁：服务端有 30 分钟 TTL，refresh=true 强制回源（apt list 在包多的机器上要几秒）
  updates: (refresh = false) => req(`/updates${refresh ? '?refresh=1' : ''}`),
  // 端口暴露面：服务端 5 分钟 TTL，refresh=true 重新跑一次 ss + 防火墙对账
  ports: (refresh = false) => req(`/ports${refresh ? '?refresh=1' : ''}`),
  // 登录安全：服务端 5 分钟 TTL，refresh=true 重跑 sshd -T + 24h 认证日志 + 公钥扫描（全程只读）
  sshAudit: (refresh = false) => req(`/ssh-audit${refresh ? '?refresh=1' : ''}`),
  storage: (refresh = false) => req(`/storage${refresh ? '?refresh=1' : ''}`),
  // 内存压力：服务端 90 秒 TTL，速率只有 0.8 秒的采样窗口；refresh=1 是重新开一次窗口，不是重新分配内存
  memory: (refresh = false) => req(`/memory${refresh ? '?refresh=1' : ''}`),
  // 负载归因：服务端 60 秒 TTL；refresh=1 是重新开一个 1 秒采样窗口，不是让机器多干活
  load: (refresh = false) => req(`/load${refresh ? '?refresh=1' : ''}`),
  pending: (refresh = false) => req(`/pending${refresh ? '?refresh=1' : ''}`),
  // 一键修复：前端构建项在服务端最长跑 900s，超时会让人误以为失败又点一次
  doctorFix: (id: string) => req('/doctor/fix', { body: { id }, timeout: 960000 }),
  templates: () => req('/templates'),
  setup: (username: string, password: string) => req('/auth/setup', { body: { username, password } }),
  login: (username: string, password: string) => req('/auth/login', { body: { username, password } }),
  changePassword: (oldp: string, newp: string) => req('/auth/password', { body: { old: oldp, new: newp } }),
  // 真登出：让服务端吊销这个账号的 token，而不是只把本地副本删掉
  logout: () => req('/auth/logout', { method: 'POST', body: {} }),
  updateProfile: (nickname: string, avatar: string) => req('/auth/profile', { body: { nickname, avatar } }),
  // 头像上传走原始字节（后端不引 python-multipart），Content-Type 必须覆盖默认的 JSON
  uploadAvatar: (blob: Blob) =>
    request('/auth/avatar', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/octet-stream' }), body: blob }, 60000).then(parse),
  info: () => req('/system/info'),
  stats: () => req('/system/stats'),
  overview: () => req('/system/overview'),
  services: () => req('/system/services'),
  // 开机自启状态：list-units 的输出里没有这一列，只能单独走 list-unit-files（服务端 30s 缓存）
  unitFiles: () => req('/system/unit-files'),
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
  // 一键同步 = fetch + reset + 重启，不跑 install；git fetch 服务端上限 300s，给到 320s
  appSync: (id: number) => req(`/apps/${id}/sync`, { body: {}, timeout: 320000 }),
  deployments: (id: number) => req(`/apps/${id}/deployments`),
  deployment: (id: number, depId: number) => req(`/apps/${id}/deployments/${depId}`),
  appSsl: (id: number, email?: string) => req(`/apps/${id}/ssl`, { body: { email }, timeout: 320000 }),
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
  pgManage: (body: any) => req('/db/pg/manage', { body, timeout: 40000 }),
  // 服务端 psql 自己在 30s 放弃，前端必须比它慢一点：
  // 定成默认 15s 时用户看到的是"请求超时"，而真实结果（错误原因/成功）已经丢了。
  pgQuery: (sql: string, db: string) => req('/db/pg/query', { body: { sql, db }, timeout: 40000 }),
  pgTables: (db: string) => req(`/db/pg/${encodeURIComponent(db)}/tables`),
  pgTable: (db: string, schema: string, name: string, page: number, size: number) =>
    req(`/db/pg/${encodeURIComponent(db)}/table?schema=${encodeURIComponent(schema)}&name=${encodeURIComponent(name)}&page=${page}&size=${size}`),
  // 行写入：服务端 psql 上限 30s，前端给 40s（与 pgQuery 同一套口径）
  pgRow: (db: string, body: any) => req(`/db/pg/${encodeURIComponent(db)}/row`, { body, timeout: 40000 }),
  redis: () => req('/db/redis'),
  redisKeys: (cursor: string, pattern: string) =>
    req(`/db/redis/keys?cursor=${encodeURIComponent(cursor)}&pattern=${encodeURIComponent(pattern)}`),
  redisKey: (k: string) => req(`/db/redis/key?k=${encodeURIComponent(k)}`),
  redisDel: (keys: string[]) => req('/db/redis/del', { body: { keys } }),
  redisSet: (key: string, value: string) => req('/db/redis/set', { body: { key, value } }),
  redisMember: (body: any) => req('/db/redis/member', { body }),
  redisTtl: (key: string, ttl: number) => req('/db/redis/ttl', { body: { key, ttl } }),
  redisPassword: (p: string) => req('/db/redis/password', { body: { password: p } }),
  backups: () => req('/backups'),
  createBackup: (b: any) => req('/backups', { body: b }),
  updateBackup: (id: number, b: any) => req(`/backups/${id}`, { method: 'PATCH', body: b }),
  deleteBackup: (id: number) => req(`/backups/${id}`, { method: 'DELETE' }),
  // 前端超时必须 ≥ 服务端子进程上限，否则用户看到"请求超时"时任务其实还在跑，
  // 重试就会并发（certbot 抢同一张证书、pg_dump 抢同一份磁盘、LE 重复订单限流）。
  // 对应服务端：certbot 申请 300s / renew 600s，psql 30s，backup_runner 1800s。
  runBackup: (id: number) => req(`/backups/${id}/run`, { body: {}, timeout: 1860000 }),
  // 恢复在服务端最长 3600s（pg 的 drop/create/restore），前端超时必须更长，
  // 否则本地 abort 之后远端还在写库，用户会以为没执行又点一次。
  restoreBackup: (id: number, file: string) => req(`/backups/${id}/restore`, { body: { file }, timeout: 3700000 }),
  // 备份产物的平铺列表（{dir, files}）：任务表里只给前 30 个，按时间清理要这一份
  backupFiles: () => req('/backups/files'),
  deleteBackupFile: (id: number, name: string) =>
    req(`/backups/${id}/files/${encodeURIComponent(name)}`, { method: 'DELETE' }),
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

/**
 * 仓库地址 → 可点击的 https 外链。
 * 只认 https（http/ssh 形式交给用户自己处理），并且剥掉 `user:token@` 凭据段：
 * repo_url 允许内嵌访问令牌，原样渲染成 <a href> 会把密钥显示在地址栏与链接标题里。
 */
export function repoHref(url: any): string {
  const s = String(url || '');
  if (!/^https:\/\//i.test(s)) return '';
  return s.replace(/^(https:\/\/)[^/@]*@/i, '$1').replace(/\.git$/i, '');
}

/**
 * 域名 → 站点外链。只接受合法主机名（含点、无空格/斜杠），
 * 域名来自用户输入与 nginx 配置，直接拼进 <a href> 会放行 javascript: 之类的怪值。
 */
export function siteHref(domain: any): string {
  const s = String(domain || '').trim().toLowerCase();
  if (!/^[a-z0-9.-]{1,253}$/.test(s) || !s.includes('.') || s.startsWith('.') || s.endsWith('.')) return '';
  return `https://${s}`;
}

export function saveLoginData(r: { token: string; role: string; username?: string; nickname?: string; avatar?: string }) {
  setToken(r.token);
  setRole(r.role || 'admin');
  if (r.username) localStorage.setItem(USER_KEY, r.username);
  localStorage.setItem(NICK_KEY, r.nickname || '');
  localStorage.setItem(AVA_KEY, r.avatar || '');
}

export function logUrl(path: string, lines?: number) {
  // 省略 lines 就走服务端自己的默认行数（app/util.py 的 LOG_LINES_DEFAULT）。
  // 这个默认值过去在 api.ts、实时日志组件、日志中心各抄了一份，改一处其余两处失真。
  const tail = lines ? `&lines=${lines}` : '';
  return `${base}${path}?token=${encodeURIComponent(getToken())}${tail}`;
}
