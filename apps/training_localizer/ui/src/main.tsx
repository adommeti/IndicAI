import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Delivery from "./Delivery";
import "./styles/tokens.css";
import "./styles/index.css";

/** Two surfaces in one bundle, chosen by query string: the reviewer table
 *  (`?module=`) and the employee delivery page (`?deliver=`). A router would be
 *  more than two routes need. */
const params = new URLSearchParams(window.location.search);
const deliver = params.get("deliver");

createRoot(document.getElementById("root")!).render(
  <StrictMode>{deliver ? <Delivery moduleId={deliver} /> : <App />}</StrictMode>,
);
