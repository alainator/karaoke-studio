// Web Push for the Studio app's notifications, with Node's own crypto (no packages): VAPID (RFC 8292) and the
// aes128gcm message encryption (RFC 8291). The Studio (app.py) runs it:
//   node webpush.mjs keys                       -> {"public": b64url, "private": b64url}   (a new VAPID key pair)
//   node webpush.mjs send < {"sub", "payload", "vapid": {"public", "private"}, "subject"}
//                                               -> {"status": http status}   (404/410: the subscription is gone)
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";

const b64u = (b) => Buffer.from(b).toString("base64url");
const unb64u = (s) => Buffer.from(s, "base64url");

function vapidKeys() {
  const ecdh = crypto.createECDH("prime256v1");
  ecdh.generateKeys();
  return { public: b64u(ecdh.getPublicKey()), private: b64u(ecdh.getPrivateKey()) };
}

function signingKey(pub, priv) {
  const p = unb64u(pub);   // uncompressed point: 0x04 || x || y
  return crypto.createPrivateKey({ key: { kty: "EC", crv: "P-256", d: priv, x: b64u(p.subarray(1, 33)), y: b64u(p.subarray(33, 65)) }, format: "jwk" });
}

function vapidHeader(endpoint, vapid, subject) {
  const aud = new URL(endpoint).origin;
  const head = b64u(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const body = b64u(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: subject }));
  const sig = crypto.sign("sha256", Buffer.from(`${head}.${body}`), { key: signingKey(vapid.public, vapid.private), dsaEncoding: "ieee-p1363" });
  return `vapid t=${head}.${body}.${b64u(sig)}, k=${vapid.public}`;
}

const hmac = (key, data) => crypto.createHmac("sha256", key).update(data).digest();

function encrypt(sub, payload) {
  const uaPub = unb64u(sub.keys.p256dh), auth = unb64u(sub.keys.auth);
  const local = crypto.createECDH("prime256v1");
  local.generateKeys();
  const asPub = local.getPublicKey();
  const shared = local.computeSecret(uaPub);
  const salt = crypto.randomBytes(16);
  const keyInfo = Buffer.concat([Buffer.from("WebPush: info\0"), uaPub, asPub]);
  const ikm = hmac(hmac(auth, shared), Buffer.concat([keyInfo, Buffer.from([1])]));
  const prk = hmac(salt, ikm);
  const cek = hmac(prk, Buffer.from("Content-Encoding: aes128gcm\0\x01")).subarray(0, 16);
  const nonce = hmac(prk, Buffer.from("Content-Encoding: nonce\0\x01")).subarray(0, 12);
  const c = crypto.createCipheriv("aes-128-gcm", cek, nonce);
  const ct = Buffer.concat([c.update(Buffer.concat([Buffer.from(payload), Buffer.from([2])])), c.final(), c.getAuthTag()]);
  const rs = Buffer.alloc(4); rs.writeUInt32BE(4096);
  return Buffer.concat([salt, rs, Buffer.from([asPub.length]), asPub, ct]);
}

async function send(job) {
  const body = encrypt(job.sub, JSON.stringify(job.payload));
  const r = await fetch(job.sub.endpoint, {
    method: "POST",
    headers: { "TTL": "86400", "Urgency": "high", "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "Authorization": vapidHeader(job.sub.endpoint, job.vapid, job.subject) },
    body,
    signal: AbortSignal.timeout(15000),
  });
  return { status: r.status };
}

const main = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
const cmd = main ? process.argv[2] : null;
if (!main) {
  // imported (tests)
} else if (cmd === "keys") {
  process.stdout.write(JSON.stringify(vapidKeys()));
} else if (cmd === "send") {
  let input = "";
  process.stdin.on("data", (d) => { input += d; });
  process.stdin.on("end", async () => {
    try { process.stdout.write(JSON.stringify(await send(JSON.parse(input)))); }
    catch (e) { process.stdout.write(JSON.stringify({ status: 0, error: String(e).slice(0, 200) })); }
  });
} else {
  process.stderr.write("usage: node webpush.mjs keys | send < job.json\n");
  process.exit(2);
}
export { encrypt, vapidHeader, vapidKeys };
