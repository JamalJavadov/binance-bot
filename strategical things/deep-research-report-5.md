# Comprehensive Trading Strategy Compendium  
*(Educational research document; not investment advice. Date: 2026-04-08, Asia/Baku. Where the user provided no configuration, parameters or assets, this document explicitly marks them **unspecified**.)*

## Executive summary  

Trading strategies are best understood as **repeatable rules** that convert information (prices, order flow, fundamentals, macro data, implied volatility, corporate events) into **positions** with **explicit risk limits and execution procedures**. In practice, a “strategy” is rarely only an entry signal: the durable edge typically comes from the *full stack*—signal design, portfolio construction, position sizing, transaction-cost-aware execution, and robust risk controls (e.g., drawdown limits, liquidity limits, stress tests, and regime awareness).  

Across liquid markets, the most consistently documented premia in the academic and high‑quality industry literature include:  
- **Trend following / time‑series momentum** across futures and major asset classes, with long historical evidence and crisis‑period convexity characteristics in some studies. citeturn0search8turn7search12  
- **Cross‑sectional momentum** (“winners minus losers”) in equities and other markets, originally documented as an anomaly and later incorporated into multi‑factor models. citeturn1search0turn1search5  
- **Value and momentum across asset classes**, suggesting “style premia” are not unique to equities. citeturn7search1  
- **Carry** as an advance‑measurable component of expected returns (e.g., term structure and interest differentials) across asset classes. citeturn3search13turn3search5  
- **Options volatility risk premium (VRP)** and related variance/volatility premia, often operationalised through systematic short‑vol exposures (e.g., put‑write/covered call indexes) with well‑documented asymmetric tail risk. citeturn6search0turn6search2turn6search3  
- **Relative‑value statistical arbitrage**, including classic **pairs trading** and broader market‑neutral stat‑arb frameworks, with strong historical backtests but also documented crowding/unwind risk (e.g., “Quant Meltdown” discussions). citeturn0search9turn7search6turn4search3  

At the same time, **strategy capacity, costs, and microstructure constraints** dominate realised performance. Execution and market impact are not secondary details: formal “optimal execution” models explicitly optimise the trade‑off between market impact costs and risk during execution. citeturn0search7  

Finally, the document emphasises a professional standard: treat any backtest as a **statistical estimate** subject to model error, overfitting, and non‑stationarity. The most repeatable research programmes combine (a) economically motivated hypotheses, (b) robust out‑of‑sample protocols, and (c) conservative transaction cost modelling and stress testing.

## Strategy taxonomy and decision pipeline  

```mermaid
flowchart TB
  A[Trading Strategies] --> B[Directional]
  A --> C[Relative Value / Market Neutral]
  A --> D[Volatility & Options]
  A --> E[Liquidity Provision / Microstructure]
  A --> F[Macro / Event-Driven]

  B --> B1[Trend Following / Time-Series Momentum]
  B --> B2[Cross-Sectional Momentum]
  B --> B3[Breakout]
  B --> B4[Mean Reversion / Contrarian]
  B --> B5[Swing / Intraday Styles]

  C --> C1[Pairs Trading]
  C --> C2[Equity Stat-Arb (factor residuals)]
  C --> C3[Index/ETF Arbitrage & Basis]
  C --> C4[Carry as Relative-Value]

  D --> D1[Covered Calls / Buy-Write]
  D --> D2[Put-Write / Short Tail Insurance]
  D --> D3[Straddles/Strangles (Long Vol)]
  D --> D4[Spreads (Vertical/Calendar)]
  D --> D5[Variance/Volatility Swaps]
  D --> D6[Delta-Neutral / Gamma Scalping]

  E --> E1[Market Making]
  E --> E2[High-Frequency Market Making]
  E --> E3[Execution Algorithms (TWAP/VWAP/IS)]
  E --> E4[Order-Flow / Microprice Signals]

  F --> F1[Global Macro Systematic]
  F --> F2[Earnings-Driven]
  F --> F3[M&A / Merger Arbitrage]
```

Typical end‑to‑end pipeline (research → production) for professional systematic trading:

```mermaid
flowchart LR
  S0[Hypothesis & Economic Rationale] --> S1[Data Definition & Cleaning]
  S1 --> S2[Signal Design & Feature Engineering]
  S2 --> S3[Backtest Engine w/ Costs]
  S3 --> S4[Validation: OOS, Walk-Forward, Robustness]
  S4 --> S5[Portfolio Construction & Risk Budgeting]
  S5 --> S6[Execution Model & Slippage Controls]
  S6 --> S7[Monitoring: Drift, Regimes, Limits]
  S7 --> S8[Post-Trade Analytics & Iteration]
```

## Strategy playbooks  

The following “playbooks” cover major strategy families requested by the user. Each is written as a *production‑oriented specification*: **definition**, **rationale**, **math foundations**, **signal rules**, **position sizing**, **risk**, **timeframes**, **instruments**, **indicators/parameters**, **pros/cons**, **variants**, **performance evidence**, **example backtest summary (assumptions unspecified)**, **data & cost notes**, and **implementation pseudocode**.

### Trend-following and breakout family  

**Strategy: Time‑series momentum (TSMOM) / Trend following (directional across futures)**  
Definition: Go **long** assets with positive look‑back returns and **short** assets with negative look‑back returns, typically across futures in equities, rates, FX, and commodities. citeturn0search8turn7search12  
Rationale: Persistence in returns over 1–12 months with partial longer‑horizon reversal has been documented in liquid futures, consistent with under‑reaction / delayed adjustment narratives and risk‑based interpretations. citeturn0search8  
Mathematical foundations:  
- Canonical signal: \( s_{t} = \mathrm{sign}(r_{t-L:t}) \) or \( s_{t} = \frac{P_t - P_{t-L}}{P_{t-L}} \) with a threshold.  
- Volatility scaling: weight \( w_t \propto \frac{s_t}{\hat{\sigma}_t} \) to equalise risk.  
- Portfolio returns: \(R_{p,t}=\sum_i w_{i,t} r_{i,t+1}\).  
Entry/exit rules:  
- Entry at rebalance time if \(s_t>0\) (long) or \(s_t<0\) (short).  
- Exit/flip on sign change; optional “neutral zone” to reduce churn.  
Position sizing:  
- Risk parity across legs (equal volatility contribution) or equal volatility targets per instrument.  
- Optional trend strength scaling: \(w\propto \tanh(k \cdot s)\).  
Risk management:  
- Volatility targeting, stop‑out on portfolio drawdown, tail hedges optional.  
- Crisis exposure and correlation spikes are central; diversify across asset classes (as done in century‑scale trend studies). citeturn7search12  
Typical timeframe: Daily to monthly rebalancing; holding days to months.  
Instruments: Liquid futures (equity indices, bonds, FX forwards/futures, commodities). citeturn7search12turn0search8  
Indicators/parameters: Lookback \(L\) (often 1–12 months), volatility estimator window (e.g., 20–60 trading days), rebalancing frequency, transaction cost model.  
Pros/cons: Robust across markets and long history; tends to perform in some crisis regimes (study‑dependent) but suffers in choppy mean‑reverting regimes and during rapid reversals. citeturn7search12turn0search8  
Common variants: Moving‑average crossovers; price‑channel breakouts; volatility breakouts; dual‑timeframe ensembles.  
Historical performance evidence: Broad cross‑asset evidence in futures for TSMOM and century‑scale trend analysis are widely cited. citeturn0search8turn7search12  
Typical edge / expected returns: Literature reports “substantial abnormal returns” diversified across asset classes, but realised returns are highly sensitive to costs, leverage, and regime. citeturn0search8turn7search12  
Example backtest summary (assumptions: **unspecified**): Universe **unspecified**; lookback 1/3/12‑month ensemble; vol target **unspecified**; costs **unspecified**; expected behaviour: smoother returns than single‑market trend, drawdown clusters during range‑bound regimes; crisis convexity may appear depending on design. citeturn7search12  
Data & costs: Daily settlement prices can be sufficient for medium‑term trend; realistic modelling needs roll yields, margin/leverage, and conservative slippage (especially in commodities and less liquid contracts).  
Implementation notes (pseudocode):  
```pseudo
for each rebalance date t:
  for each instrument i:
    signal_i = average( sign(return(i, t-1m,t)),
                        sign(return(i, t-3m,t)),
                        sign(return(i, t-12m,t)) )
    vol_i = EWMA_vol(returns_i, lambda)
    raw_weight_i = signal_i / vol_i
  weights = normalise_to_portfolio_vol_target(raw_weight)
  execute weights with cost-aware execution model
  apply risk caps: max_position, max_sector, max_drawdown, liquidity limits
```
citeturn0search8turn7search12  

