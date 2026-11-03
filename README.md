# Mnemo — a note app whose LLM decides where each note goes

One input box. Paste text, a link, or an image; the note files itself. When you
want to steer it, say so in the same box: `@gaming this build is broken`.

```bash
python -m mnemo serve
```

Everything — the runtime, the model weights, the index, your notes — lives
inside this folder. Nothing is written to your home directory, AppData, or any
absolute path. Copy the folder to another machine or another user account, run
setup, and it works unchanged.

---

## Install

Needs **Python 3.10+** and nothing else.

```bash
python -m mnemo serve
```

That already works, with no models and no downloads — see *Two modes* below.
For the better stack:

```bash
python -m mnemo.setup
```

Downloads ~5 GB into `vendor/`: the portable Ollama runtime (1.5 GB) and the
models (~3.6 GB). Then:

```bash
python -m mnemo doctor
```

On macOS or Linux, `sh bootstrap.sh` does all of it; on Windows,
`powershell -ExecutionPolicy Bypass -File bootstrap.ps1`.

On Windows, `start-notes.bat` starts the app, opens it, and shows a QR code your
phone can scan to use it over Wi-Fi. Put a shortcut to it on your desktop. If
the app is already running, it opens it and shows the QR code again.

---

## Two modes, and why the offline one is real

| | Offline (default until setup runs) | Local models |
|---|---|---|
| Downloads | **none** | ~5 GB, one time |
| Embeddings | character n-gram hashing | `nomic-embed-text` |
| Router | pure retrieval | `gemma3:4b`, JSON-constrained |
| Images | filename and context only | described by the vision model |
| Synonyms | lexical only — `car` ≠ `automobile` | genuinely semantic |

The offline path is a real fallback, not a stub: it routes at **98.8% top-1
accuracy** on the benchmark corpus, and every guardrail works without a model.
Capture never depends on a model being alive — if Ollama is down or missing,
notes still land and route later.

`vendor/` is gitignored. The repo stays small; `python -m mnemo.setup`
reconstitutes it anywhere.

---

## Measured results

`python -m mnemo.evaluate 500`, `--online` for the second column, and
`python -m mnemo.calibrate` reproduce all of it.

| | offline (no downloads) | with `nomic-embed-text` |
|---|---|---|
| Topic retrieval, held-out notes vs centroids | top-1 **98.8%** | top-1 **99.2%**, top-3 100% |
| Taxonomy size, 500 notes / 10 true topics | **12** (budget 10 ± 2) | **12** |
| Cluster purity | 76.4% | **82.0%** |
| Gardener false merges | **0** | **0** |
| Embed latency, single note | 0.4 ms | **22–47 ms** |
| `car` vs `automobile` | 0.000 | **0.914** |

**78 tests passing** (`MNEMO_SLOW=1` adds the 500-note stress run).

The offline column uses **no LLM at all** — retrieval and string normalisation
do the work. Real embeddings mainly buy semantics: synonyms that share no
characters, which is the one thing feature hashing can never do.

### Why the Inbox rate is ~39% and should stay there

That looks like a failure until you measure the alternative. Sweeping
`file_floor` over 500 notes:

| `file_floor` | topics | purity | inbox |
|---|---|---|---|
| **0.656** (calibrated) | 12 | **77.6%** | 178 |
| 0.630 | 14 | 64.2% | 101 |
| 0.600 | 14 | 57.9% | 101 |
| 0.560 | 7 | **19.8%** | 19 |

Loosening the floor to empty the Inbox collapses purity to chance (1/7 topics).
A note in Inbox is one click from filed; a misfiled note is invisible until you
go looking for it. The high Inbox rate is the system declining to guess.

---

## The design

**The graph is the truth; folders are a projection.** Topic nodes with `parent`
edges form the hierarchy you browse and the directories on disk. But `kind`
(link/video/article/task/…) and `status` are *attributes*, not folders — so
"Gaming ▸ YouTube" is a live query, not a directory. One video about an AI
coding tool shows up under `Technology/AI` and under every relevant type view
without being duplicated anywhere.

