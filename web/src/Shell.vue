<script setup lang="ts">
import { computed, h, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NLayout, NLayoutSider, NLayoutHeader, NLayoutContent, NMenu, NSpace, NText, NButton,
  NModal, NInput, NDropdown, useMessage,
} from 'naive-ui';
import { setToken, getRole, api } from './api';
import { icon } from './icons';
import { TITLES } from './router';

const route = useRoute();
const router = useRouter();
const msg = useMessage();
const collapsed = ref(false);
const showPwd = ref(false);
const oldPwd = ref('');
const newPwd = ref('');

const isLogin = computed(() => route.path === '/login');
const isAdmin = computed(() => getRole() === 'admin');
const pageTitle = computed(() => (route.path.startsWith('/apps/') ? '应用详情' : TITLES[route.path] || 'choyeon panel'));
const renewing = ref(false);
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

const menu = computed(() => {
  const base = [
    { label: '总览', key: '/dashboard', icon: icon('SpeedometerOutline') },
    { label: '应用', key: '/apps', icon: icon('ApplicationsOutline') },
    { label: '系统服务', key: '/services', icon: icon('ServerOutline') },
    { label: '数据库', key: '/db', icon: icon('CloudOutline') },
    { label: '备份', key: '/backups', icon: icon('TimeOutline') },
    { label: '文件', key: '/files', icon: icon('FolderOpenOutline') },
  ];
  if (isAdmin.value) base.push({ label: '终端', key: '/terminal', icon: icon('TerminalOutline') });
  base.push({ label: '设置', key: '/settings', icon: icon('SettingsOutline') });
  return base;
});

const active = computed(() => {
  if (route.path.startsWith('/apps')) return '/apps';
  return route.path;
});

async function doLogout() {
  setToken('');
  router.push('/login');
}

async function doChangePwd() {
  try {
    await api.changePassword(oldPwd.value, newPwd.value);
    oldPwd.value = newPwd.value = '';
    showPwd.value = false;
    msg.success('密码已修改');
  } catch (e: any) {
    msg.error(e.message);
  }
}

const userOpts = computed(() => [
  { label: '修改密码', key: 'pwd', icon: icon('KeyOutline', 16), onClick: () => (showPwd.value = true) },
  { label: '退出登录', key: 'out', icon: icon('ArrowForwardOutline', 16), onClick: doLogout },
]);
</script>

<template>
  <router-view v-if="isLogin" />
  <NLayout v-else class="h-screen" has-sider>
    <NLayoutSider
      collapse-mode="width"
      :collapsed-width="60"
      :width="208"
      :collapsed="collapsed"
      bordered
      show-trigger="bar"
      :native-scrollbar="false"
      @collapse="collapsed = true"
      @expand="collapsed = false"
      style="background: #14141a"
    >
      <div class="logo" :class="{ mini: collapsed }">
        <div class="logo-mark">C</div>
        <transition name="fade">
          <span v-if="!collapsed" class="logo-text">choyeon <b>panel</b></span>
        </transition>
      </div>
      <NMenu
        :indent="18"
        :collapsed-icon-size="19"
        :collapsed="collapsed"
        :collapsed-width="60"
        :value="active"
        :options="menu"
        @update:value="(k: string) => router.push(k)"
      />
    </NLayoutSider>
    <NLayout :content-style="`display:flex;flex-direction:column;height:100vh;overflow:hidden;background:#101014`">
      <NLayoutHeader bordered style="height: 54px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px">
        <NText depth="3" style="font-size: 12.5px; letter-spacing: 0.02em">
          {{ pageTitle }} · {{ route.path }}
        </NText>
        <NSpace align="center" :size="10">
          <NButton v-if="isAdmin" size="small" tertiary :loading="renewing" @click="renewNow">续期证书</NButton>
          <NDropdown :options="userOpts" trigger="click" placement="bottom-end">
            <div class="user-chip">
              <span class="avatar">{{ isAdmin ? 'A' : 'V' }}</span>
              <span style="font-size: 13px">{{ isAdmin ? '管理员' : '只读' }}</span>
            </div>
          </NDropdown>
        </NSpace>
      </NLayoutHeader>
      <NLayoutContent content-style="padding: 20px 22px; overflow: auto; flex: 1" :native-scrollbar="false">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in"><component :is="Component" /></transition>
        </router-view>
      </NLayoutContent>
    </NLayout>
    <NModal v-model:show="showPwd" preset="card" title="修改密码" style="width: 360px">
      <NSpace vertical>
        <NInput v-model:value="oldPwd" type="password" placeholder="原密码" />
        <NInput v-model:value="newPwd" type="password" placeholder="新密码（至少 8 位）" />
        <NButton type="primary" block @click="doChangePwd">确认修改</NButton>
      </NSpace>
    </NModal>
  </NLayout>
</template>

<style scoped>
.h-screen { height: 100vh; }
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 16px 12px;
}
.logo.mini { justify-content: center; padding: 16px 0 12px; }
.logo-mark {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #4f7cff, #7f5bff);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  flex-shrink: 0;
  box-shadow: 0 2px 10px rgba(79, 124, 255, 0.4);
}
.logo-text {
  font-size: 14.5px;
  color: #d8d8e2;
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.logo-text b { color: #86a8ff; }
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px 4px 4px;
  border-radius: 20px;
  cursor: pointer;
  color: #c9c9d2;
  transition: background 0.15s;
}
.user-chip:hover { background: rgba(255, 255, 255, 0.06); }
.avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: linear-gradient(135deg, #4f7cff, #7f5bff);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.12s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