**Strategy: Breakout (Donchian / price channel breakout; trend initiation)**  
Definition: Enter on a **new high/low** relative to a lookback window, arguing a breakout indicates the start/continuation of a trend.  
Rationale: Breakout rules are a concrete operationalisation of trend following: they favour regimes where price moves persist beyond recent ranges (conceptually aligned with time‑series momentum evidence). citeturn0search8  
Math foundations: Channel high \(H_L = \max(P_{t-L:t})\), low \(L_L = \min(P_{t-L:t})\). Trigger when \(P_t > H_L\) (long) or \(P_t < L_L\) (short).  
Entry/exit rules:  
- Entry: breakout above \(H_L\) or below \(L_L\).  
- Exit: trailing stop, opposite breakout, or time stop.  
Position sizing: Volatility‑scaled (e.g., ATR‑based) to keep risk constant; cap size by liquidity.  
Risk management: False breakouts can dominate; use filters (volatility expansion, volume confirmation, regime filters).  
Timeframes/instruments: Intraday to multi‑month; common in futures and FX due to liquidity and leverage.  
Indicators/parameters: Lookback \(L\), stop distance \(k \times ATR\), confirmation filters.  
Pros/cons: Simple and interpretable; can be robust if diversified; suffers in sideways regimes and high transaction‑cost environments.  
Performance evidence: Treated as a form of trend following; medium‑term evidence comes mainly from TSMOM/trend literature rather than “breakout” labelling. citeturn7search12turn0search8  
Example backtest summary (assumptions: **unspecified**): Daily channel breakout \(L\)=20/55; stop \(2\times ATR\); costs **unspecified**; expected: positively skewed payoffs with occasional large wins, but many small losses in noisy markets.  
Implementation notes:  
```pseudo
if close[t] > max(close[t-L:t-1]): go long
if close[t] < min(close[t-L:t-1]): go short
stop = entry_price - k*ATR for long; +k*ATR for short
exit if stop hit or breakout in opposite direction
size = risk_budget / (k*ATR)
```

**Strategy: Moving‑average crossover (trend filter / smoother trend following)**  
Definition: Long when fast MA > slow MA; short or flat otherwise.  
Rationale: Low‑pass filtering reduces sensitivity to noise; closely related to time‑series momentum. citeturn0search8  
Math: \(MA_f=\frac{1}{f}\sum_{j=0}^{f-1}P_{t-j}\), \(MA_s=\frac{1}{s}\sum_{j=0}^{s-1}P_{t-j}\). Signal \(s_t=\mathrm{sign}(MA_f-MA_s)\).  
Rules: Cross → enter/exit. Add band to reduce churn.  
Sizing: Vol targeting; correlation‑aware portfolio weighting.  
Risk: Whipsaw risk; gap risk; parameter stability.  
Backtest summary (assumptions: **unspecified**): Typical (20, 100) MA across futures; costs **unspecified**; expected: fewer trades than breakout, reduced whipsaws vs raw momentum but slower reaction to reversals.  
Pseudocode:  
```pseudo
signal = sign(MA(price, fast) - MA(price, slow))
weight = signal / EWMA_vol
```

### Momentum and swing family  

**Strategy: Cross‑sectional momentum (relative strength; winners minus losers)**  
Definition: Rank assets by past returns; go long “winners”, short “losers”. The canonical equity result is documented by buying past winners and selling past losers. citeturn1search0  
Rationale: Empirically persistent intermediate‑horizon continuation in returns; widely treated as a major factor and included in multi‑factor models (e.g., four‑factor frameworks). citeturn1search0turn1search5  
Math foundations:  
- Ranking signal: \(rank_i = r_{i,t-L:t}\).  
- Portfolio: long top decile, short bottom decile (or top/bottom quantiles).  
- Neutralisation: beta and sector neutral, dollar neutral, or volatility neutral.  
Entry/exit rules: Monthly formation/holding periods common in the literature; implement with overlapping portfolios to smooth turnover. citeturn1search0  
Position sizing: Equal weight within legs, volatility scaling, or optimisation to reduce concentration.  
Risk management: Momentum crashes (sharp reversals) and crowding; implement crash protection (e.g., defensive overlays or dynamic scaling).  
Timeframes/instruments: Mostly monthly in classic studies; can be adapted to weekly/daily (higher costs).  
Indicators/parameters: Lookback (e.g., 6–12 months) and skip‑month conventions are common; exact choices **unspecified** here.  
Pros/cons: Strong empirical regularity; but can be fragile in stressed reversals; high turnover intraday versions are cost‑sensitive.  
Performance evidence: Seminal equity evidence and later factor model incorporation are well documented. citeturn1search0turn1search5  
Example backtest summary (assumptions: **unspecified**): Universe **unspecified**; 12‑month ranking, 1‑month holding; costs **unspecified**; expected: positive long‑short returns with episodic drawdowns during sharp market rebounds.  
Implementation notes:  
```pseudo
monthly:
  ranks = trailing_return(asset, lookback)
  winners = top_quantile(ranks, q)
  losers  = bottom_quantile(ranks, q)
  long_leg  = equal_weight(winners)
  short_leg = equal_weight(losers)
  hedge beta/sector if required
```

