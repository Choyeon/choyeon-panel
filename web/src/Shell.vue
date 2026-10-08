<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NLayout, NLayoutSider, NLayoutHeader, NLayoutContent, NMenu, NSpace, NText, NButton,
  NModal, NInput, NDropdown, NDrawer, NDrawerContent, NIcon, useMessage,
} from 'naive-ui';
import { setToken, getRole, clearSession, api } from './api';
import { icons } from './icons';
import { TITLES } from './router';

type ThemeMode = 'dark' | 'light' | 'auto';
const props = defineProps<{ themeMode?: ThemeMode }>();
const emit = defineEmits<{ 'update:themeMode': [ThemeMode] }>();

const route = useRoute();
const router = useRouter();
const msg = useMessage();
const collapsed = ref(false);
const showPwd = ref(false);
const oldPwd = ref('');
const newPwd = ref('');
const renewing = ref(false);

const isMobile = ref(false);
const drawer = ref(false);
const isLogin = computed(() => route.path === '/login');
const isAdmin = computed(() => getRole() === 'admin');
const pageTitle = computed(() =>
  route.path.startsWith('/apps/') ? '应用详情' : TITLES[route.path] || 'choyeon panel',
);
const active = computed(() => (route.path.startsWith('/apps') ? '/apps' : route.path));

function ico(name: string, size = 18) {
  return () => h(NIcon, { size }, { default: () => h(icons[name] || icons.SettingsOutline) });
}

function onResize() {
  isMobile.value = window.innerWidth < 900;
  if (!isMobile.value) drawer.value = false;
}
onMounted(() => {
  onResize();
  window.addEventListener('resize', onResize);
});
onBeforeUnmount(() => window.removeEventListener('resize', onResize));

async function renewNow() {
  renewing.value = true;
  try {
    await api.certbotRenew();
    msg.success('certbot 续期检查完成');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    renewing.value = false;
  }
}

const MENU_ICON: Record<string, string> = {
  '/dashboard': 'SpeedometerOutline',
  '/apps': 'ApplicationsOutline',
  '/services': 'ServerOutline',
  '/db': 'CloudOutline',
  '/backups': 'TimeOutline',
  '/files': 'FolderOpenOutline',
  '/terminal': 'TerminalOutline',
  '/doctor': 'ShieldCheckmarkOutline',
  '/settings': 'SettingsOutline',
};

const menuOptions = computed(() => {
  // 文件管理能看到并下载应用目录里的 .env（明文口令），后端已收紧为 admin 专用；
  // 菜单同步隐藏，否则只读账号看得见入口、点进去只收到一句 403。
  const keys = ['/dashboard', '/apps', '/services', '/db', '/backups'];
  if (isAdmin.value) keys.push('/files', '/terminal');
  keys.push('/doctor');
  keys.push('/settings');
  return keys.map((k) => ({ label: TITLES[k], key: k, icon: ico(MENU_ICON[k], 18) }));
});

function go(key: string) {
  drawer.value = false;
  router.push(key);
}

function doLogout() {
  clearSession();
  router.replace('/login');
}

async function doChangePwd() {
  try {
    const r = await api.changePassword(oldPwd.value, newPwd.value);
    if (r?.token) setToken(r.token); // 改密后旧 token 失效，服务端会签发新 token
    oldPwd.value = newPwd.value = '';
    showPwd.value = false;
    msg.success('密码已修改');
  } catch (e: any) {
    msg.error(e.message);
  }
}

const themeIcon = computed(() =>
  props.themeMode === 'light' ? 'SunnyOutline' : props.themeMode === 'dark' ? 'MoonOutline' : 'ColorPaletteOutline',
);
const themeOpts = [
  { label: '跟随系统', key: 'auto' },
  { label: '浅色', key: 'light' },
  { label: '深色', key: 'dark' },
];

const userOpts = [
  { label: '修改密码', key: 'pwd', icon: ico('KeyOutline', 16), onClick: () => (showPwd.value = true) },
  { label: '退出登录', key: 'out', icon: ico('ArrowForwardOutline', 16), onClick: doLogout },
];
</script>

