# Council Evaluation Framework

**A pre-committed specification for evaluating a multi-agent trading/investing council before any real capital is deployed.**

Author: Daegan Pimenta
Date locked: 2026-06-17
Status: LOCKED — see "Amendment Rule" before changing anything.

---

## 0. Why this document exists

This framework is written *before* the agents are built and *before* any money is at risk. Its entire purpose is to define what "this works" means while I am emotionally neutral, so that a future, impatient, possibly-on-a-hot-streak version of me cannot quietly redefine success.

The core discipline: **build the judge before the players.** The system does not earn real capital by feeling smart or producing confident output. It earns real capital by clearing a numerical bar, defined here, over a defined period, against an honest baseline.

If the system fails to clear the bar, that is a successful and valuable outcome. "I built this, evaluated it honestly, and proved it did not beat the baseline" is a real result — both for my own capital protection and as a portfolio piece.

---

## 1. Two independent tracks

The system is evaluated as two separate tracks. Performance is never blended, because a good month in one must not be allowed to disguise a bad strategy in the other.

| | **Crypto Track** | **Equities Track** |
|---|---|---|
| Style | Active, shorter horizon | Passive, long-term hold |
| Risk appetite | High | Low |
| Trade types | Scalp / day / swing / position | Position only (long-term) |
| Strategy thesis | Trend + interpretation edge | Mispriced second-order beneficiaries of real structural trends |
| Baseline to beat | Buy-and-hold Bitcoin (BTC) | S&P 500 total return |
| Secondary baseline | My own manual logged decisions | My own manual logged decisions |
| Minimum evaluation window | 3 months | 6 months |

---

## 2. The baselines (the things the council must beat)

A strategy that cannot beat a simple, lazy alternative has no reason to exist. The council is measured against:

**Equities Track**
- **Primary baseline: S&P 500 total return.** This is deliberately brutal. The entire bet of a thematic, hand-picked equity strategy is that it beats just buying the whole index. History says concentrated thematic bets usually *don't*. If the council can't clear this, the honest answer is "buy an index fund."
- **Secondary baseline: my own manual picks**, logged in parallel. Tells me whether the council beats me.

**Crypto Track**
- **Primary baseline: buy-and-hold BTC**, the closest thing crypto has to a benchmark.
- **Secondary baseline: my own manual decisions**, logged in parallel.
- Note: crypto has no clean benchmark and is far more variance-prone. Treat crypto results with *more* skepticism, not less, even if they look good.

---

## 3. Every output is a falsifiable, logged prediction

No agent output counts unless it is logged at the moment it is made, with all of the following. A rating or opinion with no checkable prediction attached is discarded as noise.

Each logged decision must contain:
- **Timestamp** (when the call was made)
- **Asset**
- **Direction / action** (buy, sell, hold, avoid)
- **Conviction / rating** (the council's confidence — see §5 on the rating agent)
- **Time horizon** (when this prediction should be judged — e.g. 7 days, 90 days, 12 months)
- **Written thesis** (the specific, checkable claim — *why*, and what would make it wrong)
- **Which agent(s)** produced and challenged it

**Hard rule:** during the evaluation window, nothing is acted on with real money. Everything is paper-logged. The point of the window is to gather evidence, not returns.

---

## 4. The metric is risk-adjusted, not raw return

Raw return is a liar's metric because it rewards taking insane risk during a lucky streak. A smooth 12% beats a wild 20%.

- **Primary metric: Sharpe ratio** (return per unit of volatility) for each track vs. its baseline.
- **Also tracked:** max drawdown (worst peak-to-trough drop), hit rate (% of falsifiable predictions that resolved correct), and average win vs. average loss.
- A track only "wins" if it beats its primary baseline **on Sharpe**, not just on raw return.

---

## 5. The rating / evaluation agent

A rating is only useful if it is falsifiable. "7/10" means nothing unless the 7 is tied to a checkable prediction with a horizon. So every rating decomposes into scored criteria, each with a written rationale, each attached to the prediction it supports.

**Equities rating criteria (draft — refine before locking):**
- Valuation (is it already priced in?)
- Structural-trend strength (is the underlying trend real and durable?)
- Second-order positioning (is this a non-obvious beneficiary the market under-notices?)
- Fundamental health
- Risk factor (what kills this thesis?)

**Crypto rating criteria (draft — refine before locking):**
- Momentum / price action
- Catalyst / news flow
- Liquidity & risk
- Downside scenario

Six months later, every rating must be checkable: *was the 7/10 right, for the reason given?*

---

## 6. The proof bar (PRE-COMMITTED — do not lower)

Real capital does **not** enter either track until that track has, over its full minimum evaluation window:

1. Beaten its **primary baseline on Sharpe ratio**, and
2. Beaten its **secondary baseline (my manual picks) on Sharpe ratio**, and
3. Produced a **minimum sample size** of resolved, falsifiable predictions — **at least 30 resolved predictions per track**, and
4. Shown a **max drawdown** within tolerance: **≤40% (Crypto Track)**, **≤25% (Equities Track)**.

All four conditions. Not three. Not "close enough during a hot streak."

**If a track clears the bar:** real capital may enter, *sized conservatively* (see §7).
**If a track fails the bar:** no real capital. Either iterate and restart a fresh evaluation window, or conclude the honest result and write it up.

---

## 7. The "impatience clause"

I have already acknowledged that my patience today is not my patience in month two. This clause exists to bind the impatient version of me.

- The proof bar in §6 **cannot be lowered** once this document is locked. It can only be raised. (See Amendment Rule.)
- A hot streak is **not** evidence. A hot streak is the single most common reason traders lower their own standards. If the system has a great month, that is *expected variance*, not proof.
- If/when real capital enters, it starts **small enough that losing all of it changes nothing about my life**, and scales only with continued, evaluated, out-of-sample performance — never with a feeling.
- Last year's "great season" is treated as **unproven** until this framework demonstrates repeatable edge. It may have been skill. It may have been variance. The framework's job is to find out, not to assume.

---

## 8. Amendment Rule

This document is locked on the date in the header. After that:
- The proof bar (§6) and impatience clause (§7) may only be made **stricter**, never looser.
- Any change is logged at the bottom with a date and reason, so I can see later whether I was tightening discipline or rationalizing.
- The honest tell: if I ever find myself editing this doc to make success *easier* right after a good week, that is the exact moment to stop and walk away from the keyboard.

---

## 9. What success and failure each look like

**Success:** A track clears all four proof-bar conditions over its full window. I deploy small real capital, keep logging, keep evaluating. I also have a genuinely strong, honest portfolio project: a rigorously-evaluated multi-agent system with real out-of-sample results.

**"Failure" (also success):** A track does not clear the bar. I keep my capital, and I have an even more credible portfolio story — *"I built a thematic multi-agent investing council, evaluated it honestly against the S&P 500 and BTC, and the data showed it did not beat a passive baseline on a risk-adjusted basis."* Most people never have the discipline to run that test. Having run it is the impressive part.

Either way, the framework wins. Only un-evaluated real-money trading loses.

---

### Amendment log
- 2026-06-17 — document locked, initial version. Sample size ≥30/track; max drawdown ≤40% crypto, ≤25% equities.
