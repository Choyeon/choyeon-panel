<script setup lang="ts">
defineProps<{ title: string; sub?: string }>();
</script>

<template>
  <div class="ph">
    <div class="ph-main">
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
  flex-wrap: wrap;
}
.ph-main { min-width: 0; flex: 1 1 220px; }
.ph-title {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--cp-text-strong);
  overflow-wrap: anywhere;
}
.ph-sub {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  color: var(--cp-text-mute);
  overflow-wrap: anywhere;
}
.ph-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
@media (max-width: 640px) {
  .ph { gap: 10px; }
  .ph-actions { justify-content: flex-start; width: 100%; }
}
</style>
