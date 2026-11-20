import { createApp } from "vue";
import Disclosure from "./components/Disclosure.vue";
import UiButton from "./components/UiButton.vue";
import UiDescription from "./components/UiDescription.vue";
import App from "./App.vue";
import { router } from "./router.js";
import { contextMenuDirective } from "./directives/contextMenu.js";
import "./style.css";

createApp(App)
  .component("Disclosure", Disclosure)
  .component("UiButton", UiButton)
  .component("UiDescription", UiDescription)
  .directive("context-menu", contextMenuDirective)
  .use(router)
  .mount("#app");
