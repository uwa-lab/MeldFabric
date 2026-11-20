<script setup>
import { computed } from "vue";
import { useRoute } from "vue-router";
import { resourceSections, extensionSections } from "../domain/metadata.js";
import { state } from "../store.js";

const props = defineProps({ group: String });
const route = useRoute();
const sections = computed(() => {
  const source = props.group === "extensions" ? extensionSections : resourceSections;
  const result = { ...source };
  if (props.group === "resources") {
    if (!state.features?.resource_pools?.mcp_servers) delete result.mcp_servers;
    if (!state.features?.resource_pools?.sandbox_profiles) delete result.sandbox_profiles;
    if (!state.features?.resource_pools?.observability_providers)
      delete result.observability_providers;
  }
  return result;
});
const base = computed(() => (props.group === "extensions" ? "/extensions" : "/resources"));
</script>

<template>
  <div class="page pool-page">
    <aside class="pool-navigation">
      <header>
        <p class="eyebrow">
          {{ group === "extensions" ? "EXTENSIONS" : "RESOURCES" }}
        </p>
        <h1>{{ group === "extensions" ? "扩展" : "资源" }}</h1>
      </header>
      <nav>
        <RouterLink
          v-for="(meta, key) in sections"
          :key="key"
          :to="`${base}/${key}`"
          :class="{ active: route.path.startsWith(`${base}/${key}`) }"
        >
          <strong>{{ meta.label }}</strong
          ><small>{{ meta.description }}</small>
        </RouterLink>
      </nav>
    </aside>
    <section class="resource-outlet">
      <RouterView :key="[route.params.type, route.params.id].join(':')" />
    </section>
  </div>
</template>
