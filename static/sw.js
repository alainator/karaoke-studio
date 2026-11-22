/* The Studio app's service worker: makes it installable and shows the notifications the Studio sends (app.py
   notify: a guest's song request). It caches nothing: the Studio is always live (and behind its login). */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", function () { /* (network as usual) */ });
self.addEventListener("push", function (e) {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: "Karaoke Studio", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Karaoke Studio", {
    body: d.body || "", icon: "/static/pwa/icon-192.png", badge: "/static/pwa/icon-192.png", tag: d.tag || undefined,
    data: { url: d.url || "/" },
  }));
});
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (const c of list) { if ("focus" in c) { c.navigate(url).catch(function () {}); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
