import { createRouter, createWebHashHistory } from 'vue-router';
import { getToken } from './api';

export const TITLES: Record<string, string> = {
  '/dashboard': '概览', '/apps': '应用', '/services': '服务', '/db': '数据库',
  '/backups': '备份', '/files': '文件', '/terminal': '终端', '/settings': '设置',
};

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: () => import('./views/Login.vue') },
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: () => import('./views/Dashboard.vue') },
    { path: '/apps', component: () => import('./views/Apps.vue') },
    { path: '/apps/:id', component: () => import('./views/AppDetail.vue') },
    { path: '/services', component: () => import('./views/Services.vue') },
    { path: '/db', component: () => import('./views/Databases.vue') },
    { path: '/backups', component: () => import('./views/Backups.vue') },
    { path: '/files', component: () => import('./views/Files.vue') },
    { path: '/terminal', component: () => import('./views/Terminal.vue') },
    { path: '/settings', component: () => import('./views/Settings.vue') },
  ],
});

router.beforeEach((to) => {
  if (to.path !== '/login' && !getToken()) return '/login';
});

router.afterEach((to) => {
  const t = to.path === '/login' ? '登录' : TITLES[to.path.startsWith('/apps/') ? '/apps' : to.path] || '概览';
  document.title = `${t} · choyeon panel`;
});
