// Minimal service worker. It exists so browsers treat the page as installable
// (which is what makes "Share to vidfetch" appear). It caches nothing.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
