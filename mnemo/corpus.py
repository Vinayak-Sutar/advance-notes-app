"""Synthetic note corpus with known ground truth.

Used by two things:

* `mnemo.calibrate` -- measures the similarity distribution of same-topic vs
  different-topic note pairs, so thresholds are derived from data instead of
  guessed.
* `tests/test_sprawl.py` -- the anti-sprawl stress test, which needs a corpus
  where the *correct* number of topics is known in advance.

Vocabulary is deliberately inconsistent within a topic ("gaming" / "video
games" / "vidya"), because vocabulary drift is exactly what makes an LLM invent
a fifth folder for a topic it already has.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

TOPICS: dict[str, dict] = {
    "Gaming/Elden Ring": {
        "terms": ["elden ring", "malenia", "radahn", "boss fight", "bleed build",
                  "dexterity scaling", "mimic tear", "summon sign", "runes",
                  "great rune", "spirit ash", "colosseum"],
        "aliases": ["Elden Ring", "EldenRing", "elden ring notes", "Souls Game"],
    },
    "Gaming/Hardware": {
        "terms": ["rtx 4070", "gpu", "framerate", "ultrawide monitor", "refresh rate",
                  "thermal paste", "benchmark", "vram", "undervolt", "psu wattage"],
        "aliases": ["PC Hardware", "Gaming Hardware", "hardware", "Rig Parts"],
    },
    "Technology/AI": {
        "terms": ["rag pipeline", "embedding model", "llm inference", "transformer",
                  "fine tuning", "prompt engineering", "token budget", "vector store",
                  "quantization", "context window", "reranker"],
        "aliases": ["AI", "Artificial Intelligence", "ai notes", "LLM Stuff", "ML"],
    },
    "Technology/Web Dev": {
        "terms": ["react component", "css grid", "typescript generics", "bundler",
                  "server rendering", "hydration", "route loader", "flexbox",
                  "web vitals", "service worker"],
        "aliases": ["Web Dev", "Web Development", "webdev", "Frontend"],
    },
    "Technology/App Dev": {
        "terms": ["swift ui", "kotlin coroutine", "android manifest", "ios simulator",
                  "gradle build", "xcode signing", "apk bundle", "push notification",
                  "app store review"],
        "aliases": ["App Dev", "Mobile Dev", "App Development", "mobile"],
    },
    "Music/Albums": {
        "terms": ["vinyl pressing", "album mixing", "mastering engineer", "track listing",
                  "drum tone", "guitar riff", "b side", "remaster", "liner notes"],
        "aliases": ["Albums", "Music Albums", "records", "Album Notes"],
    },
    "Music/Guitar": {
        "terms": ["pentatonic scale", "fretboard", "barre chord", "capo position",
                  "string gauge", "tube amp", "overdrive pedal", "alternate tuning",
                  "fingerpicking"],
        "aliases": ["Guitar", "Guitar Practice", "guitar stuff", "Playing Guitar"],
    },
    "Cooking/Sourdough": {
        "terms": ["sourdough starter", "hydration ratio", "levain", "open crumb",
                  "banneton", "scoring blade", "bulk ferment", "dutch oven",
                  "autolyse", "proofing time"],
        "aliases": ["Sourdough", "Bread Baking", "sourdough bread", "Baking"],
    },
    "Personal/Finance": {
        "terms": ["invoice", "tax deduction", "receipt scan", "monthly budget",
                  "insurance premium", "mutual fund", "emergency fund", "credit score"],
        "aliases": ["Finance", "Personal Finance", "money", "Financial Notes"],
    },
    "Personal/Health": {
        "terms": ["sleep schedule", "protein intake", "workout split", "physio exercise",
                  "hamstring stretch", "zone two cardio", "resting heart rate"],
        "aliases": ["Health", "Fitness", "health notes", "Wellbeing"],
    },
}

TEMPLATES = [
    "notes on {a} and {b}",
    "{a}: worth remembering that {b} matters more than expected",
    "compared {a} with {b} today, keeping {c} in mind",
    "quick reminder about {a} — follow up on {b}",
    "read a good writeup covering {a}, {b} and {c}",
    "{a} keeps coming up. related to {b}",
    "tried {a} this week. next step is {b}",
    "{a} vs {b} — still undecided, {c} is the tiebreaker",
    "saved this about {a}",
    "{a} and {b} turned out to be the same problem",
]


@dataclass
class Sample:
    text: str
    topic: str          # ground-truth topic path


def generate(n: int = 500, seed: int = 7) -> list[Sample]:
    """Even spread across every topic, in a shuffled order."""
    rng = random.Random(seed)
    paths = list(TOPICS)
    samples: list[Sample] = []
    for i in range(n):
        path = paths[i % len(paths)]
        terms = TOPICS[path]["terms"]
        picks = rng.sample(terms, min(3, len(terms)))
        text = rng.choice(TEMPLATES).format(a=picks[0], b=picks[1], c=picks[2])
        samples.append(Sample(text=text, topic=path))
    rng.shuffle(samples)
    return samples


def alias_stream(n: int = 60, seed: int = 11) -> list[tuple[str, str]]:
    """(proposed_name, ground_truth_topic) pairs with drifting vocabulary --
    what the gate must collapse rather than turn into new folders."""
    rng = random.Random(seed)
    out = []
    paths = list(TOPICS)
    for i in range(n):
        path = paths[i % len(paths)]
        out.append((rng.choice(TOPICS[path]["aliases"]), path))
    rng.shuffle(out)
    return out


def expected_topic_count() -> int:
    return len(TOPICS)
