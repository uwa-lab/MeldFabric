import { createRouter, createWebHistory } from "vue-router";
import { adminBase } from "./api.js";
import OverviewView from "./views/OverviewView.vue";
import DirectoryView from "./views/DirectoryView.vue";
import EntityEditorView from "./views/EntityEditorView.vue";
import PoolLayout from "./views/PoolLayout.vue";
import DiagnosticsView from "./views/DiagnosticsView.vue";
import SystemView from "./views/SystemView.vue";
import DeveloperDocsView from "./views/DeveloperDocsView.vue";
import PlaygroundView from "./views/PlaygroundView.vue";
import NotFoundView from "./views/NotFoundView.vue";
import { extensionSections, resourceSections } from "./domain/metadata.js";

if (location.pathname === adminBase || location.pathname === adminBase + "/")
  history.replaceState({}, "", adminBase + "/ui/");

const directory = (path, type) => [
  {
    path,
    name: type,
    component: DirectoryView,
    props: { type },
  },
  {
    path: `${path}/:id`,
    name: `${type}-editor`,
    component: EntityEditorView,
    props: (route) => ({ type, id: route.params.id }),
  },
];

const poolTypes = {
  resources: new Set(Object.keys(resourceSections)),
  extensions: new Set(Object.keys(extensionSections)),
};

function poolRouteGuard(group) {
  return (to) => {
    const type = String(to.params.type || "");
    if (poolTypes[group].has(type)) return true;
    const destination = group === "resources" ? "extensions" : "resources";
    if (poolTypes[destination].has(type)) {
      const suffix = to.params.id ? `/${encodeURIComponent(to.params.id)}` : "";
      return `/${destination}/${encodeURIComponent(type)}${suffix}`;
    }
    return `/${group}`;
  };
}

export const router = createRouter({
  history: createWebHistory(adminBase + "/ui"),
  scrollBehavior(to, from, saved) {
    if (saved) return saved;
    if (to.path === from.path) return false;
    return { top: 0, behavior: "instant" };
  },
  routes: [
    { path: "/", name: "welcome", component: OverviewView },
    ...directory("/agents", "agents"),
    ...directory("/orchestrations", "orchestrations"),
    ...directory("/endpoints", "endpoints"),
    {
      path: "/playground/:conversationId?",
      name: "playground",
      component: PlaygroundView,
    },
    {
      path: "/resources",
      name: "resources",
      component: PoolLayout,
      props: { group: "resources" },
      children: [
        {
          path: "",
          name: "resources-index",
          redirect: "/resources/models",
        },
        {
          path: ":type",
          name: "resource-directory",
          component: DirectoryView,
          props: true,
          beforeEnter: poolRouteGuard("resources"),
        },
        {
          path: ":type/:id",
          name: "resource-editor",
          component: EntityEditorView,
          props: true,
          beforeEnter: poolRouteGuard("resources"),
        },
      ],
    },
    {
      path: "/extensions",
      name: "extensions",
      component: PoolLayout,
      props: { group: "extensions" },
      children: [
        {
          path: "",
          name: "extensions-index",
          redirect: "/extensions/plugins",
        },
        {
          path: ":type",
          name: "extension-directory",
          component: DirectoryView,
          props: true,
          beforeEnter: poolRouteGuard("extensions"),
        },
        {
          path: ":type/:id",
          name: "extension-editor",
          component: EntityEditorView,
          props: true,
          beforeEnter: poolRouteGuard("extensions"),
        },
      ],
    },
    { path: "/operations", redirect: "/diagnostics" },
    { path: "/operations/diagnostics", redirect: "/diagnostics" },
    { path: "/operations/system", redirect: "/system" },
    {
      path: "/diagnostics",
      name: "diagnostics",
      component: DiagnosticsView,
    },
    {
      path: "/system",
      name: "system",
      component: SystemView,
    },
    {
      path: "/developer/:section?",
      name: "developer",
      component: DeveloperDocsView,
    },
    { path: "/:pathMatch(.*)*", name: "not-found", component: NotFoundView },
  ],
});

export function listPath(type) {
  if (["agents", "orchestrations", "endpoints"].includes(type)) return `/${type}`;
  if (["plugins", "hooks", "interventions", "graph_conditions"].includes(type))
    return `/extensions/${type}`;
  return `/resources/${type}`;
}

export const editorPath = (type, id) => `${listPath(type)}/${encodeURIComponent(id)}`;
