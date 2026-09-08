Perform a final Vietnamese literary edit, comparing every draft paragraph against
its Chinese source. Correct omissions, mistranslation, inconsistent pronouns,
glossary deviations, awkward syntax, punctuation and unnatural line breaks.
Preserve paragraph IDs, facts, tone and the author's intended ambiguity.
Do not embellish or rewrite the plot. Return the complete corrected chapter in
the same JSON schema, including title and updated continuity note. Report
unresolved issues. Use the fixed glossary and preceding edited context.

Inspect any issues reported by the draft. Resolve straightforward typographical
name errors only when the fixed glossary and immediate context uniquely identify
the person; record that decision in the continuity note. Distinct place names are
not automatically contradictions: preserve them unless context proves an error.
Do not invent missing text. The final issues array contains only unresolved
material problems, not uncertainties that you have already resolved by comparison.
Resolve harmless web-formatting artifacts during this final edit: remove trailing
isolated ASCII junk such as " ??" after a complete sentence when clearly outside
dialogue, and describe the cleanup in continuity rather than issues. A sentence
continuing in the next source paragraph is not missing content when both parts
are supplied. Preserve the paragraph IDs while editing it across the boundary;
report an issue only if actual content or meaning cannot be recovered. Never
guess a missing sentence or silently dismiss a substantive uncertainty.
