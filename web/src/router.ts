import { createRouter, createWebHashHistory } from 'vue-router';
import { getToken, getRole, onUnauthorized } from './api';
import { beginChunk, endChunk, chunkFailed } from './composables/useAppProgress';

/**
 * 懒加载包一层 chunk 计数：进度条只看得见在飞的 API 请求，而"点侧栏卡一下"
 * 主要发生在页面 chunk 下载的那段——那段时间一个请求都没有。
 * useAppProgress 一直导出着 beginChunk/endChunk/chunkFailed 却零调用，
 * 头注释宣称的"请求数 + chunk 数"实际只剩一半，这里把它接上。
 */
function lazy(loader: () => Promise<any>) {
  return () => {
    beginChunk();
    return loader().then(
      (m) => {
        endChunk();
        return m;
      },
      (e) => {
        chunkFailed();
        return Promise.reject(e);
      },
    );
  };
}

export const TITLES: Record<string, string> = {
  '/dashboard': '总览', '/apps': '应用', '/services': '系统服务', '/db': '数据库', '/sites': '网站与证书',
  '/backups': '备份', '/backups/files': '备份文件', '/alerts': '告警中心', '/cron': '定时任务',
  '/files': '文件', '/terminal': '终端', '/doctor': '安全自检', '/firewall': '防火墙',
  '/users': '用户管理', '/audit': '操作审计', '/logs': '面板日志', '/logsearch': '日志中心', '/settings': '设置',
};

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: lazy(() => import('./views/Login.vue')), meta: { title: '登录', public: true } },
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: lazy(() => import('./views/Dashboard.vue')) },
    { path: '/apps', component: lazy(() => import('./views/Apps.vue')) },
    { path: '/apps/:id', component: lazy(() => import('./views/AppDetail.vue')), meta: { title: '应用详情' } },
    { path: '/services', component: lazy(() => import('./views/Services.vue')) },
    { path: '/db', component: lazy(() => import('./views/Databases.vue')) },
    { path: '/sites', component: lazy(() => import('./views/Sites.vue')) },
    { path: '/backups', component: lazy(() => import('./views/Backups.vue')) },
    { path: '/backups/files', component: lazy(() => import('./views/BackupFiles.vue')) },
    { path: '/alerts', component: lazy(() => import('./views/Alerts.vue')) },
    { path: '/cron', component: lazy(() => import('./views/CronTasks.vue')) },
    { path: '/files', component: lazy(() => import('./views/Files.vue')) },
    { path: '/firewall', component: lazy(() => import('./views/Firewall.vue')) },
    { path: '/terminal', component: lazy(() => import('./views/Terminal.vue')) },
    { path: '/doctor', component: lazy(() => import('./views/Doctor.vue')) },
    { path: '/users', component: lazy(() => import('./views/Users.vue')) },
    { path: '/audit', component: lazy(() => import('./views/Audit.vue')) },
    { path: '/logs', component: lazy(() => import('./views/PanelLogs.vue')) },
    { path: '/logsearch', component: lazy(() => import('./views/LogCenter.vue')) },
    { path: '/settings', component: lazy(() => import('./views/Settings.vue')) },
    { path: '/:pathMatch(.*)*', component: lazy(() => import('./views/NotFound.vue')), meta: { title: '页面不存在' } },
  ],
});

// 只有管理员能看到/用得上的页面：菜单里已隐藏，但地址栏直达同样要拦，
// 否则只读账号会停在一个只会报 403 的空页面上。
// cron 与 logsearch 的每一个端点（连列表和候选项）在服务端都是 need_admin，
// 少了这一条，只读账号会停在一片"加载失败"的空表格上。
// logs（面板日志）走 /api/system/services/{unit}/logs：journalctl 是全机内容，
// 同样按 admin 收口，所以这一页对只读账号也不该留。
// db（数据浏览）能翻出任意 PG 表的任意行、任意 Redis 键值——会话 token 就存在里面，
// 后端整块路由已挂 require_admin，少了这一条，只读账号会停在一个请求全 403 的空页面上。
export const ADMIN_ONLY = ['/files', '/terminal', '/doctor', '/firewall', '/users', '/audit', '/cron', '/logsearch', '/logs', '/db'];

router.beforeEach((to) => {
  if (to.path !== '/login' && !getToken()) return '/login';
  if (ADMIN_ONLY.includes(to.path) && getRole() !== 'admin') return '/dashboard';
});

router.afterEach((to) => {
  const key = to.path.startsWith('/apps/') ? '/apps' : to.path;
  const t = (to.meta.title as string) || TITLES[key] || 'choyeon panel';
  document.title = `${t} · choyeon panel`;
});

// token 失效由 api 层统一发现，跳转交给路由，避免直接改 location 造成状态不一致
onUnauthorized.push(() => {
  if (router.currentRoute.value.path !== '/login') router.replace('/login');
});

// 发版后旧标签页里引用的 chunk 已被删除，动态 import 会失败（404 / 拿到 JSON）。
// 认出来就整页重载一次拿新的 index.html；sessionStorage 记住这条错误，
// 避免真·故障（比如断网）时陷入重载死循环。
router.onError((err) => {
  const msg = String((err as Error)?.message || err);
  if (!/dynamically imported module|Unable to preload CSS|Importing a module script failed/i.test(msg)) return;
  if (sessionStorage.getItem('cp-chunk-reload') === msg) return;
  sessionStorage.setItem('cp-chunk-reload', msg);
  location.reload();
});