**Strategy: Swing trading as a systematic style (pullback-in-trend)**  
Definition: Trade multi‑day moves by entering on pullbacks within a higher‑timeframe trend (e.g., trend filter + mean‑reversion entry).  
Rationale: Decomposes trend into “trend regime” plus “temporary retracement”; aims to improve entries and reduce drawdowns.  
Math: Regime filter \(T_t\) from MA/TSMOM; entry trigger from z‑score of deviation from MA or RSI‑style oscillator.  
Entry/exit:  
- Regime: only long if \(T_t>0\).  
- Entry: buy when short‑term z‑score < \(-z_0\) (pullback).  
- Exit: revert to mean / take profit / trailing stop.  
Sizing: ATR‑based; reduce size in high vol.  
Risk: Can be “two models glued together”; beware over‑fitting.  
Backtest summary (assumptions: **unspecified**): Trend filter 200‑day MA; entry z<−1.5; exit at z>0; costs **unspecified**; expected: fewer whipsaws than pure mean‑reversion in bear trends.  
Pseudocode:  
```pseudo
trend = sign(MA200 - price)
z = (price - MA20) / stdev(price-MA20, 20)
if trend>0 and z<-z0: enter long
exit when z>=0 or stop hit
```

### Mean-reversion and contrarian family  

**Strategy: Short‑horizon reversal / contrarian (single‑asset or cross‑sectional)**  
Definition: Bet that recent losers rebound and winners mean‑revert over short horizons (days to weeks).  
Rationale: Short‑horizon negative autocorrelation and “reversal” effects have been documented; explanations include microstructure (bid‑ask bounce), liquidity provision, and lead‑lag/cross‑autocovariances. citeturn4search4turn4search16  
Math foundations:  
- Simple reversal signal: \(s_t = -r_{t-1}\) (or \(-r_{t-k:t-1}\)).  
- Portfolio version: long bottom decile of last‑month return and short top decile.  
Entry/exit: Frequent rebalancing; often requires careful microstructure treatment (use mid‑quotes if possible).  
Sizing: Volatility scaling; hard caps due to tail risk in trending markets.  
Risk management: Reversal strategies can be exposed to sudden “unwinds” and crowding; notable discussions around quant drawdowns (e.g., August 2007) highlight liquidity/impact amplification. citeturn7search6  
Timeframes/instruments: Equities and equity baskets; can exist in futures and FX intraday but costs dominate.  
Indicators/parameters: Lookback (1–20 days), formation/holding overlap, liquidity filters.  
Pros/cons: Can harvest liquidity provision premia; but high turnover and severe crash risk during momentum regimes.  
Performance evidence: Evidence of predictable behaviour and contrarian profits is documented, with caveats about mechanisms. citeturn4search4turn4search16  
Example backtest summary (assumptions: **unspecified**): Daily reversal on large‑cap equities, rebalance daily; costs **unspecified**; expected: small average gains, high sensitivity to spreads/fees, occasional large drawdowns in “momentum crash” regimes.  
Implementation notes:  
```pseudo
daily:
  signal = -return[t-1]
  weight = clip(signal / vol, max_abs)
  apply liquidity filter: ADV > threshold
  trade using limit orders where possible
```

**Strategy: Bollinger Bands / z‑score mean reversion (single instrument or spread)**  
Definition: Enter when price deviates multiple standard deviations from a moving average; exit when it reverts.  
Rationale: Many prices exhibit short‑horizon mean reversion due to liquidity provision, temporary order‑flow imbalances, or bounded range regimes; robust only in certain regimes.  
Math: \(z = \frac{P_t - MA_L}{SD_L}\).  
Entry/exit rules:  
- Long if \(z<-z_0\); short if \(z>z_0\).  
- Exit at \(z\to 0\) or take‑profit at \(z=-z_1\).  
Sizing: Size inversely with volatility; consider convexity to gaps.  
Risk: In trending regimes, “mean” moves; use regime filters (e.g., only trade mean reversion when long‑term trend is flat).  
Backtest summary (assumptions: **unspecified**): z0=2, L=20; costs **unspecified**; expected: frequent small gains, tail losses during breakouts.  
Pseudocode:  
```pseudo
z = (price - MA(price,L)) / stdev(price,L)
if z > z0: short
if z < -z0: long
exit if abs(z) < z_exit or stop hit
```

### Statistical arbitrage and pairs trading  

**Strategy: Classic pairs trading (distance / cointegration style)**  
Definition: Identify two historically similar assets; trade deviations in their relative price, expecting reversion. The classic academic test pairs stocks by minimum distance between normalised prices and trades when spreads widen. citeturn0search9  
Rationale: Temporary mispricing between close substitutes; market‑neutral relative value. citeturn0search9  
Math foundations:  
- Price normalisation; spread \(S_t = \log P^A_t - \beta \log P^B_t\).  
- Trigger on spread z‑score: \(z_t=(S_t-\mu)/\sigma\).  
- Optional cointegration tests (Engle‑Granger) to avoid spurious pairs (parameter choice **unspecified**).  
Entry/exit rules (canonical):  
- Find pairs in formation period.  
- Enter when z exceeds threshold; exit on mean reversion or stop‑loss.  
Position sizing: Dollar neutral (\(+1\) and \(-1\) legs) or beta‑neutral; volatility match legs.  
Risk management:  
- Stop on structural breaks (earnings, index inclusion changes).  
- Limit concentration in correlated clusters.  
Timeframes/instruments: Equity pairs; ETF pairs; futures calendar spreads; ADR/local pairs.  
Indicators/parameters: Formation window, entry z, exit z, maximum holding period.  
Pros/cons: Market‑neutral; but sensitive to transaction costs (two legs) and to regime/structural changes; limited capacity at scale.  
Historical performance evidence: Reported average annualised excess returns up to ~11% for certain historical samples and rules (with transaction cost considerations discussed). citeturn0search9  
Example backtest summary (assumptions: **unspecified**): Daily equities 1962–2002 style methodology; costs **unspecified**; expected: many small trades; strong sensitivity to realistic borrow costs, fees, and slippage. citeturn0search9  
Implementation notes:  
```pseudo
formation:
  for each candidate pair (A,B):
    normalise prices; compute distance metric
  choose top-K pairs with min distance

trading each day:
  spread = log(PA) - beta*log(PB)
  z = (spread - mean)/std
  if z > entry: short spread (short A, long B*beta)
  if z < -entry: long spread
  exit when abs(z) < exit or max_hold reached
```
citeturn0search9  

**Strategy: Equity statistical arbitrage (factor residual / PCA mean reversion)**  
Definition: Build a market‑neutral portfolio that trades mean reversion in **idiosyncratic residuals** after explaining returns with factors (e.g., ETFs/sectors) or PCA. citeturn4search3  
Rationale: Isolate temporary mispricings in residuals rather than overall market direction; diversify across many small bets.  
Math foundations:  
- Model: \(r_{i,t} = \alpha_i + \beta_i^\top f_t + \epsilon_{i,t}\).  
- Trade when \(\epsilon\) is extreme; assume \(\epsilon\) mean‑reverts (often OU‑like).  
- Use optimisation to enforce neutrality constraints: market beta, sector, dollar neutrality.  
Entry/exit: Enter on residual z‑score; exit on decay; manage holding time and turnover.  
Sizing: Risk parity across residual bets; cap per name; penalise illiquidity.  
Risk management: Crowding/unwind demonstrated by quant drawdown narratives; liquidity and correlation spikes are key risks. citeturn7search6turn4search7  
Timeframes: Daily to weekly; HFT versions exist but require microstructure‑grade modelling.  
Performance evidence: Documented model‑driven equity stat‑arb frameworks and their behaviour in crises (including 2007) are discussed in practitioner‑academic literature. citeturn4search3turn7search6  
Example backtest summary (assumptions: **unspecified**): US equities, PCA residuals; costs **unspecified**; expected: relatively stable small returns in normal liquidity, large drawdown risk during crowded deleveraging. citeturn7search6  
Implementation notes:  
```pseudo
fit factor model on rolling window:
  betas = regression(returns, factor_returns)
  residual = return - beta*factor_return
  z = residual / residual_vol
trade:
  long residuals with z<-z0
  short residuals with z>z0
optimise portfolio:
  minimise risk + costs
  subject to: beta_to_market=0, sector_exposure=0, leverage<=Lmax
```
citeturn4search3turn7search6  

