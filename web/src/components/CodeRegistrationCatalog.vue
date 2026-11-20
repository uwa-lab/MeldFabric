<script setup>
defineProps({
  items: { type: Array, default: () => [] },
  title: { type: String, default: "代码注册项" },
  description: { type: String, default: "当前进程通过装饰器登记的代码能力。" },
  actionLabel: { type: String, default: "创建配置" },
  actionable: { type: Boolean, default: true },
  usageLabel: { type: String, default: "仅供配置引用" },
  emptyText: { type: String, default: "当前进程还没有发现此类代码注册项。" },
});

defineEmits(["create"]);

function kindLabel(kind) {
  return (
    {
      tool: "Tool",
      plugin: "Plugin",
      hook: "Hook",
      hook_group: "Hook Group",
      intervention: "Intervention",
      intervention_group: "Intervention Group",
      graph_condition: "Graph Condition",
    }[kind] || kind
  );
}

function memberLabel(item) {
  const members = item.members || [];
  if (!members.length) return "无成员能力";
  const counts = members.reduce((result, member) => {
    result[member.kind] = (result[member.kind] || 0) + 1;
    return result;
  }, {});
  const summary = Object.entries(counts)
    .map(([kind, count]) => `${count} ${kind}`)
    .join(" · ");
  const details = [
    ...new Set(
      members.flatMap((member) =>
        member.kind === "tool"
          ? [member.name].filter(Boolean)
          : member.events || [],
      ),
    ),
  ];
  return details.length ? `${summary} · ${details.join(" / ")}` : summary;
}
</script>

<template>
  <section class="code-catalog">
    <header>
      <div>
        <h2>{{ title }}</h2>
        <UiDescription>{{ description }}</UiDescription>
      </div>
      <span>{{ items.length }}</span>
    </header>
    <div v-if="items.length" class="code-catalog-list">
      <article v-for="item in items" :key="item.id">
        <span class="code-catalog-mark">&lt;/&gt;</span>
        <div class="code-catalog-copy">
          <div>
            <strong>{{ item.name }}</strong>
            <span>{{ kindLabel(item.kind) }}</span>
          </div>
          <UiDescription>{{ item.description }}</UiDescription>
          <footer>
            <code>{{ item.python_path }}</code>
            <small v-if="item.members?.length">{{ memberLabel(item) }}</small>
          </footer>
        </div>
        <UiButton
          v-if="actionable"
          variant="secondary"
          @click="$emit('create', item)"
        >
          {{ actionLabel }}
        </UiButton>
        <span v-else class="code-catalog-usage">{{ usageLabel }}</span>
      </article>
    </div>
    <p v-else class="empty-state">{{ emptyText }}</p>
  </section>
</template>
