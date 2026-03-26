# Poker Strategy Arena

Terminal project focused on comparing poker strategies in a reproducible arena.

## What Remains

- Strategy system (`bots/`) with current tournament strategies, including:
  - `mc10k_75`
  - `mc10k_51`
  - `tag`
  - `lag`
  - `calling_station`
  - `maniac`
  - `random`
  - `human`
- GTO external policy integration via `gto_policies/*.json`
- Arena engine (`arena.py`) for:
  - bot vs bot
  - round-robin tournaments
  - human vs bot
- Unified TUI entrypoint (`main.py`) with two modes:
  - `arena`
  - `oracle` (live Monte Carlo consultation, no DB persistence)

## Removed from Runtime

The previous data pipeline and DB-backed oracle flow were removed:

- data producers
- old oracle scripts
- simulation DB layer
- benchmark tooling

## Run

```bash
python main.py
```

Then choose:

- `arena` for strategy tournaments
- `oracle` for interactive Monte Carlo consultation

## GTO Policies

Drop JSON files in `gto_policies/` using the template format:

- `gto_policies/template_policy.json`

Each policy is auto-registered as `gto_<filename>` in the arena strategy list.

## Project Structure

```text
poker/
├── main.py
├── oracle_mode.py
├── bot_arena.py
├── arena.py
├── league_store.py
├── simulator.py
├── ui.py
├── bots/
│   ├── base.py
│   ├── builtin.py
│   ├── gto.py
│   ├── human.py
│   └── registry.py
├── gto_policies/
│   └── template_policy.json
└── requirements.txt
```
