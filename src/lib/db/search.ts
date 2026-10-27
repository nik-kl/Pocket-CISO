// SERVER ONLY. Local keyword engine for Pipeline B: search hits only (never composed answers), same texts as the Python engine.
import { readFileSync } from "fs";
import path from "path";
import { ROLE_RANK, type AnswerSegment, type LiveChunk, type QueryResult } from "../mock-store";
import { actorFor, appendDenial } from "./auth";
import { transact } from "./store";
import { R, fail } from "./types";

/* ───────────────────────── Pipeline B: stateless retrieval ───────────────────────── */

export const norm = (s: string) =>
  s.toLowerCase().replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u").replace(/ß/g, "ss");
export const tokenize = (s: string) => norm(s).split(/[^a-z0-9]+/).filter(Boolean);

export const STOP = new Set(
  ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des " +
    "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn " +
    "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese eines einem einer neue neuen neues dafür dafuer laut " +
    "a an and are as at be been being but by can does for from have how i if in is it may my of on or our the their them these they this to us was we what who with").split(" ")
);

// General domain glossary shared with the Python engine (engine/glossary.json): German/English pairs and abbreviations.
// Each single-word member expands to the other single-word members of its group (no question-specific hints).
let glossaryCache: Map<string, string[]> | null = null;
function glossary(): Map<string, string[]> {
  if (glossaryCache) return glossaryCache;
  const map = new Map<string, string[]>();
  try {
    const raw = JSON.parse(readFileSync(path.join(process.cwd(), "engine", "glossary.json"), "utf8")) as { groups?: string[][] };
    for (const group of raw.groups ?? []) {
      const words = group.map((m) => tokenize(m)).filter((t) => t.length === 1).map((t) => t[0]);
      for (const w of words) map.set(w, [...new Set([...(map.get(w) ?? []), ...words.filter((x) => x !== w)])]);
    }
  } catch {
    // no glossary → plain keyword matching
  }
  glossaryCache = map;
  return map;
}

const tokMatch = (a: string, b: string) => a === b || (a.length >= 7 && b.length >= 7 && a.slice(0, 7) === b.slice(0, 7));

function queryGroups(q: string): string[][] {
  const seen = new Set<string>();
  const groups: string[][] = [];
  for (const t of tokenize(q)) {
    const key = t.length >= 7 ? t.slice(0, 7) : t;
    if (t.length < 3 || STOP.has(t) || /^\d+$/.test(t) || seen.has(key)) continue;
    seen.add(key);
    groups.push([t, ...(glossary().get(t) ?? [])]);
  }
  return groups;
}

function coverage(groups: string[][], c: LiveChunk): number {
  if (!groups.length) return 0;
  const toks = tokenize(`${c.docName} ${c.section} ${c.content}`);
  return groups.filter((grp) => grp.some((t) => toks.some((x) => tokMatch(t, x)))).length / groups.length;
}

const NO_ANSWER = "Keine belegbare Antwort im freigegebenen Bestand."; // same text as engine/answer/gate.py
const SEARCH_LABEL = "Fundstellen, keine KI-Antwort";
export const LOCAL_ENGINE = "Local Fallback Engine (keyword)";
const ms = (t0: number) => Math.round((performance.now() - t0) * 10) / 10;

