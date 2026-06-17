#!/usr/bin/env node
/**
 * obsidian-mcp-server/server.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Universal Obsidian Knowledge MCP Server
 * Transports:  MCP StreamableHTTP  →  POST /mcp        (Claude, MCP clients)
 *              REST API             →  GET  /api/*      (ChatGPT, Gemini, curl)
 *              Relay MD webhook     →  POST /sync       (Obsidian plugin push)
 * Storage:     SQLite + FTS5 (full-text search)
 * Auth:        Bearer token (RELAY_SECRET env var)
 * Deploy:      Railway free tier — see Dockerfile + railway.toml
 * ─────────────────────────────────────────────────────────────────────────────
 */

import express                         from 'express';
import Database                        from 'better-sqlite3';
import { McpServer }                   from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z }                           from 'zod';
import crypto                          from 'node:crypto';

// ── Config ────────────────────────────────────────────────────────────────────
const PORT     = process.env.PORT          || 3000;
const TOKEN    = process.env.RELAY_SECRET  || 'changeme-set-in-railway-env';
const DB_PATH  = process.env.DB_PATH       || '/data/obsidian.db';

// ── Database ──────────────────────────────────────────────────────────────────
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path  TEXT    UNIQUE NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    checksum   TEXT,
    synced_at  TEXT    DEFAULT (datetime('now'))
  );

  -- FTS5 virtual table for full-text search
  CREATE VIRTUAL TABLE IF NOT EXISTS fts
    USING fts5(
      file_path UNINDEXED,
      content,
      tokenize = 'unicode61',
      content  = files,
      content_rowid = id
    );

  -- Keep FTS in sync with base table
  CREATE TRIGGER IF NOT EXISTS trg_ai AFTER INSERT ON files BEGIN
    INSERT INTO fts(rowid, file_path, content)
    VALUES (new.id, new.file_path, new.content);
  END;
  CREATE TRIGGER IF NOT EXISTS trg_au AFTER UPDATE ON files BEGIN
    INSERT INTO fts(fts, rowid, file_path, content)
    VALUES ('delete', old.id, old.file_path, old.content);
    INSERT INTO fts(rowid, file_path, content)
    VALUES (new.id, new.file_path, new.content);
  END;
  CREATE TRIGGER IF NOT EXISTS trg_ad AFTER DELETE ON files BEGIN
    INSERT INTO fts(fts, rowid, file_path, content)
    VALUES ('delete', old.id, old.file_path, old.content);
  END;
`);

// Prepared statements (compiled once, reused)
const q = {
  upsert: db.prepare(`
    INSERT INTO files (file_path, content, checksum, synced_at)
    VALUES (@path, @content, @checksum, datetime('now'))
    ON CONFLICT(file_path) DO UPDATE SET
      content   = excluded.content,
      checksum  = excluded.checksum,
      synced_at = excluded.synced_at
    WHERE excluded.checksum != files.checksum
  `),
  del:    db.prepare(`DELETE FROM files WHERE file_path = ?`),
  get:    db.prepare(`SELECT content, synced_at FROM files WHERE file_path = ?`),
  list:   db.prepare(`
    SELECT file_path, synced_at
    FROM   files
    WHERE  file_path LIKE ?
    ORDER BY file_path
  `),
  search: db.prepare(`
    SELECT file_path,
           snippet(fts, 1, '**', '**', ' … ', 28) AS snippet
    FROM   fts
    WHERE  fts MATCH ?
    LIMIT  15
  `),
  count:  db.prepare(`SELECT COUNT(*) AS n FROM files`),
  map:    db.prepare(`
    SELECT content FROM files
    WHERE  file_path LIKE '%000_MAP%'
        OR file_path LIKE '%_MAP.md'
    ORDER BY length(file_path)
    LIMIT 1
  `),
};

// ── MCP tool factory (stateless: new instance per HTTP request) ───────────────
function buildMcpServer() {
  const s = new McpServer({ name: 'obsidian-knowledge', version: '1.0.0' });

  // ── read_file ──────────────────────────────────────────────────────────────
  s.tool(
    'read_file',
    {
      path: z.string().describe(
        'Vault-relative path, e.g. "C3. 논문/intro.md"'
      ),
    },
    ({ path: p }) => {
      const row = q.get.get(p);
      return row
        ? { content: [{ type: 'text', text: `# ${p}\n_synced: ${row.synced_at}_\n\n${row.content}` }] }
        : { content: [{ type: 'text', text: `❌ Not found: ${p}` }] };
    }
  );

  // ── list_files ─────────────────────────────────────────────────────────────
  s.tool(
    'list_files',
    {
      folder: z.string().optional().describe(
        'Folder prefix to filter. Leave empty to list all files.'
      ),
    },
    ({ folder = '' }) => {
      const rows = q.list.all(`${folder}%`);
      const txt  = rows.length
        ? rows.map(r => `- ${r.file_path}  (${r.synced_at})`).join('\n')
        : '(no files synced yet — run sync_all.py first)';
      return { content: [{ type: 'text', text: txt }] };
    }
  );

  // ── search ─────────────────────────────────────────────────────────────────
  s.tool(
    'search',
    {
      query: z.string().describe(
        'FTS5 query. Examples: "SSCI manuscript", "HARD LAW 8", "2026-06-17"'
      ),
    },
    ({ query }) => {
      const rows = q.search.all(query);
      const txt  = rows.length
        ? rows.map(r => `**${r.file_path}**\n${r.snippet}`).join('\n\n---\n\n')
        : 'No results.';
      return { content: [{ type: 'text', text: txt }] };
    }
  );

  // ── get_map ────────────────────────────────────────────────────────────────
  s.tool(
    'get_map',
    {},
    () => {
      const row = q.map.get();
      return {
        content: [{
          type: 'text',
          text: row?.content ?? '⚠️  000_MAP.md not found. Run sync_all.py first.',
        }],
      };
    }
  );

  return s;
}