### Market making, HFT, and scalping  

**Strategy: Inventory‑aware market making (Avellaneda–Stoikov class)**  
Definition: Post bid/ask quotes around a reference price; manage inventory risk while earning spread and rebates. A canonical model treats market order arrivals as Poisson processes and the mid‑price as diffusive, solving for optimal quotes under utility maximisation. citeturn11search2  
Rationale: Earn compensation for providing liquidity; balance adverse selection risk (informed traders) and inventory risk. Classic microstructure models explain spreads via information asymmetry. citeturn11search0turn11search2  
Math foundations (high level):  
- Midprice: \(dS_t=\sigma dW_t\).  
- Arrival intensities: \(\lambda^{bid}(\delta), \lambda^{ask}(\delta)\) decreasing in quote distance \(\delta\).  
- Control problem: choose quotes to maximise expected utility of terminal PnL; leads to HJB equations and closed‑form approximations in some regimes. citeturn11search2turn11search3  
Entry/exit rules: Always quoting during session; widen or skew quotes with inventory and volatility; retreat under toxic flow.  
Position sizing: Quote sizes depend on inventory, risk budget, queue position, and fill probabilities.  
Risk management:  
- Hard inventory limits; kill switch; volatility circuit breakers.  
- Toxic flow detection; widen spreads.  
Timeframes/instruments: Milliseconds to seconds; equities, futures, FX ECNs; depends on venue.  
Indicators/parameters: Real‑time volatility \(\sigma\), order arrival rates, inventory penalty, queue metrics, microprice/order‑book imbalance.  
Pros/cons: Potentially high Sharpe *if* technology and fees are favourable; but heavy infrastructure, latency arms race, adverse selection, and regime dependence.  
Performance evidence: Theoretical and simulation‑based evidence exists in market‑making literature; “AT improves liquidity” is documented in market structure changes. citeturn11search2turn7search3  
Example backtest summary (assumptions: **unspecified**): LOB simulator, constant \(\sigma\) and Poisson arrivals; costs **unspecified**; expected: stable spread capture in benign regimes, sharp losses in jumpy or toxic regimes.  
Implementation notes:  
```pseudo
loop every Δt:
  S = midprice()
  inv = current_inventory()
  sigma = realised_vol(short_window)
  reservation_price = S - inv * gamma * sigma^2 * (T - t)
  optimal_spread = gamma*sigma^2*(T-t) + (2/gamma)*log(1 + gamma/k)
  bid = reservation_price - optimal_spread/2
  ask = reservation_price + optimal_spread/2
  place/modify limit orders with size rules
  enforce inv limits and kill switch
```
(Representative of Avellaneda–Stoikov style control; parameters \(k,\gamma\) are model‑specific.) citeturn11search2  

**Strategy: High-frequency directional microstructure (order‑flow imbalance / microprice)**  
Definition: Predict very short‑horizon price moves using order book imbalance, microprice, queue dynamics, and short‑term realised volatility; trade with minimal holding periods.  
Rationale: Short‑term price discovery and liquidity dynamics are driven by order flow; empirical microstructure research focuses on these mechanisms. citeturn11search13turn11search1  
Math foundations:  
- Imbalance: \(I_t = \frac{Q^{bid}_t - Q^{ask}_t}{Q^{bid}_t + Q^{ask}_t}\).  
- Microprice: weighted mid by imbalance; predict \(\Delta S\) over horizon \(h\).  
Entry/exit: Enter when predicted edge exceeds costs; exit quickly or on signal flip.  
Sizing: Extremely conservative; size limited by instantaneous liquidity and queue priority.  
Risk: Latency sensitivity, adverse selection, fee changes, and exchange microstructure changes.  
Backtest summary (assumptions: **unspecified**): Tick‑by‑tick LOB; costs **unspecified**; expected: tiny gross edge per trade, dominated by fees, slippage, and adverse selection if not modelled precisely.  
Implementation notes:  
```pseudo
features = {imbalance, microprice, spread, short_vol, recent_trades_sign}
pred = model.predict(features)  # linear/logit/NN
if pred_edge > (fees + expected_slippage):
  cross_or_join_best_quote(direction=sign(pred))
exit after τ or on signal reversal
```

**Strategy: Scalping (spread capture / mean reversion at micro timeframes)**  
Definition: Very short‑horizon trading aimed at capturing small price moves (ticks) repeatedly; often overlaps with market making, but may use more discretionary/conditional entry.  
Key note: In professional systematic contexts, “scalping” must be defined in measurable rules and evaluated primarily through cost models; otherwise it is not a testable strategy.  
Math: Similar to microstructure mean reversion; depends on spread, last trade direction, short‑horizon reversion.  
Risk controls: Hard daily loss limits; kill switch; strict fee/slippage accounting.  
Backtest summary (assumptions: **unspecified**): Tick data, hold < 1 minute; costs **unspecified**; expected: net results highly sensitive to fees and fill assumptions.

### Options, volatility, delta-neutral, and volatility arbitrage  

**Strategy: Covered call / buy‑write (systematic call selling)**  
Definition: Hold an equity basket (e.g., S&P 500 proxy) and systematically sell near‑dated call options against it (covered). Cboe’s BXM methodology is an explicit benchmark for such a strategy on the S&P 500. citeturn6search2turn13search5  
Rationale: Harvest option premium (part of volatility risk premium) and reduce equity volatility; exchange upside for income.  
Math foundations:  
- PnL ≈ equity return + call premium − payoff of short call.  
- Option pricing & Greeks: call value and delta exposures are grounded in derivatives pricing frameworks (e.g., Black–Scholes). citeturn5search0  
Entry/exit rules: Sell 1‑month call at specified moneyness on roll date; hold until expiry/settlement; re‑sell next cycle. citeturn6search2turn13search5  
Position sizing: Typically “1 call per equity notional” (covered) at index level; scale by risk budget.  
Risk management:  
- Tail risk: short convexity (gamma negative) near strikes; manage gap risk.  
- Avoid over‑levering; ensure liquidity around rolls (OPEX).  
Timeframes/instruments: Monthly; index options (SPX), equity options, covered call ETFs/notes.  
Indicators/parameters: Strike selection (ATM/OTM), roll schedule, dividend handling, margin rules.  
Pros/cons: Lower volatility and potential income; capped upside; can lag in strong bull runs; complex tax/margin and assignment (single stock options).  
Performance evidence: Cboe publishes methodology and historical evaluation materials; academic/industry analyses examine risk/return of buy‑write benchmarks. citeturn13search4turn13search0turn6search2  
Example backtest summary (assumptions: **unspecified**): Monthly ATM call write on S&P 500 proxy; costs **unspecified**; expected: reduced volatility vs equity, reduced right‑tail, premium income partially offsets drawdowns. citeturn13search4turn6search2  
Implementation notes:  
```pseudo
on monthly roll date:
  hold equity_index_notional = 1.0
  sell_call(strike=ATM, expiry=1m, notional=equity_notional)
daily:
  mark-to-market equity + option
at expiry:
  settle option; repeat
```
citeturn6search2turn13search5  

