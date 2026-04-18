# Documentation Corrections Summary

This document catalogs the narrow, evidence-based corrections made to `COMPLETE_PROJECT_DOCUMENTATION.md` to perfectly align it with the exact Python implementation verified during the AQRR Strategy Compliance Audit.

## Correction 1

- **Changed Section**: `9.6 Candidate Ranking & Selection`
- **Previous Wording / Claim**: 
  > "- Correlation conflict check (prevents three highly correlated altcoin longs, for example)"
- **Corrected Wording / Claim**: 
  > "- Correlation conflict check (dynamically evaluates the rolling 72-hour Pearson correlation against existing candidate returns, discarding setups that breach `correlation_reject_threshold`)
  > - Thematic cluster limits (strictly limits concurrency of explicitly categorized beta clusters to `max_cluster_exposure`)"
- **Evidence / Source for Correction**: 
  `backend/app/services/strategy/aqrr.py` (Lines 190, 1800-1940)
  The method `_correlation(left, right)` implements the exact statistical formula for Pearson product-moment coefficient using the sum of the products of differences (`sum(a * b for a, b in zip(left_diff, right_diff)) / ((left_scale * right_scale) ** 0.5)`). In `select_candidates()`, this mathematical filter runs side-by-side with the `candidate_cluster` grouping caps.
- **Reason for Change**: 
  The original documentation severely understated the rigor of the codebase. It implied correlation filtering was merely a loose grouping mechanism, whereas the implementation successfully executes the complex dynamic statistical rolling arrays mandated by the formal AQRR Strategy Specification.

## Correction 2

- **Changed Section**: `19. Known Constraints & Risks` -> `Design Gaps / Future Work`
- **Previous Wording / Claim**: 
  > "- **Correlation filter** — the spec describes a rolling correlation filter; the implementation uses lighter-weight thematic / beta clustering checks."
- **Corrected Wording / Claim**: 
  > *(Bullet point completely deleted)*
- **Evidence / Source for Correction**: 
  `backend/app/services/strategy/aqrr.py` (Lines 190, 1800-1940)
  As established above, the code actively computes Pearson correlations using the `returns_1h` historical slices on the fly to reject correlations breaching `config.correlation_reject_threshold`.
- **Reason for Change**: 
  The bullet point was a direct contradiction to the active operational code logic. The "Design Gap" does not exist; rolling correlation is fully implemented. Removing this restores accuracy.
