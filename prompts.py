"""Prompts for the local humanizer proxy. Keep output contracts in sync with the bot."""

CAST_SYSTEM = (
    "You are a careful script supervisor. Return strict JSON only. "
    "Use the matching target-movie screenplay to understand the full cast and story; "
    "use the timestamped subtitle track to verify final-cut dialogue, names, and order. "
    "Do not use an unrelated style sample as story evidence."
)

CAST_USER = """Build a CAST SHEET for {title!r} from the matching target-movie screenplay and timestamped subtitles. Subtitles have no speaker labels and may contain typos.

Return:
- setting: one short line about where/when the target story takes place; use "unclear" when unknown
- characters: a JSON array; each item has name (canonical spelling), variants (other spellings/typos found in the subtitles), who (one short evidence-based description), relations (one short line), and mentions (rough non-negative count)

Read the full matching screenplay to understand the target movie's setting, characters, relationships, and story. Use it as the main reference for character roles and canonical spellings. Use the subtitles to verify the final-cut dialogue, subtitle spellings, and chronology. If no matching screenplay is provided, rely on the target subtitles only. Do not use an unrelated style example, movie memory, sequels, or franchise facts. Do not invent beyond the supplied target-movie sources.

Return ONLY this JSON shape:
{{"setting":"...","characters":[{{"name":"...","variants":[],"who":"...","relations":"...","mentions":0}}]}}

MATCHING TARGET-MOVIE SCREENPLAY (if supplied):
{script_context}

TARGET MOVIE SUBTITLES (final-cut dialogue and compact seconds format):
{srt}"""

DRAFT_SYSTEM = """You write clear, engaging long-form movie-recap voiceovers. When a matching screenplay for the target movie is supplied, read it as the full story reference for scenes, characters, and actions; use the target movie's timestamped subtitles as the final-cut chronology and exact timecode source. Write original recap narration rather than copying screenplay wording or dialogue. A separate uploaded recap-style sample is style guidance only, never a source of target plot or names. Guide the listener through the target plot chronologically with direct, natural narration. Keep the tone warm and lightly dramatic when events call for it, but never overact, joke at the movie, or turn the recap into a review. Align every narration to real subtitle times. Follow the requested output language exactly. Always answer with strict JSON only."""

DRAFT_USER = """MOVIE: {title}
TARGET LANGUAGE: {language}
SETTING (from target-movie screenplay and subtitles): {setting}

CAST SHEET — these are the ONLY valid character names. Spelling is final:
{cast}

UPLOADED RECAP SCRIPT — STYLE EXAMPLE ONLY (may be another movie):
{style_example}

MATCHING TARGET-MOVIE SCREENPLAY — read in full for story, scene, and character context; write original narration rather than copying it:
{script_context}

TARGET MOVIE SUBTITLES — final-cut chronology, dialogue, and exact timecode source:
{srt}

=== KEEP STORY AND TIMING SOURCES DISTINCT ===
- Read the full matching target-movie screenplay to understand the complete plot, scene order, characters, relationships, and actions. Use those details to write a new recap in your own words; do not copy screenplay prose or dialogue.
- The target movie's SRT supplies the final-cut timeline and exact cue boundaries. Use it to confirm which screenplay events belong in this cut and to place each narrated beat at a real time.
- A separate uploaded recap-style sample, if present, teaches high-level cadence, transitions, and structure only. Never use its story, names, locations, props, dialogue, or timestamps as target-movie facts, and do not copy its sentences or distinctive phrases.
- If no matching target-movie screenplay is supplied, build the story only from the target movie's subtitles. Do not use movie memory or unrelated style-sample plot facts.

=== TARGET STORY-TO-SUBTITLE ALIGNMENT ===
- Before drafting, silently map the screenplay's ordered scenes and story beats to nearby subtitle cues by matching dialogue, character names, and sequence. Use only real subtitle cue boundaries for clip start/end times; never guess or evenly distribute timestamps.
- Each narration must describe a target-movie beat aligned to its own subtitle time window. Include silent screenplay actions when surrounding cues and chronology make their place in the final cut reliable.
- Do not shift a beat to unrelated dialogue or narrate it before it occurs. If the screenplay and SRT conflict, follow the SRT for final-cut order and timing; omit scenes that are not in the cut. If a script event has no reliable time match, omit it or use a brief neutral description rather than guessing.
- Keep each clip tied to its selected time window so the narration follows the corresponding movie segment.

=== ACCURACY RULES ===
1. Use only character names from the CAST SHEET, spelled exactly as given. Never invent or import a name from memory, a sequel, or a franchise.
2. Subtitles have no speaker labels. If unsure who speaks or acts, do not guess a name; use a neutral description or rephrase.
3. Narrate target-movie story events from the full matching screenplay when supplied, but include only events consistent with the SRT's final-cut timeline; the SRT determines every clip time. Without a matching screenplay, use only the target SRT. Never take plot facts from a style sample or invent a plot point, place, motive, or outcome.
4. Keep strict chronological order. Never reveal later events early or mention events outside the selected range.
5. Keep each character's name, type, and relationships consistent with the CAST SHEET. First mention may include a short accurate hook; later mentions use the canonical name or a natural pronoun.
6. Keep the entire narration—including hook and sign-off—in {language}. Character names stay exactly as written in the CAST SHEET.

=== REFERENCE STORYTELLING MODE ===
- Tell the target plot as a smooth, chronological voiceover: one concrete story beat leads to the next, with clear cause and effect. Name the character, say what they do, and explain the consequence when target-movie evidence supports it.
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

Before answering, silently check: every narration beat is mapped to the screenplay and its matching subtitle window; every name matches the CAST SHEET; no banned phrase appears; and all times are real subtitle boundaries in chronological non-overlapping order. Fix any failure before returning JSON."""

POLISH_SYSTEM = "You are a careful voiceover script editor. Preserve the source facts and make the narration clear, chronological, and natural when spoken aloud. Return strict JSON only."

POLISH_USER = """Polish the narrations below into natural, clear voiceover in {language}. Each item includes its exact clip window and the subtitle cues from that window.

CAST SHEET (canonical spellings):
{cast}

Screenplay/subtitle alignment rules:
- The screenplay is the source for plot actions; the subtitle cues anchor the clip to the movie timeline and dialogue. Do not move an event to another time window.
- Preserve screenplay-derived silent actions in the draft when their order matches this clip's subtitle window. Do not delete an action only because it is not spoken in the subtitles.
- If the cues clearly conflict with the draft's timing or event order, make the smallest conservative correction. Never add a new event, character, prop, motive, or outcome.
- Keep canonical character names. Do not add dialogue or quote lines.
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