**Strategy: Cash‑secured put writing / put‑write (systematic put selling)**  
Definition: Sell 1‑month ATM index puts while holding collateral (T‑bills/cash) sufficient to cover potential assignment; Cboe’s PUT methodology describes precisely such an index strategy. citeturn6search3  
Rationale: Monetise volatility risk premium / insurance demand; earn premium for bearing downside risk.  
Math foundations: Short put payoff is strongly left‑tailed; risk resembles leveraged equity exposure in crash regimes.  
Entry/exit rules: Monthly roll; sell ATM put; invest collateral. citeturn6search3  
Sizing/risk: Strict collateralisation; cap leverage; stress test crash scenarios.  
Performance evidence: PUT‑style benchmarks are standard references for systematic put selling; methodology is official and detailed. citeturn6search3  
Example backtest summary (assumptions: **unspecified**): Monthly ATM put sale; costs **unspecified**; expected: steady premium in calm markets, severe drawdowns in crashes.  
Pseudocode:  
```pseudo
monthly:
  invest collateral in T-bills
  sell 1m ATM put with notional <= collateralised_limit
risk:
  if drawdown > threshold: reduce notional / stop
```
citeturn6search3  

**Strategy: Long straddle / strangle (long volatility)**  
Definition: Buy call+put (straddle) or OTM call+put (strangle) to gain from realised volatility exceeding implied volatility and/or directional jumps.  
Rationale: Long convexity; can benefit from event risk; but typically pays a carry cost when implied exceeds realised.  
Math foundations: Derivatives pricing via Black–Scholes/CRR; Greeks: delta‑neutral at entry, positive gamma, negative theta. citeturn5search0turn5search5  
Entry/exit rules: Enter ahead of catalysts (earnings, macro releases) or when implied vol is low vs expected realised; exit on vol expansion or time stop.  
Sizing: Small relative to capital; manage Vega risk.  
Risk controls: Limit bleed (theta); avoid buying vol at extremes without catalyst.  
Example backtest summary (assumptions: **unspecified**): Buy 30‑day straddles daily/weekly; costs **unspecified**; expected: negative average carry with occasional large gains; regime dependent.  
Pseudocode:  
```pseudo
if implied_vol < forecast_realised_vol - buffer:
  buy ATM call + buy ATM put
delta-hedge periodically if targeting pure vol exposure
exit on profit target or when time_to_expiry < τ_min
```

**Strategy: Vertical spreads (debit/credit spreads)**  
Definition: Express directional view with limited risk by buying one option and selling another at different strike (same expiry).  
Rationale: Tailor convexity and premium; reduce theta cost vs outright long option; or harvest premium with capped loss vs naked short.  
Math: Net option payoff is piecewise linear; Greeks are difference of legs.  
Entry/exit: Choose strikes by delta, expected move, and implied skew.  
Risk: Gap risk still exists; liquidity can be worse for far strikes.  
Backtest summary (assumptions: **unspecified**): Monthly bull call spreads; costs **unspecified**; expected: limited upside, limited loss, sensitive to vol surface dynamics.

**Strategy: Calendar spreads (time spreads)**  
Definition: Sell near‑dated option and buy longer‑dated option (or vice versa) at same strike.  
Rationale: Trade term structure of implied volatility and theta decay; can be used as relative value in vol surface.  
Math: Vega/theta profile depends on maturities; exposure to implied term structure.  
Backtest summary (assumptions: **unspecified**): Enter when near‑term IV rich vs back; costs **unspecified**; expected: profits from convergence but high gamma risk near expiry.

**Strategy: Delta‑neutral volatility trading (buy option, hedge delta; “gamma scalping”)**  
Definition: Hold options (often long gamma) and dynamically delta‑hedge; profit if realised variance exceeds implied variance after costs.  
Rationale: Options embed implied variance; delta hedging transforms option exposure into a variance‑like payoff under idealised assumptions. Literature and practitioner notes discuss volatility trading approaches and variance replication. citeturn5search2turn5search3turn6search9  
Math foundations:  
- In continuous time, delta‑hedged option PnL relates to gamma and realised variance; variance swap replication via option strips is a key conceptual tool. citeturn6search9turn5search2  
Entry/exit: Enter when implied variance is “cheap” vs forecast; rebalance hedge on schedule; exit on vol spike or time stop.  
Sizing: Vega‑based; small due to tail risks and transaction costs (hedging).  
Risk: Discrete hedging error; jumps; transaction costs; model risk.  
Example backtest summary (assumptions: **unspecified**): Buy 30‑day ATM options; delta hedge daily; costs **unspecified**; expected: sensitive to hedging frequency and bid‑ask; profits in high realised volatility regimes.  
Pseudocode:  
```pseudo
enter:
  buy option with target vega exposure
hedge loop:
  delta = option_delta(S, IV, t)
  trade_underlying(-delta)  # to stay delta-neutral
exit:
  if realised_vol regime ends or time_to_expiry small: close
```
citeturn5search0turn6search9  

**Strategy: Variance / volatility swaps and volatility risk premium harvesting**  
Definition: Trade variance directly (variance swaps) or volatility swaps; replication links to weighted option portfolios. citeturn6search9turn5search2  
Rationale: Systematic difference between implied and realised variance (variance risk premium) has explanatory power and is central to many vol strategies. citeturn6search0turn6search9  
Math foundations: Variance swap fair strike approximated via option strip (model‑light replication). citeturn6search9turn5search2  
Risk: Tail losses when short variance; convex gains when long variance.  
Example backtest summary (assumptions: **unspecified**): Short 1‑month variance on equity index; costs **unspecified**; expected: steady gains in calm markets, crash losses. citeturn6search0  

### Carry trades and macro/global macro  

