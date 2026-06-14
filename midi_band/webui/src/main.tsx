import React from "react";
import ReactDOM from "react-dom/client";
import { config } from "@fortawesome/fontawesome-svg-core";
import "@fortawesome/fontawesome-svg-core/styles.css";
import "@fontsource/days-one/400.css";
import "./styles.css";
import { App } from "./App";
import { ToastProvider } from "./components/Toasts";

// we import the FA css ourselves above, stop the lib injecting it again
config.autoAddCss = false;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </React.StrictMode>,
);
