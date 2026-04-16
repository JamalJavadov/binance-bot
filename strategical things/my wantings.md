# Crypto Trading Bot Strategy Requirements

## 1. Purpose

This document defines the **strategy requirements** for a Binance Futures USD-M trading bot.

The goal is **not** to define the full technical architecture of the bot. The goal is to clearly document what kind of **trading strategy behavior** is required so that a suitable strategy can later be selected, designed, or adapted to these expectations.

This document should be used as a **strategy requirement brief** before choosing the final trading logic.

---

## 2. Core Objective

The bot must scan a very large portion of the Binance Futures USD-M market and identify only the **best tradable opportunities**.

The main priority is:

- not to force trades,
- not to chase trade count,
- not to wait for unrealistic perfect textbook setups,
- but to find **realistically tradable, high-quality setups** that a serious trader would consider valid in live market conditions.

The strategy must therefore aim for:

- **quality over quantity**,
- **realistic execution**,
- **consistent decision logic**,
- **fully automated trade handling**.

---

## 3. What “High-Quality” Means in This Project

In this project, **high-quality setup** does **not** mean a perfect, rare, or nearly impossible setup.

It means a setup that is:

- realistic,
- tradable,
- strong enough to justify entry,
- aligned with live market conditions,
- and statistically or structurally good enough to deserve execution.

The strategy must **not** interpret “high-quality” as “only if everything is perfect.”

That would be too strict and would likely result in no trades at all.

Instead, the strategy should use a **real trader's standard**, meaning:

- the setup should be good enough to trade,
- good enough to have meaningful edge,
- good enough to pass realistic filtering,
- but not so strict that valid real-market opportunities are rejected unnecessarily.

### Required interpretation

The correct interpretation is:

> **Only take setups that are realistically strong and worth trading — not perfect, not random, and not forced.**

If no such setup exists, the bot should not open a trade.

---

## 4. Market Scope

The strategy must work for:

- **Binance Futures USD-M**

It should scan:

- as many relevant coins as reasonably possible,
- not be limited to a hard fixed number such as 300,
- and search a broad enough market universe to maximize the chance of finding strong opportunities.

The idea is to inspect a wide market set, but only promote the **best few** setups.

---

## 5. Directional Scope

The strategy must support:

- **Long positions**
- **Short positions**

The bot should be free to choose either side depending on market conditions and setup quality.

---

## 6. Operating Style Requirements

The user does not want to hard-code one fixed style such as only breakout, only pullback, only trend-following, or only mean reversion.

The strategy should be **adaptive**.

This means the final strategy may choose the most suitable logic based on the market opportunity, as long as the result is high-quality and realistic.

The following should remain flexible and strategy-driven:

- setup style,
- entry style,
- timeframe combination,
- stop-loss management style,
- take-profit management style,
- coin filtering rules,
- leverage selection,
- order duration / expiry decisions,
- position management rules.

### Important note

The requirement is **not** “use everything.”

The requirement is:

> **Choose whatever combination produces the best realistic tradable setups.**

---

## 7. Automation Model

The strategy is intended for a **fully automatic execution flow**.

The user only wants to start the process.

After that, the strategy must be capable of handling all trading decisions automatically.

This includes:

- scanning coins,
- evaluating market conditions,
- ranking setups,
- selecting entry type,
- selecting leverage,
- creating pending orders,
- canceling expired pending orders,
- converting valid signals into live orders,
- managing open positions,
- handling stop-loss and take-profit logic,
- handling updates during the trade lifecycle.

The desired model is:

> **User starts the bot; the bot handles everything else.**

---

## 8. Scan and Opportunity Selection Logic

The strategy should continuously analyze the market and identify the strongest available setups.

### Maximum number of opportunities

At any scan cycle, the strategy may select:

- up to **3 setups maximum**

However, it must **not** force the count to 3.

If market conditions only justify:

- 0 setups,
- 1 setup,
- or 2 setups,

then the strategy should keep it that way.

### Required principle

The bot must prefer:

