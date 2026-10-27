// SERVER ONLY. Document chunker for Markdown/TXT (headings, ≤ 600 chars, table rows as statements; Python is the canonical chunker).
/* ── document chunker (mirrors engine/ingest/chunk.py → chunk_document) ── */

interface DocChunk {
  section: string;
  number: string;
  content: string;
}

const MAX_CHUNK = 600;
const HEAD_MD = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const HEAD_PARA = /^\s*§\s*(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$/;
const HEAD_NUM = /^\s*(\d+(?:\.\d+)*)\.?\s+([A-ZÄÖÜ][^.!?;:]{2,80})\s*$/;
// "2 Stunden", "250.000 EUR", "36 Stunden": KPI values in tables/boxes, not numbered headings (mirrors is_quantity_heading in engine/ingest/chunk.py).
const QUANTITY_TITLE = /^(stunden?|minuten?|tage[n]?|werktage[n]?|wochen?|monate[n]?|jahre[n]?|sekunden?|prozent|zeichen|versuche[n]?|mal|eur|euro|mb|gb|tb)\b/i;
const isQuantityHeading = (number: string, title: string) => /^\d{1,3}(\.\d{3})+$/.test(number) || QUANTITY_TITLE.test(title.trim());
const NUM_TITLE = /^§?\s*(\d+(?:\.\d+)*)\.?\s*[:–-]?\s*(.*)$/;
const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

const splitSentences = (t: string) => t.split(/(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„"(])/).map((x) => x.trim()).filter(Boolean);

function packChunks(paragraphs: string[]): string[] {
  const pieces: string[] = [];
  for (const p of paragraphs) {
    if (p.length <= MAX_CHUNK) {
      pieces.push(p);
      continue;
    }
    let cur = "";
    for (let sent of splitSentences(p).length ? splitSentences(p) : [p]) {
      while (sent.length > MAX_CHUNK) {
        const cut = sent.lastIndexOf(" ", MAX_CHUNK) > 0 ? sent.lastIndexOf(" ", MAX_CHUNK) : MAX_CHUNK;
        pieces.push(sent.slice(0, cut).trim());
        sent = sent.slice(cut).trim();
      }
      if (cur && cur.length + 1 + sent.length > MAX_CHUNK) {
        pieces.push(cur);
        cur = sent;
      } else cur = `${cur} ${sent}`.trim();
    }
    if (cur) pieces.push(cur);
  }
  const chunks: string[] = [];
  let cur = "";
  for (const p of pieces) {
    if (cur && cur.length + 1 + p.length > MAX_CHUNK) {
      chunks.push(cur);
      cur = p;
    } else cur = `${cur} ${p}`.trim();
  }
  if (cur) chunks.push(cur);
  return chunks;
}

function bodyParagraphs(lines: string[]): string[] {
  const paras: string[] = [];
  let buf: string[] = [];
  const flush = () => {
    if (buf.length) paras.push(buf.join(" "));
    buf = [];
  };
  for (const ln of lines) {
    if (!ln.trim()) {
      flush();
      continue;
    }
    let item = ln.replace(BULLET, "").trim();
    if (BULLET.test(ln) && item && !".!?:;".includes(item.at(-1)!)) item += ".";
    buf.push(item);
  }
  flush();
  return paras.map((x) => x.replace(/\s+/g, " ").trim()).filter(Boolean);
}

/* ── table awareness: every Markdown table row becomes a self-contained statement ("Spalte: Wert | …") ── */

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const splitCells = (row: string) =>
  row.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").replace(/\*\*|`/g, "").trim());

function tableStatements(rows: string[], label: string): string[] {
  const hasHeader = rows.length >= 2 && TABLE_SEP.test(rows[1]);
  const header = hasHeader ? splitCells(rows[0]) : [];
  const body = hasHeader ? rows.slice(2) : rows;
  const out: string[] = [];
  for (const row of body) {
    if (TABLE_SEP.test(row)) continue;
    const pairs: string[] = [];
    splitCells(row).forEach((cell, i) => {
      if (!cell) return;
      pairs.push(header[i] ? `${header[i]}: ${cell}` : cell);
    });
    if (pairs.length) out.push(`Tabelle [${label}]: ${pairs.join(" | ").replace(/[.;]+$/, "")}.`);
  }
  return out;
}

// Body lines of one section → paragraphs; table blocks are converted row by row, in document order.
function sectionParagraphs(lines: string[], label: string): string[] {
  const paras: string[] = [];
  let text: string[] = [];
  let table: string[] = [];
  const flushText = () => {
    paras.push(...bodyParagraphs(text));
    text = [];
  };
  const flushTable = () => {
    paras.push(...tableStatements(table, label));
    table = [];
  };
  for (const ln of lines) {
    if (TABLE_ROW.test(ln)) {
      flushText();
      table.push(ln);
      continue;
    }
    flushTable();
    text.push(ln);
  }
  flushText();
  flushTable();
  return paras;
}

export function chunkDocument(raw: string): { title: string | null; chunks: DocChunk[] } {
  const text = raw.replace(/\r\n?/g, "\n").replace(/\0/g, "");
  let title: string | null = null;
  const sections: { number: string; title: string; lines: string[] }[] = [];
  let cur: (typeof sections)[number] | null = null;
  const pre: string[] = [];
  const start = (rawTitle: string, number?: string) => {
    let t = rawTitle;
    let n = number;
    const m = NUM_TITLE.exec(t);
    if (n === undefined && m) {
      n = m[1];
      t = m[2];
    }
    cur = { number: n ?? String(sections.length + 1), title: t.replace(/^[\s#*:]+|[\s#*:]+$/g, "") || "Abschnitt", lines: [] };
    sections.push(cur);
  };
  for (const line of text.split("\n")) {
    const md = HEAD_MD.exec(line);
    if (md) {
      if (md[1].length === 1 && title === null && !sections.length) {
        title = md[2].trim();
        continue;
      }
      start(md[2].trim());
      continue;
    }
    const pm = HEAD_PARA.exec(line);
    if (pm) {
      start(pm[2], pm[1]);
      continue;
    }
    const nm = HEAD_NUM.exec(line);
    if (nm && !isQuantityHeading(nm[1], nm[2])) {
      start(nm[2], nm[1]);
      continue;
    }
    (cur ? (cur as (typeof sections)[number]).lines : pre).push(line);
  }
  if (pre.reduce((n, x) => n + x.trim().length, 0) >= (sections.length ? 40 : 15)) sections.unshift({ number: "0", title: "Vorbemerkung", lines: pre });

  const chunks: DocChunk[] = [];
  for (const sec of sections) {
    const parts = packChunks(sectionParagraphs(sec.lines, `§${sec.number} ${sec.title}`)).filter((p) => p.length >= 15);
    parts.forEach((content, i) => chunks.push({ section: `§${sec.number} ${sec.title}${i ? ` (Teil ${i + 1})` : ""}`, number: sec.number, content }));
  }
  return { title, chunks };
}