**Notes are plain Markdown.** Real `.md` files in real folders, readable in
Obsidian, VS Code or Notepad. `index.db` is a cache: `mnemo rebuild` throws it
away and reconstructs the whole graph from frontmatter alone — a tested
guarantee, not an aspiration.

### The six-check gate

Letting an LLM create folders freely is easy. Doing it without ending up with
`Gaming`, `Games`, `Video Games`, `Gaming Stuff` and `PC Gaming` — five folders,
one topic — is the actual engineering problem. A `create_topic` is a request,
not a command, and cheapest checks run first so most duplicates never reach an
embedding comparison:

1. **alias** — a synonym refused once resolves instantly forever after
2. **sibling** — exact slug match under the same parent
3. **norm_key** — `Games`, `Gamer`, `My Games`, `Gaming Stuff` all reduce to the
   key `gam` and collapse into the existing `Gaming`
4. **near_key** — prefix-aware token overlap: `Web Dev` ~ `Web Development`
5. **embedding, two axes** — proposed name vs topic *names*, and vs topic
   *content centroids*, each with its own threshold
6. **shape** — depth ≤ 3, ≤ 12 children per parent

Containment means **nesting, not merging**: `Gaming Hardware` becomes a child of
`Gaming`, never a rival root folder.

Survivors are created **provisional** — real in the graph, but with *no folder
on disk* until 3 notes accumulate. That is what stops thirty folders each
holding one note.

### Cold start

An empty vault has no centroids to retrieve against, so naming a topic from one
note's incidental wording produces junk like "Covering Deduction". Instead the
first 20 notes queue in Inbox, then a single clustering pass derives the
starting taxonomy from real data — and names each cluster from the terms its
members *share*. Explicit `@steering` always overrides this.

### Nothing is one-way

Every organisational mutation writes a journal row carrying its own inverse, so
any action — including a gardener merge from last week — is undoable.

### Things that were measured, not assumed

- The plan specified a **0.86** dedupe threshold. Correct for a trained
  embedder, badly wrong here: measured same-topic pairs average 0.45,
  different-topic 0.05. `mnemo/calibrate.py` derives thresholds from data.
- The gate's two embedding axes needed **separate** thresholds. For a true
  duplicate, similarity to a topic's *name* has median 0.672 but to its *content
  centroid* only 0.077 — lower than the 0.216 unrelated names reach. One shared
  threshold would either miss every duplicate or merge unrelated topics.
- The merge threshold (0.28) came from a real 200-note vault: true duplicate
  pairs scored 0.18–0.41, distinct pairs topped out at 0.23. It catches 3 of 4
  real duplicates with **zero false merges** — a false merge destroys structure,
  a missed one just leaves a folder for the next pass.
- The threshold *deriver itself* was wrong. It multiplied the noise floor, which
  works when unrelated pairs sit near 0.05 but produced a dedupe threshold of
  **1.97** for nomic, whose unrelated pairs sit near 0.56. Cosine cannot exceed
  1.0, so the gate would have silently stopped refusing anything. It now
  interpolates between the two measured distributions and is tested against
  low-floor, high-floor and overlapping embedders.
- `nomic-embed-text` documents task prefixes (`search_document:` /
  `search_query:`). Measured, they made separation **worse** here (+0.153 vs
  +0.208 with no prefix), so the code does not use them.
- Embedding calls omitted `keep_alive`, so Ollama unloaded the model on its own
  timer and every capture paid a reload: **1,709 ms per note**. Pinning it
  brought that to **22–47 ms**, a ~50x difference that no amount of prompt or
  threshold tuning would have found.
- Confidence is **margin-based**, not an absolute cutoff. Absolute cosine is
  scale-dependent; "the winner beat the runner-up clearly" means the same thing
  on any scale.

---

## Using it

The GUI is the main surface: capture bar at the top, topic tree with auto-views
on the left, notes in the middle, the note on the right. `Ctrl+K` focuses
capture, `?` at the start of the box searches, `Ctrl+Z` undoes.

There is a full CLI too:

```bash
python -m mnemo add '@"Gaming/Elden Ring" malenia waterfowl timing'
```