- **no trade** over bad trade,
- **fewer trades** over lower-quality trades,
- **strongest opportunities only**.

---

## 9. Ranking Requirement

When multiple opportunities exist, the strategy should prioritize the **top 3 most promising setups**.

The main ranking priority should be:

- **highest probability of success / highest chance of winning**

This ranking can be supported by any appropriate internal logic, but the end result should reflect:

- strongest tradable opportunity,
- best overall setup quality,
- best realistic expected outcome,
- best market structure quality,
- best execution viability.

The top setups should not simply be the most active or most volatile ones.
They should be the ones most worth trading.

---

## 10. Trade Frequency Requirement

Trade frequency is **not** a target by itself.

The user does not want the strategy to optimize for a fixed number of trades per day.

The only true rule is:

- if good trades exist, the bot may take them,
- if very few good trades exist, the bot should stay selective,
- if no good trades exist, the bot should do nothing.

So the correct interpretation is:

- low trade count is acceptable,
- high trade count is acceptable,
- **only trade quality matters**.

---

## 11. Risk/Reward Requirement

Every executed trade must preserve a **minimum risk-to-reward ratio of 1:3**.

That means:

- if the strategy risks 1 unit,
- the target reward should be at least 3 units.

### Hard rule

- **Minimum R:R = 1:3**

Higher than 1:3 is acceptable.
Lower than 1:3 is not acceptable.

The strategy may manage profit dynamically if needed, but the minimum structural reward expectation must remain at or above this threshold.

---

## 12. Capital Allocation Requirement

Available budget:

- **10 USD**

The strategy should treat this as a small account and remain realistic about execution constraints.

### Allocation rule

If multiple trades are active, the capital allocation should be:

- **equal / evenly distributed**

The user wants balanced exposure, balanced potential gain, and balanced potential loss across active trades.

This means the strategy should avoid over-weighting one setup versus another in normal operation.

### Maximum concurrency

The system may have at most:

- **3 pending orders**
- **3 open positions**

---

## 13. Position and Order Lifecycle Requirements

### Pending orders

The strategy may use whichever order entry form is best for the setup, such as:

- limit entry,
- stop entry,
- breakout entry,
- retest-based entry,
- or any other suitable mechanism.

There is no requirement to force one fixed order type.

The strategy should choose the entry form that best matches the setup.

### Pending order expiry

Each pending order must have its own expiry logic.

The strategy should decide:

- how long an order remains valid,
- when it becomes stale,
- when it should be canceled.

This should be determined dynamically by the strategy, and each order should have a clearly defined validity period.

### Re-entry

If a coin produces a valid setup again later, the strategy is allowed to re-enter that same coin.

A coin should not be blocked permanently after one attempt, provided that a fresh valid opportunity appears.

---

## 14. Open Position Management Requirement

Once a position is opened, the strategy must manage it automatically.

This includes:

- maintaining the stop-loss,
- maintaining the take-profit,
- making trade-management decisions when necessary,
- handling the full lifecycle of the position.

There should be **no forced fixed time limit** on a position.

If the trade still remains valid according to the strategy, it may continue running.

The position should stay open for as long as the strategy logically requires.

---

## 15. Stop-Loss and Take-Profit Requirements

### Stop-loss

The exact stop-loss method is **not pre-fixed**.

The strategy may decide the best stop-loss logic based on the setup and market context.

It should choose the most appropriate method, as long as it remains realistic and consistent with the minimum 1:3 reward requirement.

### Take-profit

Take-profit logic may also be adaptive.

However, the following must remain true:

- the trade structure must preserve at least **1:3 minimum R:R**,
- profit logic should remain aligned with realistic execution,
- take-profit handling should not undermine the reward profile.

---

## 16. Correlation Control Requirement

The strategy may open up to 3 trades, but these should not all be effectively the same exposure.

It should avoid loading multiple positions that are too similar in behavior, such as:

- highly correlated coins,
- near-identical setups across the same market theme,
- repeated exposure to the same underlying directional risk.

### Required principle

The strategy should avoid taking 3 positions that are essentially the same trade in disguise.

