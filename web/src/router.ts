import { createRouter, createWebHashHistory } from 'vue-router';
import { getToken, getRole, onUnauthorized } from './api';

export const TITLES: Record<string, string> = {
  '/dashboard': '总览', '/apps': '应用', '/services': '系统服务', '/db': '数据库',
  '/backups': '备份', '/files': '文件', '/terminal': '终端', '/doctor': '安全自检', '/settings': '设置',
};

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: () => import('./views/Login.vue'), meta: { title: '登录', public: true } },
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: () => import('./views/Dashboard.vue') },
    { path: '/apps', component: () => import('./views/Apps.vue') },
    { path: '/apps/:id', component: () => import('./views/AppDetail.vue'), meta: { title: '应用详情' } },
    { path: '/services', component: () => import('./views/Services.vue') },
    { path: '/db', component: () => import('./views/Databases.vue') },
    { path: '/backups', component: () => import('./views/Backups.vue') },
    { path: '/files', component: () => import('./views/Files.vue') },
    { path: '/terminal', component: () => import('./views/Terminal.vue') },
    { path: '/doctor', component: () => import('./views/Doctor.vue') },
    { path: '/settings', component: () => import('./views/Settings.vue') },
    { path: '/:pathMatch(.*)*', component: () => import('./views/NotFound.vue'), meta: { title: '页面不存在' } },
  ],
});

// 只有管理员能看到/用得上的页面：菜单里已隐藏，但地址栏直达同样要拦，
// 否则只读账号会停在一个只会报 403 的空页面上。
const ADMIN_ONLY = ['/files', '/terminal', '/doctor'];

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
