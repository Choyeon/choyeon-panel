<script setup lang="ts">
import { NButton, NResult } from 'naive-ui';
import { useRouter } from 'vue-router';

const router = useRouter();
</script>

<template>
  <div class="nf">
    <NResult status="404" title="页面不存在" description="链接可能已失效，或该功能尚未提供。">
      <template #footer>
        <NButton type="primary" size="small" @click="router.push('/dashboard')">回到总览</NButton>
      </template>
    </NResult>
  </div>
</template>

<style scoped>
.nf {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
}
</style>
