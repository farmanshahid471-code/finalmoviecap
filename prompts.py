"""Prompts for the local humanizer proxy. Keep output contracts in sync with the bot."""

CAST_SYSTEM = (
    "You are a careful script supervisor. Return strict JSON only. "
    "Use only evidence in the supplied dialogue."
)

CAST_USER = """Below are the subtitles of the movie {title!r}. Subtitles have no speaker labels and may contain typos.

Build a CAST SHEET of named characters actually mentioned in the dialogue. Return:
- setting: one short line about where/when the story takes place, based only on dialogue; use "unclear" when unknown
- characters: a JSON array; each item has name (canonical spelling), variants (other spellings/typos found), who (one short evidence-based description), relations (one short line), and mentions (rough non-negative count)

Use the subtitles as the source of truth for the movie's final-cut dialogue and character spellings. The optional script is supplementary context: use it to resolve a relationship or spelling only when consistent with the subtitles. Do not include a character, event, location, or detail merely because it appears in the script; it must also be relevant to the subtitle track. Do not use memory of the movie, its sequels, or the franchise. Do not invent anything. Use "unclear" when evidence is insufficient.

Return ONLY this JSON shape:
{{"setting":"...","characters":[{{"name":"...","variants":[],"who":"...","relations":"...","mentions":0}}]}}

SUBTITLES (compact seconds format; authoritative):
{srt}

OPTIONAL ENGLISH SCRIPT CONTEXT (may be a different draft; context only):
{script_context}"""

DRAFT_SYSTEM = """You write clear, engaging long-form movie-recap voiceovers. Guide the listener through the plot in chronological order with direct, natural narration and smooth transitions between storylines. Keep the tone warm and lightly dramatic when the events call for it, but never overact, joke at the movie, or turn the recap into a review. Be specific about who does what and why, using only evidence in the supplied subtitles and cast sheet. Follow the requested output language exactly. Always answer with strict JSON only."""

DRAFT_USER = """MOVIE: {title}
TARGET LANGUAGE: {language}
SETTING (from dialogue only): {setting}

CAST SHEET — these are the ONLY valid character names. Spelling is final:
{cast}

OPTIONAL ENGLISH SCREENPLAY (story context only; it may differ from the final cut):
{script_context}

=== SUBTITLE / SCRIPT ALIGNMENT ===
- The timestamped subtitles are authoritative for the final cut: use them to select ranges, order events, and determine what is happening inside each clip window.
- Use the screenplay only to clarify a name or motivation when that context agrees with the subtitles. Never import a screenplay-only scene, action, prop, or outcome into a clip.
- Do not move an event to another timestamp because it appears elsewhere in the screenplay. If script and subtitles conflict, follow the subtitle window and keep the narration conservative.
- Every narration must match the dialogue and story beat in its own selected time window. Do not use later script context to describe an event before it occurs.

=== ACCURACY RULES ===
1. Use only character names from the CAST SHEET, spelled exactly as given. Never invent or import a name from memory, a sequel, or a franchise.
2. Subtitles have no speaker labels. If unsure who speaks or acts, do not guess a name; use a neutral description or rephrase.
3. Narrate only events supported by the subtitle text inside the chosen time range. Never invent a plot point, place, motive, or outcome.
4. Keep strict chronological order. Never reveal later events early or mention events outside the selected range.
5. Keep each character's name, type, and relationships consistent with the CAST SHEET. First mention may include a short accurate hook; later mentions use the canonical name or a natural pronoun.
6. Keep the entire narration—including hook and sign-off—in {language}. Character names stay exactly as written in the CAST SHEET.

=== REFERENCE STORYTELLING MODE ===
- Tell the plot as a smooth, chronological voiceover: one concrete event leads to the next, with clear cause and effect. Name the character, say what they do, and explain the consequence when the subtitles support it.
- Use plain, accessible language and a steady narrator voice. Keep most sentences short or medium length; vary the rhythm with an occasional longer sentence that connects related events. Avoid run-ons, choppy fragments, and vague summary statements.
- When the story cuts between characters or locations, use a brief, natural bridge—equivalents of “Meanwhile,” “A little later,” “Not long after,” “The next morning,” “Back at the house,” or “After that.” Vary the wording and use transitions only when they clarify the timeline; do not repeat a formula mechanically.
- Let suspense and emotion come from the events. Explain what a character wants or fears only when supported by the source. Keep humor understated and tied to what is happening; no invented jokes, commentary, or review-like opinions.
- The first narration must {opening_rule}, then move straight into the setup. Do not greet the audience or spend time on a generic introduction.
- Use third person and present tense for action. Report dialogue indirectly rather than quoting it.
- Ignore transcript debris such as [music], [applause], speaker arrows, HTML entities, or transcription glitches. Never narrate these artifacts.
- Spend more words on turning points and less on filler. Skip opening credits and long stretches with no dialogue. If the source includes an epilogue or post-credit event, place it after the main resolution.
- Avoid these clichés/phrases: delve, tapestry, testament, journey (unless literal), “little did they know,” “in a world where,” buckle up, dive in, heartwarming, emotional rollercoaster, unforgettable, ultimately, “it’s worth noting,” “not only ... but also,” “a stark reminder,” “as the story unfolds,” in conclusion, serves as, sets the stage, stakes are high, pivotal, begins to realize.
- No bullet points, emojis, stage directions, sound effects, or hashtags. Never mention subtitles, timestamps, the camera, the movie/film, or that you are an AI. Never describe visuals, lighting, or editing.

=== TASK ===
Choose exactly {n_clips} non-overlapping ranges in seconds from the subtitle timestamps below. Cover the plot arc in chronological order, from setup through the ending. Do not use ranges starting at zero. Each narration must describe only its own time window and fit that window when spoken. Aim for roughly 2.5 spoken words per second, while following these clip-length rules from the bot:
{length_rules}
Reserve enough room in the final clip for the localized sign-off. {outro_rule}

Return ONLY valid JSON, no markdown or commentary, in the bot's exact schema:
{{"clips":[{{"start":120,"end":135,"narration":"..."}}]}}

SUBTITLES (seconds, compact format [start-end] text):
{srt}

Before answering, silently check: every name matches the CAST SHEET; no banned phrase appears; all times are real subtitle times in chronological non-overlapping order; and the wording sounds spoken rather than essay-like. Fix any failure before returning JSON."""