**Strategy: FX carry (interest differential / forward discount trade)**  
Definition: Long high‑interest‑rate currencies and short low‑interest‑rate currencies (often via forwards), capturing interest differentials but bearing crash risk.  
Rationale & evidence: Currency markets exhibit common risk factors related to carry trade returns; carry premia are linked to global risk. citeturn3search0turn3search12  
Math foundations:  
- Forward discount: \(f_t - s_t\) relates to interest differential under covered interest parity.  
- Carry return approximated by interest differential plus spot move.  
Entry/exit: Periodic rebalance (e.g., monthly) by ranking interest rates/forward discounts; optional risk‑off filters.  
Sizing: Volatility scaling; strict drawdown controls; avoid illiquid EM if costs not modelled.  
Risk: “Carry crashes” during risk‑off; correlation spikes.  
Example backtest summary (assumptions: **unspecified**): G10 carry baskets, monthly rebalance; costs **unspecified**; expected: positive average returns punctuated by sharp drawdowns in crises. citeturn3search12  
Pseudocode:  
```pseudo
monthly:
  rank currencies by interest_rate_diff
  long top K, short bottom K
  scale by vol and cap per currency
  apply risk-off filter (e.g., reduce when global vol high)
```
citeturn3search12  

**Strategy: Cross‑asset carry (term structure / roll yield in futures)**  
Definition: Hold assets with positive carry (backwardation / favourable roll yield) and short assets with negative carry (contango), broadly across futures.  
Rationale & evidence: Carry can be measured in advance and studied across asset classes; research decomposes expected returns and documents carry as a major component distinct from value/momentum. citeturn3search13turn3search5  
Entry/exit: Rank by carry measure; rebalance monthly/weekly.  
Sizing: Risk parity; diversify across asset classes.  
Risk: Carry can be a disguised exposure to crisis risk (e.g., short volatility / liquidity).  
Example backtest summary (assumptions: **unspecified**): Futures carry factor across commodities/rates/FX/equity index futures; costs **unspecified**; expected: diversifying premia but regime‑dependent. citeturn3search13turn3search5  

**Strategy: Systematic global macro (signals: trend + carry + value)**  
Definition: Multi‑signal allocation across asset classes using systematic rules (often combining TSMOM, carry, value).  
Rationale: Evidence for trend, value, momentum, and carry across markets motivates multi‑signal diversified portfolios. citeturn7search12turn7search1turn3search13  
Math: Weighted combination \(w \propto a\cdot trend + b\cdot carry + c\cdot value\), followed by risk budgeting (risk parity, volatility targeting).  
Risk controls: Cross‑asset correlation regime shifts; tail events; leverage/margin constraints.  
Example backtest summary (assumptions: **unspecified**): 50–100 futures, monthly rebalance; costs **unspecified**; expected: diversified return stream, lower correlation to 60/40 in some studies, but not guaranteed. citeturn7search12turn3search13  

### Event-driven strategies  

**Strategy: Earnings drift (post‑earnings announcement drift; PEAD)**  
Definition: Trade the tendency for stocks with positive earnings surprises to continue outperforming and negative surprises to underperform after announcements.  
Rationale & evidence: PEAD is a long‑studied phenomenon; classic research investigates delayed reaction vs risk premia explanations. citeturn3search3turn3search15  
Math foundations: Standardise surprise \(ES = \frac{EPS_{actual}-EPS_{expected}}{\sigma(EPS)}\). Form long/short portfolios by surprise quantiles; hold for weeks/months.  
Entry/exit: After earnings release; hold for fixed horizon; avoid immediate microstructure noise by waiting a defined delay (**unspecified**).  
Sizing: Beta/sector neutralise; limit single‑name idiosyncratic risk.  
Risk: Earnings guidance changes; crowding; short borrow costs.  
Example backtest summary (assumptions: **unspecified**): Long top‑surprise decile, short bottom; hold 60 trading days; costs **unspecified**; expected: positive drift if phenomenon persists, but sensitive to implementation frictions. citeturn3search3  
Pseudocode:  
```pseudo
on earnings day:
  surprise = standardised_EPS_surprise()
weekly/monthly:
  long high surprise, short low surprise
  hedge market beta
  exit after H days
```

**Strategy: Merger arbitrage (risk arbitrage; M&A event-driven)**  
Definition: When a deal is announced, buy target (and sometimes short acquirer in stock deals) to capture the spread between current price and deal consideration, bearing deal‑break risk.  
Rationale & evidence: Large-sample analysis characterises risk/return of risk arbitrage and documents state dependence (e.g., behaviour in sharply falling markets). citeturn3search2turn3search14  
Math foundations:  
- Spread \(s = \frac{OfferPrice - P_{target}}{P_{target}}\).  
- Expected return \(\approx p\cdot s - (1-p)\cdot loss\) over expected time to close; estimate \(p\) (deal completion probability) and downside.  
Entry/exit: Enter after announcement (avoid initial volatility); exit on completion, stop‑loss on adverse news, or time stop.  
Sizing: Size by estimated downside and liquidity; diversify across deals.  
Risk: Gap risk; regulatory risk; financing risk; correlated deal breaks in crises.  
Example backtest summary (assumptions: **unspecified**): Diversified US deals 1963–1998 style; costs **unspecified**; expected: positive average returns, but exposure becomes market‑like in severe downturns in empirical findings. citeturn3search2turn3search14  

### Quantitative systematic and machine learning strategies  

**Strategy: Multi‑factor / systematic equity long‑short (value, momentum, quality, low beta)**  
Definition: Combine multiple equity factors into a diversified long‑short portfolio with explicit neutrality constraints.  
Rationale & evidence:  
- Value and size effects documented in asset pricing literature; multi‑factor frameworks formalise common drivers. citeturn2search0turn2search1  
- Five‑factor extensions incorporate profitability and investment. citeturn8search0turn8search4  
- Momentum is a major factor in four‑factor models. citeturn1search5  
- Quality and low‑beta effects also have dedicated research streams. citeturn8search3turn8search1  
Math foundations:  
- Score each stock on factor z‑scores; build composite score.  
- Optimise portfolio to maximise expected factor return subject to risk/turnover constraints; or use simple rank‑weighting.  
Entry/exit: Rebalance monthly/weekly; manage turnover; incorporate costs.  
Sizing: Risk model (covariance) + risk budgeting; neutralise exposures (market beta, sectors, industries).  
Risk: Crowding, factor crashes, model risk, liquidity.  
Example backtest summary (assumptions: **unspecified**): US large‑cap universe; monthly rebalance; costs **unspecified**; expected: smoother long‑short returns than single factor, but correlated drawdowns when factors unwind. citeturn7search1turn8search0  
Implementation notes:  
```pseudo
for each rebalance:
  compute factor signals: value, momentum, profitability/quality, low_beta
  composite = weighted_sum(zscores)
  long = top_quantile(composite)
  short = bottom_quantile(composite)
  run optimiser:
    maximise expected_return(composite)
    subject to: beta=0, sector bounds, turnover limit, position limit
```

