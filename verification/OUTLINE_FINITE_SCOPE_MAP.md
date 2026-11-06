# Outline v3 finite scope map

Read-only at HEAD `2a3729b2b7f042ab3ac63979844134fca65398d4`.

## Reachable calls

Canonical inputs expose `P` papers, `U` research units, `C` unique typed claims, `F` source fields, `D` qualifier dependencies, `T` topic/outlier tasks, and `R` selected relation bundles. Topic planning is source-derived with ≤5 cross-group questions. Relations use shared evidence labels, not all pairs; >2,000 pairs for one label adds a blocking diagnostic; selected bundles target 24k tokens and unselected bundles stay local. The semantic call plan lists navigation/topic/cross/relation work with `expected_physical_calls=None`; its declared provider-plan scope is `navigation_and_relation_candidates_only`, not a whole-stage bound. (`semantic_chunking.py:1764,1790,1899,1950`; `v3_relations.py:214`.)

For fresh decision variant `v`:

```text
N(v) = A_rel(v) + Σ_i S_candidate(i,v) + Σ_enabled critics q C_q(v) + A(v) + R(v)
```

`S_candidate` is the `_candidate_shard_requests` count. `C_q=1` when a critic fits flat, otherwise it is the sum of complete-section shards across candidates. `A_rel` is 0/1 when empty/flat, otherwise actual local plus cross/atomic batches. `A` is one arbitration call if candidates remain; coordination is in it. `R(v)≤K` is conditional repair per candidate, counted per fresh stability variant. (`v3_executor.py:5007,8606,8646,9020,12282,12782,15236`.)

Stability adds 0 fresh chains off, 1 in smoke, and 5 perturbation chains in full; baseline reuse/exact replay add 0 POSTs. Recompute shards per variant. The repair exposure is primary-only; stability-prefixed repair callbacks need per-variant bounds too. Candidate scope is inconsistent: executor clamps `candidate_count≤12` and reserves that many IDs, but plans emit only `min(requested,5)` from five fixed axes. Honor 6–12 by preserving those axes and adding distinct directions (e.g. ordered axis pairs with explicit dimensions/relations); share one candidate list for plan and accounting. (`v3_executor.py:387,975,3975,16333`; `v3_relations.py:56,440`.)

## Output bounds and minimum contract

Candidate validation checks nonempty sections/claims, unique section IDs, allowed paper/relation IDs, and source support, but no maximum section, claim, or support-reference counts. Flat/sharded outputs cap tokens at `min(profile,4096)`/`min(profile,1024)`; critics/arbitration at `min(profile,2048)`. Tokens do not bound record counts. Repair preserves section count/order/IDs and cannot increase claims, but does not cap the initial candidate. Coordination exact-covers source sections with disjoint hash-checked merges, so final sections ≤ input sections. (`v3_executor.py:12521,12782,8778,9086,14803,16010,8883`.)

Extend the existing plan with source-derived `section_slots` from topic/outlier tasks and selected bundles. Each slot needs a stable task ID, allowed papers/relations, and `claim_slots`; each claim slot carries primary/qualifier IDs, evidence/field IDs, and its support closure. Incomplete sources remain unresolved. Enforce `max_sections=len(slots)`, `max_claims=Σ claim slots`, and `max_support_rows=Σ closure rows` before persistence/merge and in critic/arbitration output. Existing packers then yield per-variant shard counts; sum these with fresh stability variants and conditional repairs for a content-derived whole-stage bound without `P×P` expansion.
