<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NInput, NButton, NSpace, NText, NIcon, useMessage } from 'naive-ui';
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
  const s = await api.status();
  needsSetup.value = s.needsSetup;
  setTimeout(() => (ready.value = true), 30);
});

async function submit() {
  err.value = '';
  if (password.value.length < 8) return (err.value = '密码至少 8 位');
  if (needsSetup.value && password.value !== password2.value) return (err.value = '两次密码不一致');
  loading.value = true;
  try {
    const r = needsSetup.value
      ? await api.setup(username.value, password.value)
      : await api.login(username.value, password.value);
    setToken(r.token);
    setRole(r.role || 'admin');
    router.push('/dashboard');
  } catch (e: any) {
    err.value = e.message;
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="wrap">
    <div class="bg-glow g1"></div>
    <div class="bg-glow g2"></div>

    <div class="left" :class="{ ready }">
      <div class="brand">
        <div class="brand-mark">C</div>
        <span>choyeon <b>panel</b></span>
      </div>
      <h1>服务器，<br />尽在掌握。</h1>
      <p class="tagline">应用部署 · systemd 服务 · 数据库 · 备份 · 监控告警 —— 一台机器，一个面板。</p>
      <ul class="feats">
        <li><NIcon :component="icons.CheckmarkCircleOutline" :size="16" color="#34c77b" /> Node / Python 项目一键部署与回滚日志</li>
        <li><NIcon :component="icons.CheckmarkCircleOutline" :size="16" color="#34c77b" /> nginx 反代 + Let's Encrypt 全自动</li>
        <li><NIcon :component="icons.CheckmarkCircleOutline" :size="16" color="#34c77b" /> PostgreSQL / Redis 管理与定时备份</li>
        <li><NIcon :component="icons.CheckmarkCircleOutline" :size="16" color="#34c77b" /> 实时监控 · 网页终端 · 多用户权限</li>
      </ul>
    </div>

    <div class="right" :class="{ ready }">
      <div class="card">
        <template v-if="!needsSetup">
          <h2>欢迎回来</h2>
          <p class="sub">登录以管理 choyeon 服务器</p>
        </template>
        <template v-else>
          <h2>初始化面板</h2>
          <p class="sub">创建管理员账号开始使用</p>
        </template>
        <NSpace vertical :size="14">
          <div class="field">
            <label>用户名</label>
            <NInput v-model:value="username" placeholder="admin" size="large" :status="err ? 'error' : undefined" />
          </div>
          <div class="field">
            <label>密码</label>
            <NInput v-model:value="password" type="password" size="large" show-password-on="click" placeholder="至少 8 位" @keyup.enter="submit" />
          </div>
          <div v-if="needsSetup" class="field">
            <label>确认密码</label>
            <NInput v-model:value="password2" type="password" size="large" placeholder="再输入一次" @keyup.enter="submit" />
          </div>
          <NButton type="primary" size="large" block :loading="loading" @click="submit">
            {{ needsSetup ? '创建并进入' : '登 录' }}
            <template #icon><NIcon :component="icons.ArrowForwardOutline" /></template>
          </NButton>
        </NSpace>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6vw;
  background: #0c0c11;
  position: relative;
  overflow: hidden;
  padding: 24px;
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(120px);
  opacity: 0.22;
  pointer-events: none;
}
.g1 {
  width: 520px;
  height: 520px;
  background: #4f7cff;
  top: -140px;
  left: -120px;
}
.g2 {
  width: 460px;
  height: 460px;
  background: #7f5bff;
  bottom: -160px;
  right: -100px;
}
.left {
  max-width: 460px;
  opacity: 0;
  transform: translateY(14px);
  transition: all 0.5s ease 0.05s;
}
.left.ready { opacity: 1; transform: none; }
.right {
  width: 360px;
  opacity: 0;
  transform: translateY(14px);
  transition: all 0.5s ease 0.15s;
}
.right.ready { opacity: 1; transform: none; }
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 34px;
  color: #d9d9e2;
  font-size: 17px;
}
.brand b { color: #86a8ff; }
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
  color: #ececf2;
  letter-spacing: -0.01em;
}
.tagline {
  color: #8a8a95;
  font-size: 14px;
  margin: 0 0 26px;
}
.feats {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 11px;
  color: #a5a5b0;
  font-size: 13px;
}
.feats li { display: flex; align-items: center; gap: 9px; }
.card {
  background: rgba(24, 24, 30, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 16px;
  padding: 30px 28px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(8px);
}
.card h2 {
  margin: 0 0 4px;
  font-size: 21px;
  color: #eeeef4;
}
.sub {
  margin: 0 0 22px;
  color: #77777f;
  font-size: 13px;
}
.field label {
  display: block;
  font-size: 12px;
  color: #8b8b96;
  margin-bottom: 6px;
}
@media (max-width: 900px) {
  .left { display: none; }
}
</style>
