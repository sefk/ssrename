# `fm` backend: M1 Max Studio vs M3

Same 21-image `bench/sets/studio` set, `fm` variant only.

| | Studio (M1 Max, 64GB) | This machine (M3, 16GB) |
|---|---|---|
| pass | 4/21 | 6/21 |
| app | 2/19 | 7/19 |
| topic | 11/21 | 10/21 |
| median latency | 2.2s | 0.8s |
| p90 latency | 2.6s | 0.9s |

Results files:

- `bench/sets/studio/results/studio-20260918-170349.json`
- `bench/sets/studio/results/sef-m3-20260919-110349.json`

## Takeaways

- The M3 runs `fm` roughly 2.7x faster per image (0.8s vs 2.2s median).
- Both machines' `fm` backend does poorly on `app` accuracy — it routinely
  hallucinates generic web-app guesses (`reddit`, `github`, `google`,
  `discord`) instead of reading the actual UI, most often mistaking
  `datatalk`/`datalk` screens for something else.
- Neither machine's `fm` run is close to the `openai` backend's studio numbers
  (15/21 pass, 13/19 app) captured in the same comparison run — `fm` is
  faster but meaningfully less accurate.

Full side-by-side, including per-image answers:

```
uv run ssrename-bench compare bench/sets/studio/results/studio-20260918-170349.json \
                              bench/sets/studio/results/sef-m3-20260919-110349.json
```
