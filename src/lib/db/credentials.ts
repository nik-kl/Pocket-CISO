// SERVER ONLY. Role passwords from the environment (scrypt, per-process salt) and the HMAC secret (env or demo file).
import { promises as fs } from "fs";
import crypto from "crypto";
import { AUTH_SECRET_PATH, DATA_DIR, DEMO_MODE, Db, g } from "./types";

let authSecretCache: string | null = null;
const MIN_PASSWORD_LENGTH = 14;
const ENV_MANAGED = "env-managed"; // the DB never stores a password hash; credentials come from the environment

function configuredPassword(role: "L2" | "L3"): string {
  const envName = role === "L2" ? "POCKET_CISO_L2_PASSWORD" : "POCKET_CISO_L3_PASSWORD";
  const configured = process.env[envName]?.trim();
  if (configured) {
    if (!DEMO_MODE && configured.length < MIN_PASSWORD_LENGTH) throw new Error(`${envName} muss mindestens ${MIN_PASSWORD_LENGTH} Zeichen lang sein.`);
    return configured;
  }
  if (!DEMO_MODE) throw new Error(`${envName} muss im Nicht-Demo-Modus gesetzt sein.`);
  return role === "L2" ? "admin2026" : "ciso2026";
}

// Per-process random salt; the plaintext only lives in the environment, never in data/.
export function credential(role: "L2" | "L3") {
  const cache = (g.__pcCred ??= {});
  if (!cache[role]) {
    const salt = crypto.randomBytes(16);
    cache[role] = { salt, hash: crypto.scryptSync(configuredPassword(role), salt, 64) };
  }
  return cache[role]!;
}

export const configuredPasswordHash = (role: "L2" | "L3"): string => (configuredPassword(role), ENV_MANAGED);

export async function ensureAuthSecret(): Promise<string> {
  if (authSecretCache) return authSecretCache;
  const configured = process.env.POCKET_CISO_SECRET?.trim();
  if (configured) {
    if (configured.length < 32) throw new Error("POCKET_CISO_SECRET muss mindestens 32 Zeichen lang sein.");
    authSecretCache = configured;
    return configured;
  }
  if (!DEMO_MODE) {
    throw new Error("POCKET_CISO_SECRET muss außerhalb des Demo-Modus gesetzt sein.");
  }
  try {
    const existing = (await fs.readFile(AUTH_SECRET_PATH, "utf8")).trim();
    if (existing.length >= 32) {
      authSecretCache = existing;
      return existing;
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  const generated = crypto.randomBytes(32).toString("hex");
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.writeFile(AUTH_SECRET_PATH, `${generated}\n`, { encoding: "utf8", mode: 0o600 });
  authSecretCache = generated;
  return generated;
}

export function authSecret(): string {
  if (authSecretCache) return authSecretCache;
  const configured = process.env.POCKET_CISO_SECRET?.trim();
  if (configured && configured.length >= 32) return configured;
  throw new Error("Auth-Secret ist noch nicht initialisiert.");
}

export function applyCredentialPolicy(db: Db): void {
  for (const role of ["L2", "L3"] as const) {
    configuredPassword(role); // throws on missing / too short non-demo passwords
    db.users.find((u) => u.role === role)!.passwordHash = ENV_MANAGED;
  }
}
