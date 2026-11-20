<script setup>
defineProps({ name: String });
const paths = {
  search: "M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15 M16 16l5 5",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  agents:
    "M12 3v3 M8 3h8 M5 6h14a2 2 0 0 1 2 2v11H3V8a2 2 0 0 1 2-2 M8 11v2 M16 11v2 M8 16h8",
  orchestrations:
    "M4 5h5v5H4z M15 4h5v5h-5z M10 15h5v5h-5z M9 7h6 M17 9v3l-4 3 M7 10v3l4 2",
  endpoints: "M4 12h12 M12 7l5 5-5 5 M4 5v14 M20 5v14",
  playground: "M4 5h16v12H8l-4 4z M8 9h8 M8 13h5",
  resources: "m12 3 9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5",
  extensions: "M8 3v5H3 M16 3v5h5 M8 21v-5H3 M16 21v-5h5 M8 8h8v8H8z",
  operations: "M3 12h4l3-8 4 16 3-8h4",
  topology: "M9 3h6v6H9z M3 16h6v5H3z M15 16h6v5h-6z M12 9v4 M6 16v-3h12v3",
  system: "M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6",
  config: "m8 5-6 7 6 7 M16 5l6 7-6 7 M14 3l-4 18",
  diagnostics: "M3 12h4l3-8 4 16 3-8h4",
  developer: "M4 5h16v14H4z M8 9l-2 3 2 3 M16 9l2 3-2 3 M14 7l-4 10",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4",
};
</script>
<template>
  <svg
    class="icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.resources" />
  </svg>
</template>
