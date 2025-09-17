
Results:
We tested our optimization on 47 benchmarks from the benchmarks/core folder (you can find them in the test_suite)
Results for DCE:
Analysis complete. Summary: {'Total Benchmarks': 47, 'Improved': 18, 'Unchanged': 29, 'Regressed': 0, 'Incorrect Results (tdce)': 0, 'Missing Results (tdce)': 0, 'Timeout Results (tdce)': 0, 'Total Absolute Improvement': 208, 'Average Improvement': 4.43, 'Mean Percent Improvement': 0.38}

Results for LVN:
Analysis complete. Summary: {'Total Benchmarks': 47, 'Improved': 15, 'Unchanged': 21, 'Regressed': 0, 'Incorrect Results (LVN)': 0, 'Missing Results (LVN)': 0, 'Timeout Results (LVN)': 11, 'Total Absolute Improvement': 2104546.0, 'Average Improvement': 58459.61, 'Mean Percent Improvement': 14.96}

GAI Tools Disclaimer

We used OpenAI’s ChatGPT and GitHub Copilot for the following aspects of the assignment:

Uses of ChatGPT:

Debugging dead code elimination (DCE): ChatGPT helped identify why control-flow-critical variables like not_finished were mistakenly eliminated, particularly in SSA-style loops. This led to several iterations on refining our global variable detection and preservation logic.

Improving correctness of LVN optimization: It was especially helpful in discussing how to retain semantically necessary id instructions by canonicalizing them while still enabling substitution for common subexpressions.

Writing helper functions for block-level DCE: Suggestions around split_blocks, join_blocks, and identifying live-in variables across blocks helped improve modularity and correctness of our optimization passes.

Constructing the Python script for analyzing benchmark results: ChatGPT helped write the lvn-analysis.py script that loads the .csv output, pivots it, computes per-benchmark improvements, and generates plots of improvement vs. baseline.

Times When the Tool Was Unhelpful

Turnt: As in the example, the guidance ChatGPT provided on turnt usage was vague and sometimes incorrect. Suggestions around how .out and .prof files should be generated were misleading. We relied instead on course-provided notes and example harnesses to verify and format benchmark results properly.

SSA semantics for LVN: At times, ChatGPT’s suggestions for value numbering across blocks ignored SSA guarantees or control flow, which would lead to incorrect substitutions. We had to manually correct this to ensure block-local substitution only.

Conclusion

Strengths:

Fast iteration while debugging broken DCE and helping design robust heuristics for what variables should be kept.

Useful for writing data analysis and plotting code for CSV performance results.

Helped brainstorm test cases and benchmark structure (e.g., arithmetic series, nested branches, redundant id chains).

Weaknesses:

Misleading and hallucinated advice around turnt testing pipeline.

Required careful double-checking of SSA-related suggestions and control-flow logic to avoid incorrect optimization.