// ── Express app ───────────────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '20mb' }));

// Auth middleware
const auth = (req, res, next) => {
  const via_bearer = req.headers.authorization === `Bearer ${TOKEN}`;
  const via_custom  = req.headers['x-relay-secret'] === TOKEN;
  const via_query   = req.query.token === TOKEN;
  return (via_bearer || via_custom || via_query)
    ? next()
    : res.status(401).json({ error: 'Unauthorized' });
};

// ── Health (public) ───────────────────────────────────────────────────────────
app.get('/health', (_req, res) =>
  res.json({ status: 'ok', files: q.count.get().n, ts: new Date().toISOString() })
);

// ── MCP endpoint (Claude Code, Anthropic clients, MCP-compatible tools) ───────
async function mcpHandler(req, res) {
  const server    = buildMcpServer();
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  res.on('close', () => { transport.close(); server.close(); });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
}
app.post('/mcp',   auth, mcpHandler);
app.get('/mcp',    auth, mcpHandler);
app.delete('/mcp', auth, (_req, res) => res.status(200).end());

// ── Relay MD sync webhook ─────────────────────────────────────────────────────
// Expected body: { files: [{ path: "...", content: "..." } | { path: "...", deleted: true }] }
app.post('/sync', auth, (req, res) => {
  const { files } = req.body ?? {};
  if (!Array.isArray(files)) {
    return res.status(400).json({ error: 'Expected { files: [{path, content}] }' });
  }

  const run = db.transaction(files => {
    let synced = 0, deleted = 0, skipped = 0;
    for (const f of files) {
      if (!f.path) continue;
      if (f.deleted) {
        q.del.run(f.path);
        deleted++;
      } else {
        const hash = crypto
          .createHash('sha256')
          .update(f.content ?? '')
          .digest('hex')
          .slice(0, 16);
        const info = q.upsert.run({ path: f.path, content: f.content ?? '', checksum: hash });
        info.changes ? synced++ : skipped++;
      }
    }
    return { synced, deleted, skipped };
  });

  res.json({ ok: true, ...run(files) });
});

// ── REST API (ChatGPT function calls, Gemini, curl) ───────────────────────────
app.get('/api/map', auth, (_req, res) => {
  const row = q.map.get();
  row ? res.json(row) : res.status(404).json({ error: '000_MAP.md not synced' });
});

app.get('/api/files', auth, (req, res) => {
  res.json(q.list.all(`${req.query.folder ?? ''}%`));
});

// Dynamic segment — must come after /api/files
app.get('/api/files/*path', auth, (req, res) => {
  const row = q.get.get(req.params.path);
  row ? res.json(row) : res.status(404).json({ error: 'Not found' });
});

app.get('/api/search', auth, (req, res) => {
  if (!req.query.q) return res.status(400).json({ error: 'Missing ?q=' });
  res.json(q.search.all(req.query.q));
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`🧠 Obsidian MCP Server`);
  console.log(`   port  : ${PORT}`);
  console.log(`   db    : ${DB_PATH}`);
  console.log(`   files : ${q.count.get().n}`);
  console.log(`   MCP   : POST /mcp`);
  console.log(`   sync  : POST /sync`);
  console.log(`   REST  : GET  /api/files | /api/search | /api/map`);
});
