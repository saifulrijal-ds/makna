Questions that repeat the synonyms make the keyword list look best, so choose the model and the weights on paraphrased questions or at least measure them there, and keep the tuning code unable to read the held-out file.

Seen in WP-14C (2026-11-16), 51 golden questions against 46 held-out ones written from the business meanings of the metrics.

- Keyword only scores 0.976 on the golden questions and 0.783 on the held-out ones. Vector only scores 0.786 and 0.587 with the default MiniLM model, but 0.905 and 0.935 with `multilingual-e5-large`. The golden set cannot tell the two models apart in the fused search (0.976 both) and the held-out set can (0.783 and 0.935).
- With e5 the rule picked a vector to keyword weight ratio of 0.75 on the golden questions, while the vector list alone beats the fused search on the held-out and the review's hard questions (0.935 against 0.935; 0.850 against 0.675). A tuned split that repeats synonyms will keep pulling the weights toward the keyword list; give it paraphrased questions before a stronger model is tuned.
- The guard that keeps tuning off the held-out file is an import walk, not a convention: `tests/unit/test_eval_heldout.py` follows every `evals.*` import of `evals.tune` and fails if the held-out loader is among them. A guard on one file read would miss a helper that reads the file for another reason.
- fastembed does not add the e5 prefixes (`query: `, `passage: `); the eval scripts did, and without them held-out recall at 3 fell from 0.935 to 0.913. The Makna provider has no setting for them yet (DR-9).
