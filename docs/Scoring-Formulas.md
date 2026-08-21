# AI Visibility Audit — Scoring Formulas

This document is the source of truth for how CiteSight turns 62 live parameter checks into three pillar scores and one overall score. It matches the engine in `backend/app/parameters/scoring.py` and the weights in `backend/app/config.py`.

---

## 1. What is scored

| Pillar | Parameters | Weight in overall |
|---|---|---|
| **On-Page** | 22 checks | **40%** |
| **Off-Page** | 18 checks | **25%** |
| **Technical** | 22 checks | **35%** |
| **Overall** | 62 checks | **100%** |

Every parameter is scored on a 0–100 scale and given a relative weight (`0.7`, `1.0`, or `1.5`) from `registry.json`.

Status rules:

| Status | Meaning | Used in score? |
|---|---|---|
| `PASS` | Score ≥ 90 | Yes |
| `PARTIAL` | Score 60–89 | Yes |
| `FAIL` | Score < 60 | Yes |
| `UNKNOWN` | Source unavailable | **No — excluded from the denominator** |

---

## 2. Parameter contribution (points inside a pillar)

Inside a pillar, parameter weights are normalized so they add to 100 max points.

\[
\text{MaxPoints}_i = \frac{w_i}{\sum w_j} \times 100
\]

\[
\text{Points}_i = \frac{\text{Score}_i}{100} \times \text{MaxPoints}_i
\]

`UNKNOWN` rows have no score, so they contribute neither max points nor earned points.

---

## 3. Pillar score (On-Page, Off-Page, Technical)

A pillar score is the **weighted average** of every *known* parameter in that section:

\[
\text{PillarScore} = \frac{\sum (\text{Score}_i \times w_i)}{\sum (100 \times w_i)} \times 100
\]

which simplifies to:

\[
\text{PillarScore} = \frac{\sum (\text{Score}_i \times w_i)}{\sum w_i}
\]

The engine then rounds to **1 decimal place**.

`UNKNOWN` parameters are dropped before both the numerator and the denominator. A missing source does not drag the pillar down; it is simply not counted.

If a pillar has no known parameters, its score is `null` and it is omitted from the overall calculation.

---

## 4. Weighted contribution (what the dashboard table shows)

\[
\text{Weighted}_p = \text{PillarScore}_p \times \text{PillarWeight}_p
\]

| Pillar | Weight \(W_p\) |
|---|---|
| On-Page | \(0.40\) |
| Off-Page | \(0.25\) |
| Technical | \(0.35\) |

The three weighted values are the pieces that add up to the overall score when every pillar is present.

---

## 5. Overall score

When all three pillars have a score:

\[
\text{Overall} = (\text{OnPage} \times 0.40) + (\text{OffPage} \times 0.25) + (\text{Technical} \times 0.35)
\]

Rounded to **1 decimal place**.

If a pillar is missing (`null`), its weight is removed and the remaining weights are **renormalized** so they still add to 100%:

\[
\text{Overall} = \frac{\sum (\text{PillarScore}_p \times W_p)}{\sum W_p} \quad \text{over pillars that exist}
\]

Example: Technical = 100, On-Page = 50, Off-Page unavailable:

\[
\text{Overall} = \frac{(100 \times 0.35) + (50 \times 0.40)}{0.35 + 0.40} = \frac{55}{0.75} = 73.3
\]

---

## 6. Score bands (dashboard)

| Band | Range |
|---|---|
| Excellent | 90+ |
| Good | 75–89 |
| Fair | 60–74 |
| Poor | 40–59 |
| Critical | Under 40 |

The same bands apply to each pillar and to the overall score.

---

## 7. Worked example

The overall numbers below match the Overall Status dashboard: On-Page **28.4**, Off-Page **34.8**, Technical **60.3**, overall **41.2**.

### Step A — How a pillar score is built

Five On-Page parameters were scored. A sixth was `UNKNOWN` and is excluded.

| Parameter | Weight \(w\) | Score / 100 | Score × weight | Max points | Points earned |
|---|---:|---:|---:|---:|---:|
| Question-style headings | 1.0 | 20 | 20.0 | 20.00 | 4.00 |
| Core concept definitions | 1.0 | 10 | 10.0 | 20.00 | 2.00 |
| Topic coverage | 1.0 | 40 | 40.0 | 20.00 | 8.00 |
| Answer freshness | 1.0 | 30 | 30.0 | 20.00 | 6.00 |
| Citation-ready facts | 1.0 | 42 | 42.0 | 20.00 | 8.40 |
| Licensed mention check | 1.0 | UNKNOWN | — | — | — |
| **Known total** | **5.0** | | **142.0** | **100.00** | **28.40** |

\[
\text{OnPage} = \frac{142.0}{5.0} = 28.4
\]

The dashboard On-Page value of 28.4 is this same formula on all 22 On-Page checks. The five-row table is a scaled-down version that lands on 28.4 so the arithmetic is easy to verify. Off-Page and Technical use the identical formula on their own parameter sets:

\[
\text{OnPage} = 28.4,\quad \text{OffPage} = 34.8,\quad \text{Technical} = 60.3
\]

### Step B — Convert each pillar into a weighted contribution

\[
\text{Weighted On-Page} = 28.4 \times 0.40 = 11.36 \;\rightarrow\; 11.4
\]

\[
\text{Weighted Off-Page} = 34.8 \times 0.25 = 8.70 \;\rightarrow\; 8.7
\]

\[
\text{Weighted Technical} = 60.3 \times 0.35 = 21.105 \;\rightarrow\; 21.1
\]

### Step C — Add them for overall

\[
\text{Overall} = 11.36 + 8.70 + 21.105 = 41.165 \;\rightarrow\; \mathbf{41.2}
\]

Displayed table (each cell rounded to 1 decimal, then summed):

| Pillar | Weight | Score / 100 | Weighted | Band |
|---|---:|---:|---:|---|
| On-Page | 40% | 28.4 | 11.4 | Critical |
| Off-Page | 25% | 34.8 | 8.7 | Critical |
| Technical | 35% | 60.3 | 21.1 | Fair |
| **Overall** | **100%** | | **41.2** | **Poor** |

41.2 sits in the **40–59 Poor** band.

### Step D — Tiny pillar example you can compute by hand

Three Technical checks, all known:

| Parameter | Weight | Score |
|---|---:|---:|
| AI crawler permissions | 1.5 | 100 |
| llms.txt present | 1.5 | 40 |
| Structured data | 1.0 | 70 |

\[
\text{Technical} = \frac{(100 \times 1.5) + (40 \times 1.5) + (70 \times 1.0)}{1.5 + 1.5 + 1.0} = \frac{150 + 60 + 70}{4.0} = 70.0
\]

If `llms.txt` had been `UNKNOWN`, it would drop out:

\[
\text{Technical} = \frac{(100 \times 1.5) + (70 \times 1.0)}{1.5 + 1.0} = \frac{220}{2.5} = 88.0
\]

---

## 8. One-line summary

1. Score each of the 62 parameters 0–100. Skip `UNKNOWN`.
2. On-Page / Off-Page / Technical = weighted average of that pillar’s known parameters.
3. Overall = On-Page × 40% + Off-Page × 25% + Technical × 35%, rounded to 1 decimal.
4. If a pillar is missing, divide by the remaining weights so they still total 100%.
