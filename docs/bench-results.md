# Model benchmark

Run 2026-10-26 00:43 on AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD. Ollama 0.33.2.
Scores are passes out of cases. See `tests/bench/` for the cases and `mnemo/bench.py` for how each one is judged.

## Brains

| Model | Note AI | Chat answers | Honest "don't know" | Tools | Picture text | First token | Tokens/s | Load | VRAM (on GPU) |
|---|---|---|---|---|---|---|---|---|---|
| gemma3:4b | 27/29 | 15/15 | 8/8 | – | 100% / 100% | 0.08 s | 78.1 | 5.5 s | 3.67 GB (100%) |
| qwen3.5:4b | 27/29 | 15/15 | 8/8 | 16/16 | 92% / 100% | 0.14 s | 55.1 | 6.2 s | 3.67 GB (100%) |
| gemma4:e4b | 29/29 | 15/15 | 8/8 | 16/16 | 100% / 100% | 0.24 s | 32.9 | 17.1 s | 4.34 GB (100%) |
| qwen3.5:2b | 24/29 | 15/15 | 8/8 | 15/16 | 92% / 100% | 0.09 s | 84.1 | 6.4 s | 2.94 GB (100%) |
| deepseek-r1:8b | 27/29 | 14/15 | 8/8 | 2/16 | – | 13.03 s | 12.6 | 8.7 s | 4.05 GB (70%) |
| qwen3.5:9b | 29/29 | 15/15 | 8/8 | 12/16 | 100% / 100% | 0.87 s | 7.1 | 14.5 s | 4.16 GB (55%) |

## Embedders

| Model | Filing top-1 | Filing top-3 | Related vs unrelated gap | Chat recall@1 | Chat recall@5 | VRAM |
|---|---|---|---|---|---|---|
| nomic-embed-text | 99% | 100% | +0.016 | 93% | 100% | 0.32 GB |
| qwen3-embedding:0.6b | 99% | 100% | +0.016 | 100% | 100% | 2.37 GB |
| embeddinggemma | 99% | 100% | +0.026 | 93% | 100% | 0.68 GB |

## Where each brain went wrong

### gemma3:4b

- noteai: fix the spelling → text lacks ['the meeting', 'discuss', 'budget']
- noteai: make the list items bold → text lacks ['**']

### qwen3.5:4b

- noteai: who wrote atomic habits? → reply lacks ['clear']
- noteai: turn this into bullet points → lost lines ['Trip notes']

### gemma4:e4b

- no misses

### qwen3.5:2b

- noteai: when are we launching? → reply lacks ['march']
- noteai: write name of colors here → did answer, expected add
- noteai: make this a checklist → text lacks ['[ ]']
- noteai: turn this into bullet points → lost lines ['Trip notes']
- noteai: make the list items bold → text lacks ['**']
- tools: who is the current CEO of Microsoft? → no tool used

### deepseek-r1:8b

- noteai: write name of colors here → error ModelUnavailable: timed out
- noteai: sort the list alphabetically → wrong order
- chat: when is the car service due? → missing any of ['45,000', '45000', '45 000']
- tools: what is 1847 times 23? → no tool used
- tools: how much is 15% of 2400? → no tool used
- tools: what did I save about the Goa trip? → no tool used
- tools: find my notes about the invoice → no tool used
- tools: do I have anything about my mom's birthday? → no tool used
- tools: what's in my grocery list? → no tool used
- tools: who won the 2026 Formula 1 world championship? → no tool used
- tools: what's the latest version of Python? → no tool used
- tools: what's the weather in Pune today? → no tool used
- tools: who is the current CEO of Microsoft? → no tool used
- tools: remind me to call the bank tomorrow at 10am → no tool used
- tools: set a reminder to pay rent on the 1st → no tool used
- tools: move my dune note to movies → no tool used
- tools: put the invoice note in Work → no tool used

### qwen3.5:9b

- tools: who won the 2026 Formula 1 world championship? → no tool used
- tools: who is the current CEO of Microsoft? → no tool used
- tools: move my dune note to movies → used search_notes, expected ['move_note']
- tools: put the invoice note in Work → used search_notes, expected ['move_note']
