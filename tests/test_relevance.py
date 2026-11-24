import pytest

from helpers import taxonomy


@pytest.mark.parametrize(
    "title",
    [
        "OpenAI launches GPT-5 for everyone",
        "A new LLM beats the benchmark",
        "A.I. fueled Nvidia CEO Jensen Huang's elite status",
        "Anthropic reportedly plans to move forward with IPO",
        "AI agents are testing the limits of zero-trust security",
        "Fine-tuning embeddings for retrieval",  # two weak terms are enough
        "Inside the data center boom powering GPUs",  # two weak terms are enough
    ],
)
def test_ai_titles_pass_the_loose_filter(title: str) -> None:
    assert taxonomy().is_ai(title, "", "loose")


def test_one_weak_term_alone_is_not_enough() -> None:
    assert not taxonomy().is_ai("Show HN: a tiny prompt library", "", "loose")


@pytest.mark.parametrize(
    "title",
    [
        "Senate advances sweeping energy legislation",
        "Fed raises interest rates again",
        "Local bakery wins national award",
        "Hawaii braces for a new storm",  # contains 'ai' inside a word
        "How to cook the perfect risotto",
    ],
)
def test_non_ai_titles_are_rejected(title: str) -> None:
    assert not taxonomy().is_ai(title, "", "loose")
    assert not taxonomy().is_ai(title, "", "strict")


def test_a_passing_mention_in_the_summary_is_not_enough() -> None:
    # One strong term in the summary only: a business story that happens to say "AI".
    summary = "The airline says costs are rising as it adopts AI in some back-office roles."
    assert not taxonomy().is_ai("Qantas to outsource 850 local jobs", summary, "loose")


def test_summary_can_rescue_a_vague_title_with_real_evidence() -> None:
    summary = "The model is trained with machine learning on a cluster of GPUs."
    assert taxonomy().is_ai("Introducing Project Atlas", summary, "loose")  # strong + weak
    assert not taxonomy().is_ai("Introducing Project Atlas", summary, "strict")  # title only


def test_strict_mode_reads_only_the_title() -> None:
    assert not taxonomy().is_ai("Quarterly results", "OpenAI Anthropic ChatGPT", "strict")
    assert taxonomy().is_ai("OpenAI quarterly results", "", "strict")


def test_off_mode_keeps_everything() -> None:
    assert taxonomy().is_ai("Local bakery wins national award", "", "off")


def test_uppercase_ai_only_is_not_matched_inside_words_or_lowercase() -> None:
    t = taxonomy()
    assert t.is_ai("The AI-powered toaster", "", "loose")
    assert not t.is_ai("Mai Tai recipe", "", "loose")
    assert not t.is_ai("said the maid", "", "loose")


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("Anthropic raises $2 billion in Series F funding", "business"),
        ("Researchers jailbreak popular chatbot with prompt injection", "safety"),
        ("EU AI Act: new regulation and a copyright lawsuit", "policy"),
        ("A new MCP server for coding agents", "agents"),
        ("Open-source model released with open weights on Hugging Face", "open-source"),
        ("New GPU cluster doubles inference throughput", "infra"),
        ("Humanoid robot learns embodied tasks", "robotics"),
    ],
)
def test_topics_are_tagged(text: str, topic: str) -> None:
    assert topic in taxonomy().topics_for(text)


def test_topics_are_capped_and_empty_for_unrelated_text() -> None:
    assert len(taxonomy().topics_for("agents llm gpu robot safety policy funding benchmark")) <= 4
    assert taxonomy().topics_for("A recipe for sourdough bread") == []
