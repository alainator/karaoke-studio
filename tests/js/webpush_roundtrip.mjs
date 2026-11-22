// webpush.mjs: what it encrypts, a browser can decrypt (RFC 8291, done here the receiving side's way), and its VAPID
// token is a valid ES256 JWT for the push service's origin.
import crypto from "node:crypto";
import { encrypt, vapidHeader, vapidKeys } from "../../webpush.mjs";
const hmac = (k, d) => crypto.createHmac("sha256", k).update(d).digest();
const ua = crypto.createECDH("prime256v1"); ua.generateKeys();
const auth = crypto.randomBytes(16);
const sub = { endpoint: "https://push.example.com/abc", keys: { p256dh: ua.getPublicKey().toString("base64url"), auth: auth.toString("base64url") } };
const msg = JSON.stringify({ title: "Song request", body: "Toto - Africa" });
const enc = encrypt(sub, msg);
const salt = enc.subarray(0, 16), idlen = enc[20], asPub = enc.subarray(21, 21 + idlen), ct = enc.subarray(21 + idlen);
const shared = ua.computeSecret(asPub);
const keyInfo = Buffer.concat([Buffer.from("WebPush: info\0"), ua.getPublicKey(), asPub]);
const ikm = hmac(hmac(auth, shared), Buffer.concat([keyInfo, Buffer.from([1])]));
const prk = hmac(salt, ikm);
const cek = hmac(prk, Buffer.from("Content-Encoding: aes128gcm\0\x01")).subarray(0, 16);
const nonce = hmac(prk, Buffer.from("Content-Encoding: nonce\0\x01")).subarray(0, 12);
const d = crypto.createDecipheriv("aes-128-gcm", cek, nonce);
d.setAuthTag(ct.subarray(ct.length - 16));
const plain = Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]);
if (plain.subarray(0, plain.length - 1).toString() !== msg || plain[plain.length - 1] !== 2) { console.error("decrypt mismatch"); process.exit(1); }
const v = vapidKeys();
const h = vapidHeader(sub.endpoint, v, "https://github.com/alainator/karaoke-studio");
const [, jwt] = h.match(/t=([^,]+)/), [hd, bd, sg] = jwt.split(".");
const claims = JSON.parse(Buffer.from(bd, "base64url"));
const p = Buffer.from(v.public, "base64url");
const pub = crypto.createPublicKey({ key: { kty: "EC", crv: "P-256", x: p.subarray(1, 33).toString("base64url"), y: p.subarray(33).toString("base64url") }, format: "jwk" });
const ok = crypto.verify("sha256", Buffer.from(`${hd}.${bd}`), { key: pub, dsaEncoding: "ieee-p1363" }, Buffer.from(sg, "base64url"));
if (!ok || claims.aud !== "https://push.example.com") { console.error("vapid bad", ok, claims.aud); process.exit(1); }
console.log("webpush ok");
