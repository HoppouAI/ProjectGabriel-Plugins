import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource/days-one/400.css";
import "./styles.css";
import { App } from "./App";
import { ToastProvider } from "./components/Toasts";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </React.StrictMode>,
);