It should maintain reasonable diversification across active selections.

---

## 17. Market Condition Filtering Requirement

The strategy should account for dangerous or low-quality market conditions.

It should be able to recognize when execution quality or setup quality is weakened by conditions such as:

- extreme volatility,
- sharp pump or dump behavior,
- abnormal spreads,
- poor liquidity,
- unstable order execution conditions,
- other risk-heavy moments where trade quality degrades.

The strategy should avoid or filter trades when these conditions materially reduce reliability.

---

## 18. Exchange Reality Requirement

The strategy must be realistic for live Binance Futures USD-M execution.

It must account for practical trading constraints, including:

- Binance minimum notional requirements,
- fees / commissions,
- slippage,
- leverage limits,
- small-account execution constraints.

This is especially important because the account size is only **10 USD**.

The strategy must therefore be **execution-aware**, not just theoretically attractive on paper.

---

## 19. Leverage Requirement

Leverage should not be hard-coded to one fixed value.

The strategy should select leverage automatically based on what makes the most sense for the setup and execution context.

The choice should remain realistic and compatible with:

- account size,
- Binance rules,
- trade safety,
- execution viability,
- and the strategy's internal risk structure.

---

## 20. Realistic Quality Threshold Requirement

This is a critical requirement.

The strategy must operate under a **realistic quality threshold**, not an impossible one.

That means:

- it should reject weak or random setups,
- it should reject low-quality noise,
- but it should **not** demand perfection,
- and it should **not** become so strict that it almost never trades.

The correct behavior is to select setups that are:

- realistically good,
- actionable,
- live-tradable,
- and sufficiently strong to justify entry.

This requirement exists specifically to prevent the strategy from becoming unusably strict.

---

## 21. Strategy Intelligence Requirement

The user wants the strategy to perform **deep analysis**, but this should be understood correctly.

Deep analysis does **not** simply mean adding many indicators or making the logic complicated.

It means the strategy should intelligently evaluate market opportunities and identify the setups with the best real trading value.

In practical terms, the strategy should be capable of using strong market-evaluation logic such as, where appropriate:

- structure-based evaluation,
- trend and directional context,
- momentum and strength confirmation,
- volatility awareness,
- volume or liquidity context,
- market condition filtering,
- multi-factor ranking,
- and other quality-improving filters.

The key requirement is not a specific list of indicators.
The key requirement is:

> **The strategy must be smart enough to identify the most tradable and highest-quality realistic setups.**

---

## 22. Non-Goal Clarifications

The following are **not** the goal:

- forcing exactly 3 trades,
- requiring impossible perfect setups,
- trading constantly just to create activity,
- using one rigid strategy style no matter the market,
- over-optimizing for frequency,
- selecting visually attractive but impractical setups,
- ignoring real exchange execution constraints.

---

## 23. Strategy Output Expectations

When the strategy identifies trades, the selected opportunities should effectively represent:

- the strongest currently tradable setups,
- the best-ranked opportunities available,
- realistic entries that can actually be executed,
- opportunities that satisfy minimum reward structure,
- positions that fit within concurrency and capital rules,
- selections that do not create excessive correlation concentration.

---

## 24. Final Strategy Requirement Statement

The required strategy can be summarized as follows:

> A fully automated Binance Futures USD-M strategy that scans a broad universe of coins, supports both long and short trading, selects only realistically high-quality and tradable setups, avoids forced trades, ranks opportunities by strongest probability and overall quality, chooses up to 3 best setups without forcing the count, preserves a minimum 1:3 risk-to-reward ratio, allocates capital evenly across active trades, manages pending orders and open positions automatically, accounts for correlation and live execution realities, and remains adaptive rather than rigid in its method selection.

---

## 25. Intended Use of This Document

This document should be used as the basis for:

- evaluating candidate strategy ideas,
- choosing the most suitable trading strategy,
- refining a final execution model,
- or instructing another system or developer on the required strategic behavior.

This document is intentionally focused on **strategy expectations**, not on backend/frontend implementation details.

