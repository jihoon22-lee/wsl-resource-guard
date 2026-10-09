"use strict";
// Only revision metadata is cached, never pages, task names, credentials or API data.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
let pushQueue = Promise.resolve();
async function showPush(event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch { data = {title: "Resource Guard", body: "알림 내용을 읽지 못했습니다. 웹에서 최신 상태를 확인하세요."}; }
  const id = /^[0-9a-f]{32}$/.test(data.incident_id || "") ? data.incident_id : null;
  const revision = Number.isSafeInteger(data.revision) && data.revision > 0 ? data.revision : 0;
  const url = id ? "/#incident?id=" + id : "/#alerts";
  if (id && revision) {
    try {
      const cache = await caches.open("wrg-alert-revisions-v1");
      const key = new URL("/__alert_revision__/" + id, self.location.origin).href;
      const previous = await cache.match(key);
      const stored = previous ? await previous.json() : {};
      const observed = Number(data.observed_at) || 0;
      if (stored.revision > revision || (stored.revision === revision && stored.observed_at >= observed)) return;
      await cache.put(key, new Response(JSON.stringify({revision, observed_at: observed})));
      const keys = await cache.keys();
      for (const old of keys.slice(0, Math.max(0, keys.length - 256))) await cache.delete(old);
    } catch { /* A cache failure cannot prevent the alert; clicks always fetch current state. */ }
  }
  await self.registration.showNotification(String(data.title || "Resource Guard").slice(0,120), {
    body: String(data.body || "").slice(0,500), tag: id ? "wrg-" + id : "wrg-alert",
    renotify: true, requireInteraction: data.severity === "critical",
    icon: "/assets/icon.svg", badge: "/assets/icon.svg", data: {url, incident_id:id, revision}
  });
}
self.addEventListener("push", event => {
  pushQueue = pushQueue.catch(()=>{}).then(()=>showPush(event));
  event.waitUntil(pushQueue);
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const id=event.notification.data?.incident_id;
  const path=/^[0-9a-f]{32}$/.test(id || "") ? "/#incident?id="+id : "/#alerts";
  const url=new URL(path,self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(windows=>{
    // Never navigate a different tab away from an unfinished form or confirmation.
    const same=windows.find(w=>w.url===url);
    return same ? same.focus() : self.clients.openWindow(url);
  }));
});