export function runQuery(token: string | null, question: string): Promise<R<{ result: QueryResult }>> {
  return transact((db) => {
    const tStart = performance.now();
    const q = typeof question === "string" ? question.trim() : "";
    if (q.length < 3 || q.length > 500) return fail(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.");
    const a = actorFor(db, token);

    // 1 — isolated rewrite (no history exists in this function by design)
    const groups = queryGroups(q);
    const rewrite = `Welche freigegebene Richtlinie regelt: „${q}“?${groups.length ? ` [Suchbegriffe: ${groups.map((x) => x[0]).join(", ")}]` : ""}`;
    const tRewrite = ms(tStart);

    // 2 — RBAC pre-filter before ranking
    const t2 = performance.now();
    const scored = db.live_chunks.map((c) => ({ c, cov: coverage(groups, c) }));
    const accessible = scored.filter((s) => ROLE_RANK[s.c.minRole] <= ROLE_RANK[a.role]).sort((x, y) => y.cov - x.cov);
    const restricted = scored.filter((s) => ROLE_RANK[s.c.minRole] > ROLE_RANK[a.role]).sort((x, y) => y.cov - x.cov);
    const tRbac = ms(t2);

    // 3 — retrieval + threshold gate
    const t3 = performance.now();
    const best = accessible[0];
    const coverageThreshold = groups.length >= 8 ? Math.max(0.12, Math.min(0.6, 2 / groups.length)) : 0.6;
    const tRetrieval = ms(t3);
    const base = {
      original: q, rewrite, rbacFilter: `min_role <= ${a.role}`, searched: accessible.length, total: db.live_chunks.length, temperature: 0,
      engineMode: LOCAL_ENGINE, rewriteBy: "rules", topScore: Math.round((best?.cov ?? 0) * 100) / 100,
    };
    const lat = (generation: number, nli: number) => ({ rewrite: tRewrite, rbac: tRbac, retrieval: tRetrieval, generation, nli, total: ms(tStart) });

    if (best && best.cov >= coverageThreshold) {
      // 4 — search hits only: this engine never composes an answer (the Python engine does, with a verifier)
      const t4 = performance.now();
      const top = accessible.filter((s) => s.cov >= coverageThreshold && s.cov >= best.cov - 0.15).slice(0, 3);
      const segments: AnswerSegment[] = [{ type: "text", text: `${SEARCH_LABEL}:` }];
      const citations: Record<string, LiveChunk> = {};
      const scores: Record<string, number> = {};
      // table rows as the Python engine shows them: no "Tabelle [§…]:" chunker label, "Spalte: Wert; Spalte: Wert"
      const readable = (t: string) => t.replace(/Tabelle \[[^\]]*\]:\s*/g, "").split(" | ").join("; ");
      top.forEach((s) => {
        segments.push({ type: "text", text: ` ${s.c.docName} ${s.c.section.split(" ")[0]}: ${readable(s.c.content)} ` }, { type: "cite", chunkId: s.c.id });
        citations[s.c.id] = s.c;
        scores[s.c.id] = Math.round(s.cov * 100) / 100;
      });
      const hits = top.map((s) => ({ chunkId: s.c.id, docName: s.c.docName, section: s.c.section, content: readable(s.c.content) }));
      const tGen = ms(t4);
      return { ok: true as const, result: { kind: "search" as const, requiredRole: null, segments, citations, scores, hits, trace: { ...base, nliVerified: false, verification: "none" as const, confidence: Math.round(best.cov * 100) / 100, retrievalScore: Math.round(best.cov * 100) / 100, lexicalSupport: Math.round(best.cov * 100) / 100, answerBy: "search", latencyMs: lat(tGen, 0) } } };
    }

    if (restricted[0] && restricted[0].cov >= coverageThreshold) {
      const need = restricted[0].c.minRole;
      appendDenial(db, { actor: a.email, role: a.role, action: "RBAC_DENIED", target: restricted[0].c.docName, detail: `Retrieval erfordert ${need}` });
      return {
        ok: true as const,
        result: {
          kind: "fallback" as const, requiredRole: null, // indistinguishable from "no match" (no existence oracle)
          segments: [{ type: "text" as const, text: NO_ANSWER }],
          citations: {}, scores: {}, trace: { ...base, topScore: 0, nliVerified: false, confidence: 0, latencyMs: lat(0, 0) },
        },
      };
    }

    return {
      ok: true as const,
      result: {
        kind: "fallback" as const, requiredRole: null,
        segments: [{ type: "text" as const, text: NO_ANSWER }],
        citations: {}, scores: {}, trace: { ...base, nliVerified: false, verification: "none" as const, confidence: Math.round((best?.cov ?? 0) * 100) / 100, retrievalScore: Math.round((best?.cov ?? 0) * 100) / 100, latencyMs: lat(0, 0) },
      },
    };
  });
}
