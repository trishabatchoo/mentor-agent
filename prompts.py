from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

_MODE_FILES = {
    "pre_session": "pre_session.md",
    "post_session": "post_session.md",
    "new_student": "new_student.md",
}


def load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def build_instructions(mode: str) -> str:
    if mode not in _MODE_FILES:
        raise ValueError(f"Unknown mode: {mode}")

    shared_instructions = load_prompt("shared.md")
    mode_instructions = load_prompt(_MODE_FILES[mode])

    return (
        f"{shared_instructions}"
        f"\n\n---\n\n"
        f"{mode_instructions}"
    )