**Strategy: Supervised ML return prediction (cross‑sectional)**  
Definition: Use supervised learning (trees, regularised regression, neural networks) to predict expected returns or ranks, then trade a portfolio based on predicted scores.  
Rationale & evidence: Comparative research in empirical asset pricing finds ML methods can improve predictive performance and show economic gains in some settings, with trees/NNs performing strongly in certain benchmarks. citeturn9search0turn9search8  
Math foundations:  
- Predict \( \hat{r}_{i,t+1} = f_\theta(x_{i,t}) \) where \(x\) includes momentum, liquidity, volatility, etc.  
- Portfolio: long top‑predicted, short bottom‑predicted; or mean‑variance optimisation using predicted returns.  
Entry/exit: Usually periodic (weekly/monthly) due to costs; retrain on rolling window; strict leakage control.  
Position sizing: Risk model + turnover penalty; robust optimisation; conservative shrinkage of forecasts.  
Risk management: Overfitting, non‑stationarity, feature drift, hidden exposures; require strong validation.  
Example backtest summary (assumptions: **unspecified**): Predict next‑month returns on equities; rolling retrain; costs **unspecified**; expected: gains vs linear baseline may appear in some periods, but stability depends on data pipeline and regime. citeturn9search8  
Implementation notes:  
```pseudo
walk-forward:
  train = data[t-W : t]
  model.fit(train.features, train.forward_returns)
  preds = model.predict(current.features)
  portfolio = build_long_short(preds, constraints, costs)
  execute portfolio
monitor:
  feature drift, prediction decay, turnover, realised vs expected
```
citeturn9search0  

**Strategy: Reinforcement learning (RL) trading / control**  
Definition: Frame trading as a sequential decision / control problem with states (positions, volatility, inventory), actions (trade sizes), and rewards (PnL net of costs); learn policy to maximise objective (often risk‑adjusted).  
Rationale & evidence: RL has been proposed for trading where transaction costs, impact and path dependence matter; classic work discusses direct reinforcement learning with risk‑adjusted objectives. citeturn9search9turn9search1  
Math foundations: Markov decision process; policy optimisation to maximise expected cumulative reward; objective may incorporate Sharpe‑like metrics or differential Sharpe updates (implementation dependent). citeturn9search1  
Risk: Non‑stationarity breaks policies; simulation‑to‑real gap; overfitting to backtest simulator.  
Example backtest summary (assumptions: **unspecified**): Simulated market with costs; expected: policy sensitive to environment model; robust deployment requires conservative constraints.  
Implementation notes:  
```pseudo
state = {prices, indicators, position, cash, vol, drawdown}
action = trade_size (continuous) or {buy/hold/sell}
reward = pnl - costs - risk_penalty(drawdown, var)
train policy with off-policy RL on historical + simulator
deploy with strict risk limits and human kill switch
```
citeturn9search1turn9search9  

**Strategy: Deep learning on limit order books (LOB prediction)**  
Definition: Use deep architectures (CNN/LSTM) on LOB snapshots to predict short‑horizon price movements; a prominent model class is DeepLOB. citeturn9search2turn9search6  
Rationale: LOB has spatial (price levels) and temporal dynamics; CNNs capture spatial structure, LSTMs capture temporal dependence. citeturn9search2  
Key warning: Even with strong classification accuracy, converting predictions to profitable strategies depends on costs, latency, and execution assumptions.  
Example backtest summary (assumptions: **unspecified**): LSE quotes dataset, horizon **unspecified**; costs **unspecified**; expected: prediction accuracy does not guarantee trading profitability. citeturn9search2  

## Portfolio construction, optimisation, and execution algorithms  

### Portfolio construction and optimisation  

Modern strategy research is inseparable from portfolio construction. Three foundational paradigms:

**Mean–variance optimisation (Markowitz)**  
- Objective: maximise expected return for a given variance (or minimise variance for given return). citeturn1search2  
- Problem (typical form):  
  \[
  \min_{w} \; w^\top \Sigma w \;\;\text{s.t.}\;\; \mu^\top w \ge \mu_0,\; \sum w=1
  \]
- Practical issues: estimation error in \(\mu\) and \(\Sigma\); instability; need for shrinkage and constraints.

**Black–Litterman (Bayesian blending of equilibrium and views)**  
- Motivation: avoid “extreme weights” from naive mean–variance; combine implied equilibrium returns and investor views with confidences. citeturn1search11  
- Practical role: macro and multi‑asset portfolios often use BL to encode views without unstable allocations.

**Risk parity (risk budgeting)**  
- Idea: allocate so that each asset/class contributes equally (or per budget) to total portfolio risk; widely used as an alternative to capital‑weighting. citeturn2search3  
- Core concept: equalise marginal risk contributions \(RC_i\) where \(RC_i = w_i (\Sigma w)_i / \sqrt{w^\top \Sigma w}\).

**Kelly criterion (growth optimal sizing)**  
- Goal: maximise expected log growth; classic treatment ties optimal betting to information rate. citeturn2search2  
- In practice: use fractional Kelly due to estimation error and drawdown aversion.

### Execution algorithms and transaction cost analysis  

Execution is where many strategies succeed or fail. Two cornerstone concepts:

**Implementation shortfall (Perold)**  
- Measures total slippage between decision price and realised execution, capturing explicit and implicit costs. citeturn10search10turn10search2  

**Almgren–Chriss optimal execution**  
- Optimises trade schedule balancing expected market impact vs risk during execution, modelling temporary and permanent impact. citeturn0search7  

Common execution algorithms (rule‑based, not mutually exclusive): TWAP, VWAP, POV (participation), Implementation Shortfall algorithms, liquidity seeking and adaptive strategies. In professional settings, these are selected based on urgency, market impact, and risk, consistent with the “best execution” framing used in formal work. citeturn0search7turn10search6  

Representative execution pseudocode (cost‑aware):  
```pseudo
order = target_position - current_position
if urgency == low:
  schedule = TWAP(order, horizon)
elif benchmark == VWAP:
  schedule = follow_volume_curve(order)
else:
  schedule = AlmgrenChriss(order, impact_params, risk_aversion)

for slice in schedule:
  place passive if spread wide and fill_prob high
  otherwise cross partially
  update impact estimates and re-optimise
```
citeturn0search7turn10search10  

## Risk controls, metrics, and robustness standards  

### Risk measurement and controls  

Professional trading systems implement layered controls:

- **Position limits**: max per instrument, per sector, per factor, per venue; leverage/margin limits.  
- **Liquidity limits**: trade size as fraction of ADV/volume; avoid forced liquidation.  
- **Drawdown‑based de‑risking**: reduce exposure after peak‑to‑trough drawdown thresholds (config **unspecified**).  
- **Factor and beta constraints**: especially for stat‑arb and long‑short portfolios.  
- **Scenario and stress tests**: historical crises, liquidity freezes, volatility spikes.  
- **Tail risk management**: recognise convexity; short‑vol strategies (put‑write, market making) require explicit crash protocols. citeturn6search3turn11search2  

Widely used risk measures include VaR frameworks and Expected Shortfall (ES). The RiskMetrics technical documentation is a major historical reference for VaR-style market risk measurement in practice. citeturn10search0  
Expected Shortfall has been developed and analysed as a coherent risk measure alternative to VaR in academic work and has become central in banking capital frameworks (e.g., Basel market risk standards emphasise ES in internal models approaches). citeturn10search1turn12search3  

### Performance metrics and evaluation  

- **Sharpe ratio**: reward‑to‑variability; Sharpe emphasises time dependence and ex‑ante vs ex‑post interpretation. citeturn12search4  
- **Sortino ratio**: downside deviation variant; useful for skewed strategies (short‑vol, covered calls). citeturn12search13turn12search1  
- **Tail metrics**: maximum drawdown, Calmar-style frameworks (definitions vary by source; treat as descriptive rather than definitive).  
- **Transaction cost attribution**: implementation shortfall decomposition and post‑trade TCA. citeturn10search10turn10search6  

