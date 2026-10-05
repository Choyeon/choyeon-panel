<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NInput, NButton, NSpace, NIcon, NAlert, useMessage } from 'naive-ui';
import { api, setToken, setRole } from '../api';
import { icons } from '../icons';

const router = useRouter();
const msg = useMessage();
const needsSetup = ref(false);
const username = ref('');
const password = ref('');
const password2 = ref('');
const err = ref('');
const loading = ref(false);
const ready = ref(false);

onMounted(async () => {
  try {
    const s = await api.status();
    needsSetup.value = s.needsSetup;
    if (!s.needsSetup && s.username) username.value = s.username;
  } catch {
    err.value = '无法连接面板后端，请确认服务已启动';
  }
  setTimeout(() => (ready.value = true), 30);
});

const FEATS = [
  'Node / Python 项目一键部署与回滚日志',
  'nginx 反代 + Let’s Encrypt 全自动',
  'PostgreSQL / Redis 管理与定时备份',
  '实时监控 · 网页终端 · 多用户权限',
];

function validate(): string {
  if (!username.value) return '请输入用户名';
  if (password.value.length < 8) return '密码至少 8 位';
  if (needsSetup.value && password.value !== password2.value) return '两次密码不一致';
  return '';
}

async function submit() {
  err.value = validate();
  if (err.value) return;
  loading.value = true;
  try {
    const r = needsSetup.value
      ? await api.setup(username.value, password.value)
      : await api.login(username.value, password.value);
    setToken(r.token);
    setRole(r.role || 'admin');
    router.replace('/dashboard');
  } catch (e: any) {
    err.value = e.message || '请求失败';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="wrap">
    <div class="bg-glow g1" aria-hidden="true"></div>
    <div class="bg-glow g2" aria-hidden="true"></div>

    <div class="left" :class="{ ready }">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">C</div>
        <span>choyeon <b>panel</b></span>
      </div>
      <h1>服务器，<br />尽在掌握。</h1>
      <p class="tagline">应用部署 · systemd 服务 · 数据库 · 备份 · 监控告警 —— 一台机器，一个面板。</p>
      <ul class="feats">
        <li v-for="f in FEATS" :key="f">
          <NIcon :component="icons.CheckmarkCircleOutline" :size="16" color="#34c77b" />
          <span>{{ f }}</span>
        </li>
      </ul>
    </div>

    <div class="right" :class="{ ready }">
      <div class="card">
        <h2>{{ needsSetup ? '初始化面板' : '欢迎回来' }}</h2>
        <p class="sub">{{ needsSetup ? '创建管理员账号开始使用' : '登录以管理 choyeon 服务器' }}</p>

        <NAlert v-if="err" type="error" :bordered="false" size="small" style="margin-bottom: 14px">
          {{ err }}
        </NAlert>

        <form @submit.prevent="submit">
          <NSpace vertical :size="14">
            <div class="field">
              <label for="cp-username">用户名</label>
              <NInput
                id="cp-username"
                v-model:value="username"
                placeholder="admin"
                size="large"
                autocomplete="username"
                :autofocus="true"
                :input-props="{ autocomplete: 'username' }"
              />
            </div>
            <div class="field">
              <label for="cp-password">密码</label>
              <NInput
                id="cp-password"
                v-model:value="password"
                type="password"
                size="large"
                show-password-on="click"
                placeholder="至少 8 位"
                autocomplete="current-password"
                :input-props="{ autocomplete: needsSetup ? 'new-password' : 'current-password' }"
              />
            </div>
            <div v-if="needsSetup" class="field">
              <label for="cp-password2">确认密码</label>
              <NInput
                id="cp-password2"
                v-model:value="password2"
                type="password"
                size="large"
                placeholder="再输入一次"
                :input-props="{ autocomplete: 'new-password' }"
              />
            </div>
            <NButton
              type="primary"
              size="large"
              block
              attr-type="submit"
              :loading="loading"
              :disabled="loading"
            >
              {{ needsSetup ? '创建并进入' : '登 录' }}
              <template #icon><NIcon :component="icons.ArrowForwardOutline" /></template>
            </NButton>
          </NSpace>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6vw;
  background: var(--cp-bg);
  position: relative;
  overflow: hidden;
  padding: 24px;
  box-sizing: border-box;
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(120px);
  opacity: 0.22;
  pointer-events: none;
}
.g1 { width: 520px; height: 520px; background: #4f7cff; top: -140px; left: -120px; }
.g2 { width: 460px; height: 460px; background: #7f5bff; bottom: -160px; right: -100px; }
.left {
  max-width: 460px;
  opacity: 0;
  transform: translateY(14px);
  transition: opacity 0.5s ease 0.05s, transform 0.5s ease 0.05s;
}
.left.ready { opacity: 1; transform: none; }
.right {
  width: 360px;
  max-width: 100%;
  opacity: 0;
  transform: translateY(14px);
  transition: opacity 0.5s ease 0.15s, transform 0.5s ease 0.15s;
}
.right.ready { opacity: 1; transform: none; }
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 34px;
  color: var(--cp-text);
  font-size: 17px;
}
.brand b { color: var(--brand-soft); }
.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: linear-gradient(135deg, #4f7cff, #7f5bff);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 18px rgba(79, 124, 255, 0.45);
}
h1 {
  font-size: 34px;
  line-height: 1.3;
  margin: 0 0 14px;
  color: var(--cp-text-strong);
  letter-spacing: -0.01em;
}
.tagline { color: var(--cp-text-dim); font-size: 14px; margin: 0 0 26px; }
.feats {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 11px;
  color: var(--cp-text-dim);
  font-size: 13px;
}
.feats li { display: flex; align-items: center; gap: 9px; }
.card {
  background: var(--cp-elevated);
  border: 1px solid var(--cp-border);
  border-radius: 16px;
  padding: 30px 28px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
}
html[data-theme='dark'] .card { background: rgba(24, 24, 30, 0.85); backdrop-filter: blur(8px); }
.card h2 { margin: 0 0 4px; font-size: 21px; color: var(--cp-text-strong); }
.sub { margin: 0 0 22px; color: var(--cp-text-mute); font-size: 13px; }
.field label { display: block; font-size: 12px; color: var(--cp-text-dim); margin-bottom: 6px; }
@media (max-width: 900px) {
  .wrap { gap: 0; }
  .left { display: none; }
  .right { width: 100%; max-width: 420px; }
}
</style>
