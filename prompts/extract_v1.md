You are tagging a piece of fiction for a personal search/browse index. The
reader wants to find this story again later by what happens in it and what
kind of story it *feels* like — not by plot summary, and not by broad genre
labels alone.

Aim for "mid-level" tags: more specific than a genre, more general than a
plot event. Think in terms of recurring situations, character dynamics,
tropes, and tone — the kind of descriptive, reusable label you'd find on a
trope wiki. A tag should be useful across many different stories, not just
this one.

- Too broad (avoid): "romance", "mystery", "fiction", "drama", "adventure"
- Too narrow (avoid): "elizabeth refuses mr. collins", "holmes solves the
  case in chapter three", anything that only makes sense for this one story
- Good mid-level tags: "enemies to lovers", "arranged marriage", "class
  differences", "wrongful accusation", "amateur sleuth", "battle of wits",
  "unreliable narrator", "fish out of water", "slow burn", "found family",
  "locked room mystery", "epistolary format", "coming of age"

Rules:
- Produce between 3 and 10 tags.
- Each tag is a short lowercase phrase (1-4 words), no punctuation besides
  hyphens or spaces.
- No duplicate or near-duplicate tags in the same list.
- Do not include the title, author name, or character names as tags.
- Do not include a tag unless it's actually present in this story — don't
  pad the list to hit a target count.
- Do not explain your reasoning. Output nothing but the JSON object below.

Output strictly as JSON, no markdown fences, matching this shape:
{"tags": ["tag one", "tag two", "..."]}

Title: {title}
Author: {author}

Story:
{body_text}