<template>
  <router-view v-if="isLogin" />

  <NLayout v-else class="cp-shell" has-sider>
    <NLayoutSider
      v-if="!isMobile"
      collapse-mode="width"
      :collapsed-width="64"
      :width="208"
      :collapsed="collapsed"
      bordered
      show-trigger="bar"
      :native-scrollbar="false"
      class="cp-sider"
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="logo" :class="{ mini: collapsed }">
        <div class="logo-mark" aria-hidden="true">C</div>
        <span v-if="!collapsed" class="logo-text">choyeon <b>panel</b></span>
      </div>
      <NMenu
        :indent="18"
        :collapsed-icon-size="19"
        :collapsed="collapsed"
        :collapsed-width="64"
        :value="active"
        :options="menuOptions"
        @update:value="(k: string) => go(k)"
      />
    </NLayoutSider>

    <NDrawer v-else v-model:show="drawer" :width="240" placement="left">
      <NDrawerContent :native-scrollbar="false" body-content-style="padding: 0 12px">
        <template #header>
          <div class="logo">
            <div class="logo-mark" aria-hidden="true">C</div>
            <span class="logo-text">choyeon <b>panel</b></span>
          </div>
        </template>
        <NMenu :indent="14" :value="active" :options="menuOptions" @update:value="(k: string) => go(k)" />
      </NDrawerContent>
    </NDrawer>

    <NLayout :content-style="'display:flex;flex-direction:column;height:100vh;overflow:hidden'">
      <NLayoutHeader bordered class="cp-header">
        <NSpace align="center" :size="8" :wrap="false">
          <NButton
            v-if="isMobile"
            quaternary
            size="small"
            title="打开导航菜单"
            aria-label="打开导航菜单"
            :icon="ico('MenuOutline')"
            @click="drawer = true"
          />
          <NText depth="3" class="cp-title">{{ pageTitle }}</NText>
        </NSpace>

        <NSpace align="center" :size="8" :wrap="false">
          <NButton v-if="isAdmin && !isMobile" size="small" tertiary :loading="renewing" @click="renewNow">
            续期证书
          </NButton>

          <NDropdown
            :options="themeOpts"
            trigger="click"
            placement="bottom-end"
            @select="(k: string) => emit('update:themeMode', k as ThemeMode)"
          >
            <NButton quaternary circle size="small" title="外观主题" aria-label="切换外观主题" :icon="ico(themeIcon)" />
          </NDropdown>

          <NDropdown :options="userOpts" trigger="click" placement="bottom-end">
            <button class="user-chip" type="button" :aria-label="`账号菜单，当前：${isAdmin ? '管理员' : '只读'}`">
              <span class="avatar">{{ isAdmin ? 'A' : 'V' }}</span>
              <span v-if="!isMobile" class="user-role">{{ isAdmin ? '管理员' : '只读' }}</span>
            </button>
          </NDropdown>
        </NSpace>
      </NLayoutHeader>

      <NLayoutContent
        content-style="overflow: auto; flex: 1"
        :native-scrollbar="false"
        class="cp-content"
      >
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in"><component :is="Component" /></transition>
        </router-view>
      </NLayoutContent>
    </NLayout>

    <NModal v-model:show="showPwd" preset="card" title="修改密码" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput
          v-model:value="oldPwd"
          type="password"
          placeholder="原密码"
          autocomplete="current-password"
          :input-props="{ 'aria-label': '原密码' }"
          @keyup.enter="doChangePwd"
        />
        <NInput
          v-model:value="newPwd"
          type="password"
          placeholder="新密码（至少 8 位）"
          autocomplete="new-password"
          :input-props="{ 'aria-label': '新密码' }"
          @keyup.enter="doChangePwd"
        />
        <NSpace justify="end" :size="8">
          <NButton tertiary @click="showPwd = false">取消</NButton>
          <NButton type="primary" class="cp-press" :disabled="!oldPwd || newPwd.length < 8" @click="doChangePwd">
            确认修改
          </NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </NLayout>
</template>

<style scoped>
.cp-shell { height: 100vh; }
.cp-sider { background: var(--cp-bg-1); }
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 16px 12px;
  min-height: var(--header-h);
}
.cp-content :deep(.n-layout-scroll-container) { padding: var(--space-5) var(--space-5) var(--space-6); }
@media (max-width: 900px) {
  .cp-content :deep(.n-layout-scroll-container) { padding: var(--space-4); }
}
@media (max-width: 640px) {
  .cp-content :deep(.n-layout-scroll-container) { padding: var(--space-3); }
  .cp-title { font-size: var(--fs-sm); font-weight: 600; }
}
.logo-mark {
  width: 28px;
  height: 28px;
  border-radius: var(--radius);
  background: var(--gradient-brand);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  flex-shrink: 0;
  box-shadow: var(--shadow-glow);
}
.logo-text {
  font-size: 14.5px;
  color: var(--cp-text);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.logo-text b { color: var(--cp-brand-soft); }
.cp-header {
  height: var(--header-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  gap: 10px;
}
.cp-title { font-size: 12.5px; letter-spacing: 0.02em; }
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px 4px 4px;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  background: transparent;
  color: var(--cp-text-dim);
  cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.user-chip:hover { background: var(--cp-hover); border-color: var(--cp-border); }
.avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--gradient-brand);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-role { font-size: var(--fs-sm); }
</style>