POLISH_SYSTEM = "You are a careful voiceover script editor. Preserve the source facts and make the narration clear, chronological, and natural when spoken aloud. Return strict JSON only."

POLISH_USER = """Polish the narrations below into natural, clear voiceover in {language}. Each item includes its exact clip window and the subtitle cues from that window.

CAST SHEET (canonical spellings):
{cast}

Grounding and style rules:
- Use the subtitle cues for the item's own time window as the authority for what can be narrated there. Do not move an event to another time window.
- If a detail from the screenplay or draft is not supported by this clip's cues, remove it or make the narration neutral. Never add an event, character, prop, motive, or outcome.
- Keep every supported event and canonical character name. Do not add dialogue or quote lines.
- Keep each narration's word/character count within 15 percent of its draft so it still fits the clip. Preserve the first opening and final sign-off exactly.
- Use direct, chronological voiceover with clear cause and effect, plain spoken language, and brief transitions only when useful. Avoid run-ons, vague filler, repetition, and ornate or overdramatic wording.
- Keep present tense and third person. No emoji, lists, sound cues, or transcript artifacts.
{fix_notes}

Return ONLY this JSON shape, with exactly {count} strings in the same order:
{{"narrations":["...","..."]}}

CLIPS, DRAFT NARRATIONS, AND TIME-ALIGNED SUBTITLE EVIDENCE:
{items}"""

REPAIR_SYSTEM = "You are a careful movie-recap fact checker. Make the smallest correction needed while preserving the clear, chronological voiceover style. Return strict JSON only."

REPAIR_USER = """Repair this single narration in {language}. The narration contains an unapproved name or spelling.

CAST SHEET — only these canonical names may appear:
{cast}

SUBTITLE EVIDENCE FOR THIS CLIP:
{subtitles}

CURRENT NARRATION:
{narration}

UNAPPROVED NAME(S): {bad_names}

Rewrite only this clip. Keep the same events and approximate length. Replace any unapproved or misspelled character name with the correct CAST SHEET spelling, or a neutral role/description if identity is uncertain. Do not add events, people, places, motives, outcomes, or unsupported transitions. Keep the narration in {language}, present tense, and third person, using plain, clear voiceover language. Preserve the required opening/sign-off if this is the first/last clip.

Return ONLY: {{"narration":"..."}}"""
