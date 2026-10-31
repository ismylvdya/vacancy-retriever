"use strict";

/* Every tunable of the interface in one place: timings, thresholds, browser-storage keys. Colors and sizes live
   in tokens.css. */
const UI = Object.freeze({
  poll: { idleMs: 15000, runningMs: 1500, retryMs: 5000 },
  applications: { activeMs: 2500, idleMs: 30000, retryMs: 15000 },
  toast: { plainMs: 2500, undoMs: 6000 },
  limits: { warnPercent: 80, dangerPercent: 95, refreshWaitMs: 6000 },
  storage: {
    debug: "vr-debug",
    sidebarOpen: "vr-sidebar-open",
    history: "vr-history",
    runOptions: "vr-run-options",
  },
});
