# PokerOracle

A Python terminal tool that estimates your **Texas Hold'em equity** at every
street (Pre-Flop → Flop → Turn → River) using Monte Carlo simulation.

---

## Features

| Feature | Details |
|---------|---------|
| **Monte Carlo engine** | Configurable number of iterations (default 50 000) |
| **Hand evaluator** | [`treys`](https://github.com/ihendley/treys) – fast bitwise evaluation |
| **Rich TUI** | Colour-coded results, progress bar, equity bar |
| **Interactive flow** | Stage-by-stage card input with full validation |
| **SQLite persistence** | Results are stored; re-running the same scenario **merges** iterations |

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/Blockburnb/poker.git
cd poker

# 2. (Recommended) Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Usage

```bash
python main.py
```

Additional entry points:

```bash
# Read-only consultation from DB (no Monte Carlo)
python consult_oracle.py

# Automatic River data producer (continuous 1000-iteration batches)
python produce_data.py

# Multi-strategy arena (bot vs bot, round-robin, human vs bot)
python bot_arena.py
```

The tool will guide you step-by-step:

1. **Number of opponents** – how many players you are up against (1–9).
2. **Iterations** – more iterations → higher precision (at the cost of time).
3. **Your hole cards** – e.g. `Ah Kd`.
4. **Pre-Flop equity** is displayed immediately.
5. You are then prompted for the **Flop** (3 cards), **Turn** (1 card) and
   **River** (1 card), with updated equity after each.

### Card format

```
<Rank><Suit>
```

| Rank | 2–9 · T · J · Q · K · A |
|------|--------------------------|
| Suit | `h` (hearts) · `d` (diamonds) · `s` (spades) · `c` (clubs) |

**Examples:** `Ah`  `Kd`  `Ts`  `2c`  `Jh`

---

## Project structure

```
poker/
├── main.py          ← Entry point & interactive flow
├── bot_arena.py     ← TUI arena for strategy comparison
├── arena.py         ← Heads-up engine + round-robin evaluation
├── bots/
│   ├── base.py      ← Strategy interface (decision context)
│   ├── registry.py  ← Strategy registry / factories
│   ├── builtin.py   ← Built-in bot strategies
│   └── human.py     ← Human strategy adapter for TUI play
├── simulator.py     ← Monte Carlo engine (treys)
├── db.py            ← SQLite persistence layer
├── ui.py            ← Rich TUI components
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Bot Arena

`bot_arena.py` gives a single place to view all registered strategies and compare
them quickly in a reproducible way.

Modes:

1. `list` – show all available strategies with summary and tags.
2. `bot` – run one heads-up match between two bots.
3. `rr` – run round-robin over a set of strategies and show ranking by profit.
4. `human` – play Human vs selected bot in the same TUI.

Ranking metric in round-robin:

- `Profit/100`: average chip profit per 100 hands (higher is better).

Default variance-control behavior:

- Round-robin runs with **100 runs** by default.
- Results are accumulated in `bot_league.db`.
- Re-launching and running another tournament **adds** to existing strategy stats.

Built-in strategy profiles:

- `mc10k_75`: Monte Carlo 10,000 simulations at hand draw, plays check/call only when equity >= 75%, else fold.
- `tag`: Tight-Aggressive.
- `lag`: Loose-Aggressive.
- `calling_station`: Calling Station.
- `maniac`: Maniac.
- `random`: Random decisions.

GTO policy comparison (PioSolver / GTO+ / Simple Postflop exports):

- Drop exported JSON policy files into `gto_policies/`.
- Each file is auto-registered as `gto_<filename>` strategy in the arena.
- Use `gto_policies/template_policy.json` as a format reference.

This structure is designed so you can add new bots by implementing the strategy
interface and registering them in `bots/registry.py`.

---

## SQLite persistence

Every simulation is stored in **`poker_oracle.db`** (created automatically in
the current working directory).

### Schema

```sql
CREATE TABLE simulations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hand            TEXT    NOT NULL,        -- e.g. "Ah Kd"
    stage           TEXT    NOT NULL,        -- Pre-Flop | Flop | Turn | River
    community_cards TEXT    NOT NULL DEFAULT '',
    num_opponents   INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    ties            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    total           INTEGER NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
```

### Accumulation logic

A simulation is uniquely identified by the tuple
`(hand, stage, community_cards, num_opponents)`.

- **First run** → a new row is inserted.
- **Subsequent runs** with the *same* key → wins/ties/losses/total are **added**
  to the existing counts, and `updated_at` is refreshed.

This means that running 50 000 iterations twice is equivalent to a single
100 000-iteration run: the equity estimate becomes more precise without
discarding any prior work.

### Inspecting the database manually

```bash
sqlite3 poker_oracle.db
sqlite> SELECT hand, stage, total, ROUND((wins + ties*0.5)*100.0/total, 2) AS equity FROM simulations;
```

## Git sync for stored data

The live SQLite file (`poker_oracle.db`) is intentionally ignored by Git.
To make your simulation history recoverable after cloning/pulling the repo,
the app maintains a versioned JSON snapshot:

- `db_snapshot.json` is exported automatically after each saved simulation.
- On startup, `main.py` imports `db_snapshot.json` into the local SQLite DB
  when matching rows do not already exist.

This gives you a Git-friendly history format while keeping runtime reads/writes
fast in SQLite.

Recommended workflow:

1. Run simulations (`python main.py`).
2. Commit `db_snapshot.json` when you want to share or back up your latest
  accumulated data.
3. After `git clone` or `git pull`, run `python main.py` once to restore
  missing rows into your local `poker_oracle.db`.

Both `consult_oracle.py` and `produce_data.py` also import the snapshot on
startup and export updates when new data is produced.

---

## Development notes

- Tested with Python 3.10+.
- The `treys` evaluator scores hands from **1** (Royal Flush) to **7462**
  (worst High Card) – lower is better.  The simulator counts a hand as a win
  when the player's score is strictly lower than every opponent's score.