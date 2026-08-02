# Setup on a new machine

Assumes the repo lives at `C:\momentum-tracker`. If you put it elsewhere,
substitute that path everywhere below — the path appears in Task
Scheduler's **Start in** field and nowhere else.

## 1. Prerequisites

- **Python 3.11 or newer** (3.13 recommended — `pandas 3.0` requires ≥3.11)
- Git

## 2. Clone and build

```
cd C:\
git clone https://github.com/ranjankai/momentummori.git momentum-tracker
cd momentum-tracker
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
```

Expect `24 passed`. Do **not** copy `.venv` from another machine — its
paths are baked in at creation and it will fail confusingly.

## 3. Copy the gitignored files

| from the old machine | to the new one | needed? |
|---|---|---|
| `.env` | repo root | **Required.** Nothing sends without it. |
| `data\cache\` | `data\cache\` | Strongly recommended (~500 MB). Without it the first run re-downloads years of bhavcopy. |
| `data\llm_cache\` | `data\llm_cache\` | Recommended. Avoids re-paying for corporate-action classifications. |
| `data\signals_cache\` | `data\signals_cache\` | Optional, regenerates quickly. |
| `logs\` | — | Don't bother. |

## 4. Verify by hand before scheduling

```
cd C:\momentum-tracker
.venv\Scripts\python.exe run_strategy.py daily --no-send
```

Prints the note without sending. If this fails, Task Scheduler will too.

## 5. Task Scheduler

Two tasks. Both are triggered **daily** — the scripts decide among
themselves which evening does what:

- `daily` stands down on weekends, exchange holidays, and expiry evenings
- `sheet` stands down on every day that is not the monthly expiry

So neither needs a date maintained, ever.

### Task 1 — Momentum Daily

1. `Win` → **Task Scheduler** → right pane → **Create Task…**
   (*not* "Create Basic Task")
2. **General**
   - Name: `Momentum Daily`
   - Leave **Run only when user is logged on** selected
   - Tick **Run with highest privileges**
3. **Triggers** → **New…** → Daily, start `19:30:00`, recur every `1` days → OK
4. **Actions** → **New…** → *Start a program*
   - Program/script: `C:\momentum-tracker\.venv\Scripts\python.exe`
   - Add arguments: `run_strategy.py daily`
   - Start in: `C:\momentum-tracker`  ← **no quotes**, Windows rejects them silently
5. **Conditions** → untick *Start the task only if the computer is on AC power*
6. **Settings** → tick *Run task as soon as possible after a scheduled start is missed*
7. **OK**

### Task 2 — Momentum Expiry Sheet

Identical, except:

- Name: `Momentum Expiry Sheet`
- Trigger time: `19:45:00` (still **Daily**)
- Add arguments: `run_strategy.py sheet`

## 6. Test

Right-click each task → **Run**.

- `Momentum Daily` → a note should reach Telegram in ~30s
- `Momentum Expiry Sheet` → **nothing should arrive** unless today is the
  monthly expiry. Silence is the correct result.

`Last Run Result` of `0x0` means success. Anything else → `logs\app.log`.

## 7. Turn the old machine off

Only ONE machine may run these tasks. Two runners means duplicate
Telegram messages and both machines appending to `data\ledger.jsonl`,
which is tracked in git — merge conflicts on an append-only audit trail
are painful to unpick.

Disable both tasks on the old machine before enabling them here.

## Troubleshooting

**Task reports success but nothing arrives.** Almost always a wrong
**Start in**: the script cannot find `.env`, so the Telegram credentials
are empty. Confirm by running step 4 by hand from that directory.

**`0x1` result.** Open `logs\app.log`. A failure inside the run also
sends a Telegram failure alert, so silence plus `0x1` points at a
startup problem (wrong interpreter path, missing venv).

**Nothing on Saturdays or holidays.** Correct behaviour, not a fault.
