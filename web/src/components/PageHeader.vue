<script setup lang="ts">
defineProps<{ title: string; sub?: string }>();
</script>

<template>
  <div class="ph">
    <div>
      <h2 class="ph-title">{{ title }}</h2>
      <p v-if="sub" class="ph-sub">{{ sub }}</p>
    </div>
    <div class="ph-actions"><slot name="actions" /></div>
  </div>
</template>

<style scoped>
.ph {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.ph-title {
  margin: 0;
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: #e8e8ec;
}
.ph-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: #77777f;
}
.ph-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
</style>