```bash
python -m mnemo tree --views
```

```bash
python -m mnemo find "chunk documents for retrieval"
```

Also: `inbox`, `garden`, `move <id> <topic>`, `undo`, `rebuild`, `status`,
`doctor`, `setup`. `--offline` forces the dependency-free stack; `--online`
requires the models and fails if they are missing.

**Capture syntax:** `@topic` or `@"Topic/With Spaces"` to steer, `#tag`,
`!task`. A bare `@token` that names an existing topic is extended greedily, so
`@Gaming/Elden Ring notes` resolves once that topic exists.

### Tests

```bash
python -m unittest discover -s tests
```

```bash
MNEMO_SLOW=1 python -m unittest discover -s tests
```

---

## Honest limitations

- **Offline embeddings are lexical.** `car` and `automobile` score 0.000. ~25%
  of true synonym pairs (`Souls Game` for `Elden Ring`) share no characters and
  the gate cannot catch them until the gardener's merge pass. Running
  `python -m mnemo.setup` fixes this: with real embeddings the same pair scores
  0.914, and a true duplicate topic name scores p25 0.647 where offline scored
  p25 0.000.
- **Machine-generated topic names are functional, not elegant** — `Banneton
  Proofing` rather than `Sourdough`. The maths finds the structure; naming is
  precisely what the router model is for, and is the main thing still missing
  until `gemma3:4b` finishes downloading.
- **Inbox rate is ~39%** on the synthetic corpus, and that is deliberate — see
  the sweep above. Those notes are *unfiled*, not misfiled.
- **No global hotkey or system tray.** The GUI is a local web app, which is what
  makes it run anywhere with only Python. A native window is a small addition
  (`pywebview`) if that trade stops being worth it.
- Run-to-run results vary slightly: ULIDs are random, so query tie-breaking
  differs. Tests assert on ranges, not exact values.

---

## Security notes

The server binds to loopback **and** validates `Host` and `Origin` on every
request. A loopback bind alone is not enough: any web page you visit can issue
requests to `127.0.0.1`, and DNS rebinding defeats a naive host check. This
server can read and write your notes, so both guards are tested
(`tests/test_server.py`).

Pasted links are fetched to extract a title and readable text — that is what
makes a link route as well as a typed note. `--offline`, or `Engine(enrich=False)`,
disables all outbound fetching.

---

## Layout

```
mnemo/
  schema.sql     tables; index.db is a rebuildable cache of the .md files
  paths.py       every path, resolved relative to this repo
  setup.py       downloads runtime + models into vendor/
  runtime.py     starts the bundled Ollama with OLLAMA_MODELS inside the repo
  models.py      which models, and why those for 6 GB of VRAM
  doctor.py      verifies the model stack really behaves as assumed
  db.py          ULIDs and the name normalisation the gate leans on
  vecs.py        embedder interface, offline hashing embedder, brute-force cosine
  vault.py       Markdown frontmatter, path projection, Windows-safe names
  topics.py      the topic tree, centroids, aliases
  gate.py        the six-check anti-sprawl gate
  router.py      retrieve -> decide -> validate -> gate -> tier
  llm.py         provider interface, OfflineRouter, OllamaRouter
  enrich.py      link fetching and image captioning
  gardener.py    k-means, merge/split/reparent/dissolve proposals
  engine.py      capture pipeline, cold start, bootstrap, undo, rebuild
  search.py      FTS5 + vector, fused with Reciprocal Rank Fusion
  server.py      local HTTP API for the GUI
  web/app.html   the entire GUI: one file, no build step
  calibrate.py   derives thresholds from measured distributions
  evaluate.py    purity / coverage / sprawl metrics
  corpus.py      synthetic corpus with known ground truth
  cli.py         add · tree · find · inbox · garden · serve · undo · rebuild
tests/
  test_core.py    core logic, vault, gate, portability
  test_server.py  web API and its security guards
  test_sprawl.py  the anti-sprawl stress test
vendor/          gitignored; created by `python -m mnemo.setup`
profiles/        gitignored; your notes, one folder per profile
backups/         gitignored; daily zips of the first profile
```
