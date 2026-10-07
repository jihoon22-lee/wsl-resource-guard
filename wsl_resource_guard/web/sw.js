"use strict";
// Push-only service worker: shows guard alerts and opens the dashboard on
// tap. It caches nothing, so the dashboard is always live data.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "Resource Guard", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Resource Guard", {
      body: data.body || "",
      tag: data.tag || "wrg-alert",
      renotify: true,
      requireInteraction: data.severity === "critical",
      icon: "/assets/icon.svg",
      badge: "/assets/icon.svg",
      data: { url: data.url || "/#overview" },
    }),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/#overview", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => w.url.startsWith(self.location.origin));
      if (open) return open.navigate(url).then((w) => (w || open).focus());
      return self.clients.openWindow(url);
    }),
  );
});
