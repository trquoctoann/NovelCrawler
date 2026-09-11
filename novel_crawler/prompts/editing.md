Edit the supplied Vietnamese draft against its Chinese source for fluent reading.
Preserve all source IDs and content. Correct actual omissions, mistranslation,
wrong numbers relative to the SAME source sentence, and named-entity mistakes.
Keep ordinary prose natural Vietnamese and historical Hán–Việt forms of address.
Never use chàng for the male protagonist in narration or dialogue. Third person uses hắn;
direct address uses the supported title (e.g. phu quân), not third-person hắn.
For example "Phu quân, chàng lại vay bao nhiêu?" becomes "Phu quân lại vay bao nhiêu?".
The ordinary noun phrase chàng trai may remain where it means a young man.
Do not change phu quân,
nương tử, ta/ngươi or other supported dialogue address forms into modern ones.

Review paragraph boundaries independently from the website's source layout.
Set join_previous=true when this item's opening words complete the previous
item's unfinished sentence; use false for a genuine new paragraph. The first
item is always false. Keep IDs unchanged even across a joined reading paragraph.
Use \n\n inside text to separate different speakers' utterances that share a
source item. Keep speech introductions with the speech. Do not leave names,
clauses, closing quotes or punctuation stranded on a new line. Single newlines
are treated as wrapping spaces by the renderer. Do not add or drop words solely
to change paragraph layout.

Do not act as a continuity editor of the original novel. The author's arithmetic,
fish-sale amounts, sibling names/relationships and timelines can be inconsistent.
Translate the supplied statements faithfully without repairing their logic. These
source-only inconsistencies must never stop editing or become reader-facing
notes. Ignore old draft warnings about such issues after checking that the draft
accurately conveys the supplied source. Prefer source_notes=[]; diagnostics, if
needed, stay internal. A concern about the author's text alone can be marked
SOURCE: in issues; only genuine unresolved translation defects use unprefixed
issues. Do not insert commentary or source analysis into chapter prose.

Preserve explicit source identities in the same sentence: when the website has
split an unfinished sentence across source IDs and join_previous=true joins it,
a name or amount may move within those fragments for natural Vietnamese word
order. Keep every ID and all content; never borrow a name from another complete
sentence to satisfy a missing identity. Otherwise keep identities in their source
paragraph. A name cannot be
replaced solely by hắn/lão, a title or a surname when that loses the supplied
identity. Preserve source pronouns as pronouns. Do not substitute a different
identity to repair an apparent author typo. Preserve
the name actually written even when the surrounding scene suggests another person.
Do not mechanically repeat names
inside ordinary idioms. Reassess glossary_readings against source context; full
names/families/places cannot be relabeled as ordinary words. Ordinary readings
can be rendered idiomatically rather than matching the commentary word for word.

If repair_findings is present, correct actual translation defects against the
source. Do not endlessly reproduce a resolved warning or a source-logic concern.
Keep Arabic numeric literals exact; render Chinese numbers in Vietnamese without
changing their value. 两 after an amount is the unit lạng; 两百 is two hundred.
No cross-paragraph arithmetic or narrative consistency checks are requested.
Return the complete chapter in the supplied JSON schema, with issues=[] when no
actual translation defect remains. Do not summarize or manufacture missing text.
