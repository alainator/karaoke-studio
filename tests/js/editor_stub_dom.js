process.on("unhandledRejection", () => {});
const mk = (base = {}) => new Proxy(Object.assign(function () {}, base), {
  get: (t, k) => { if (k in t) return t[k]; if (k === Symbol.toPrimitive) return () => ""; if (k === "then") return undefined; if (k === Symbol.iterator) return [][Symbol.iterator]; t[k] = mk(); return t[k]; },
  apply: () => mk(), construct: () => mk(), set: (t, k, v) => { t[k] = v; return true; } });
const elems = {};
globalThis.document = mk({querySelector: s => elems[s] || (elems[s] = mk({style: {}, dataset: {}, value: "", classList: mk(), addEventListener() {}, offsetHeight: 50, scrollTop: 0})),
  querySelectorAll: () => [], addEventListener() {}, createElement: () => mk({style: {}}), activeElement: {tagName: "BODY"}, documentElement: mk({style: mk()})});
globalThis.window = globalThis; window.addEventListener = () => {}; window.scrollY = 0; window.scrollTo = () => {}; window.devicePixelRatio = 1;
window.matchMedia = () => mk({matches: false}); globalThis.localStorage = {getItem: () => null, setItem() {}};
globalThis.location = mk({hash: "", pathname: "/", search: ""}); globalThis.navigator = mk({serviceWorker: undefined});
globalThis.fetch = () => Promise.reject(new Error("offline")); globalThis.requestAnimationFrame = () => 0; globalThis.cancelAnimationFrame = () => {};
globalThis.history = mk(); globalThis.Image = function () { return mk({complete: false}); }; globalThis.EventSource = function () { return mk(); };
globalThis.setInterval = () => 0;
