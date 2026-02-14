# Create Voice Training Profiles

Use this prompt to generate domain-specific profiles for Samsara's voice recognition.

---

## Prompt Template

Copy everything below the line and paste it into your AI chat:

---

I need help creating a voice training profile for Samsara, a voice dictation app. Generate a valid JSON profile.

### Profile Structure

```json
{
  "profile_name": "Profile Name",
  "description": "What this profile is for",
  "author": "Your Name",
  "version": "1.0",
  "created": "YYYY-MM-DD",
  "vocabulary": [
    "custom term 1",
    "custom term 2"
  ],
  "corrections": {
    "misheard phrase": "correct phrase",
    "another mistake": "what it should be"
  },
  "initial_prompt": "Optional context for the AI model"
}
```

### Field Descriptions

| Field | Purpose |
|-------|---------|
| `vocabulary` | Words Whisper often misses - names, technical terms, brands |
| `corrections` | Auto-replace misheard words (key = wrong, value = right) |
| `initial_prompt` | Hint to Whisper about context (e.g., "Medical transcription") |

### Example Profiles

**Programming Profile:**
```json
{
  "profile_name": "Programming",
  "description": "Software development terminology",
  "vocabulary": [
    "TypeScript", "JavaScript", "Python", "async", "await",
    "npm", "git", "GitHub", "localhost", "API", "JSON",
    "boolean", "parseInt", "querySelector", "useState"
  ],
  "corrections": {
    "function": "function",
    "jason": "JSON",
    "get hub": "GitHub",
    "java script": "JavaScript"
  },
  "initial_prompt": "Software development and programming discussion"
}
```

**Medical Profile:**
```json
{
  "profile_name": "Medical",
  "description": "Healthcare and medical terminology",
  "vocabulary": [
    "hypertension", "tachycardia", "bradycardia",
    "acetaminophen", "ibuprofen", "metformin",
    "CBC", "MRI", "CT scan", "EKG"
  ],
  "corrections": {
    "high per tension": "hypertension",
    "meta form in": "metformin"
  },
  "initial_prompt": "Medical transcription and healthcare documentation"
}
```

### My Request

[DESCRIBE YOUR DOMAIN/USE CASE HERE]

Examples:
- "Create a profile for legal transcription with court terminology"
- "I'm a game streamer, I need vocabulary for game names and gaming slang"
- "Profile for academic writing in philosophy"

---

## How to Use the Output

1. Copy the JSON the AI generates
2. Save as `YourProfile.json` in `profiles/dictionaries/`
3. Open Samsara Settings → Voice Training
4. Click "Manage Profiles" → Import
5. Select your profile to activate it

## Tips

- Add words Whisper consistently gets wrong
- Include both formal and casual versions of terms
- The `initial_prompt` helps with ambiguous homophones
- Test and refine - add corrections as you discover them