Robustness checklist (high-level):  
- Walk‑forward / rolling out‑of‑sample validation; avoid leakage.  
- Conservative cost modelling; stress slippage (especially for high turnover).  
- Parameter stability tests (sweep ranges, not single “best” point).  
- Regime analysis (trend vs chop, vol regimes).  
- Capacity analysis (market impact scaling).  
- “Crowding” stress tests (synchronised de‑risking), motivated by historical quant deleveraging discussions. citeturn7search6  

## Comparison tables and selection framework  

### Strategy comparison table (indicative; depends heavily on implementation)  

**Important**: Sharpe ranges below are **illustrative heuristics**, not guarantees. Many strategies (especially HFT) have limited public disclosure; where literature provides stronger evidence, this document cites it explicitly.  

| Strategy family | Typical holding | Capital requirement | Liquidity needs | Complexity | Typical Sharpe (indicative) | Core risks |
|---|---:|---:|---:|---:|---:|---|
| Trend / TSMOM (diversified futures) | days–months | medium–high (margin) | high | medium | ~0.5–1.0 (illustrative) | choppy regimes; reversals; leverage |
| Breakout / MA trend | days–months | low–medium | medium–high | low–medium | ~0.3–0.8 (illustrative) | whipsaws; gaps |
| Cross‑sectional momentum | weeks–months | medium | medium–high | medium | ~0.4–1.0 (illustrative) | momentum crashes; turnover |
| Mean reversion (single asset) | minutes–days | low–medium | high | low–medium | ~0.2–0.8 (illustrative) | trend regimes; tail breaks |
| Pairs trading | days–weeks | medium | medium–high | medium | ~0.5–1.2 (illustrative) | structural breaks; two‑leg costs citeturn0search9 |
| Equity stat‑arb (factor residual) | days–weeks | high | high | high | ~0.5–1.5 (illustrative) | crowding/unwinds citeturn7search6turn4search3 |
| Market making | seconds–minutes | high | very high | very high | often high if viable (not public) | adverse selection; tech/fee risk citeturn11search2turn11search0 |
| Options buy‑write (covered calls) | monthly | low–medium | high | medium | ~0.3–0.8 (illustrative) | capped upside; crash risk dynamics citeturn6search2turn13search4 |
| Put‑write (short puts) | monthly | medium–high (collateral) | high | medium | ~0.3–1.0 (illustrative) | left-tail crashes citeturn6search3 |
| Long vol (straddles) | days–weeks | low | high | medium | often low/negative carry | theta bleed; entry timing citeturn5search0 |
| FX carry | weeks–months | low–medium | medium–high | medium | ~0.3–1.0 (illustrative) | carry crashes; risk-off citeturn3search12 |
| Event-driven earnings drift | weeks–months | medium | medium | medium | varies | decay; costs; crowding citeturn3search3 |
| Merger arb | weeks–months | medium–high | medium | high | varies | deal breaks; crisis beta citeturn3search2 |
| ML cross‑sectional forecasting | weeks–months | medium–high | medium–high | high | varies | overfit; drift citeturn9search0 |
| LOB deep learning / HFT signals | ms–seconds | very high | very high | very high | not reliably public | latency, costs, microstructure shifts citeturn9search2 |

### Strategy selection framework (practical)  

When choosing among strategies, professional desks usually prioritise:

- **Edge source clarity**: risk premium (carry/VRP), behavioural anomaly (momentum/PEAD), microstructure (market making).  
- **Capacity and liquidity**: high turnover strategies saturate quickly; impact rises super‑linearly with size.  
- **Operational realism**: data availability, execution infrastructure, borrow availability, legal/venue constraints.  
- **Risk shape**: convexity vs concavity (e.g., long trend can be crisis‑helpful; short vol often carries crash risk). citeturn7search12turn6search3  
- **Diversification**: combine low‑correlated premia (trend, carry, value/momentum, and selective vol exposures) with explicit risk budgeting. citeturn7search12turn7search1turn3search5  

## Primary and seminal sources referenced  

The list below is intentionally weighted toward seminal papers and high‑quality institutional sources (methodology documents, NBER/peer‑reviewed articles).  

- Trend / time‑series momentum: Moskowitz, Ooi & Pedersen, “Time Series Momentum” (JFE, 2012). citeturn0search0turn0search8  
- Long-horizon trend evidence: Hurst, Ooi & Pedersen, “A Century of Evidence on Trend‑Following Investing” (JPM, 2017). citeturn7search12turn7search16  
- Cross‑sectional momentum: Jegadeesh & Titman (1993). citeturn1search0turn1search4  
- Value and momentum across markets: Asness, Moskowitz & Pedersen (2013). citeturn7search1  
- Classic pairs trading: Gatev, Goetzmann & Rouwenhorst (RFS, 2006). citeturn0search9turn0search1  
- Equity stat‑arb framework: Avellaneda & Lee (Quantitative Finance, 2010). citeturn4search3  
- Quant crowding/unwind risk: Khandani & Lo (NBER w14465; published later). citeturn7search2turn7search6  
- Market making models: Avellaneda & Stoikov, “High‑frequency trading in a limit order book” and extensions. citeturn11search2turn11search3  
- Microstructure and spreads: Glosten & Milgrom (1985); Hasbrouck (book/online materials). citeturn11search0turn11search13  
- Options pricing foundations: Black & Scholes (1973); Cox, Ross & Rubinstein (1979). citeturn5search0turn5search5  
- Volatility/variance trading & swaps: Demeterfi et al. (1999); Carr & Madan (volatility trading notes); Carr & Wu (variance risk premia). citeturn5search2turn5search3turn6search9  
- Variance risk premium and equity returns: Bollerslev, Tauchen & Zhou (RFS, 2009). citeturn6search0turn6search12  
- Options strategy benchmarks: Cboe methodology for BXM (buy‑write) and PUT (put‑write). citeturn6search2turn6search3  
- Carry across asset classes: Koijen, Moskowitz, Pedersen & Vrugt (NBER w19325; later publication forms). citeturn3search5turn3search13  
- Currency carry risk factors: Lustig, Roussanov & Verdelhan (RFS, 2011; NBER). citeturn3search0turn3search12  
- Merger arbitrage: Mitchell & Pulvino (JF, 2001). citeturn3search2turn3search14  
- Portfolio optimisation fundamentals: Markowitz (1952); Black–Litterman (1992); risk parity (Qian, 2005); Kelly (1956). citeturn1search2turn1search11turn2search3turn2search2  
- Execution & transaction costs: Almgren–Chriss (optimal execution); Perold (implementation shortfall). citeturn0search7turn10search10  
- ML strategies: Gu, Kelly & Xiu (Empirical Asset Pricing via ML); RL trading references; DeepLOB for LOB learning. citeturn9search0turn9search1turn9search2  
- Risk measurement: RiskMetrics technical documentation; Expected Shortfall coherence; Basel market risk standards emphasising ES. citeturn10search0turn10search1turn12search3