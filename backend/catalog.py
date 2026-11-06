"""Timbre v3.0 voices, languages and audio tags (from docs.gnani.ai)."""

VOICES = {
    # name: (gender, persona, description)
    "Shivani": ("female", "warm, empathetic", "Warm, patient, empathy-first voice"),
    "Lakshmi": ("female", "expressive, energetic", "Expressive, warm storytelling voice"),
    "Kalpana": ("female", "warm, empathetic", "Empathetic, composed voice"),
    "Chetana": ("female", "warm, empathetic", "Gentle, empathetic voice"),
    "Vasudha": ("female", "expressive, energetic", "Expressive, composed narration voice"),
    "Riyaa": ("female", "confident, authoritative", "Confident, lively voice"),
    "Tarika": ("female", "warm, empathetic", "Warm, reassuring voice"),
    "Avinash": ("male", "professional", "Professional, firm voice with deliberate pacing"),
    "Shubham": ("male", "expressive, energetic", "Energetic, confident voice"),
    "Prakash": ("male", "professional", "Steady, deliberate voice"),
    "Devansh": ("male", "conversational", "Energetic, young conversational voice"),
    "Yashraj": ("male", "warm, empathetic", "Warm narrative voice"),
    "Harshad": ("male", "professional", "Professional, firm voice"),
}

LANGUAGES = {
    "hi-en": "Hinglish (Hindi-English code-mixed)",
    "hi-IN": "Hindi",
    "en-IN": "English (Indian)",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}

# Timbre v3.0 inline audio tags. A tag applies from its position until the next tag.
TAGS = [
    "<laugh>", "<giggle>", "<sigh>", "<exhales>", "<sniffles>", "<yawns>", "<coughs>",
    "<clears throat>", "<whispers>", "<shouting>", "<excited>", "<slow>", "<pauses>",
]

PACE = {"slow": 0.9, "normal": 1.0, "fast": 1.1}


def voice_catalog_text() -> str:
    return "\n".join(f"- {n} ({g}; {p}): {d}" for n, (g, p, d) in VOICES.items())
