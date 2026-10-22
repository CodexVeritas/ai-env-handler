from envh.client.scrub import Scrubber

ENV = {"OPENAI_API_KEY": "sk-openai-abc123", "DATABASE_URL": "postgres://reader:sk-openai-abc123@db/x", "PIN": "1234"}


def scrub_all(scrubber: Scrubber, writes: list[bytes]) -> bytes:
    return b"".join(scrubber.feed(write) for write in writes) + scrubber.flush()


def test_a_value_is_replaced_with_its_variable_name() -> None:
    scrubber = Scrubber(ENV)
    assert scrub_all(scrubber, [b"key=sk-openai-abc123\n"]) == b"key=[envh:OPENAI_API_KEY]\n"
    assert scrubber.hidden == {"OPENAI_API_KEY"}


def test_a_value_split_across_writes_is_still_hidden() -> None:
    scrubber = Scrubber(ENV)
    data = b"before sk-openai-abc123 after"
    assert scrub_all(scrubber, [bytes([byte]) for byte in data]) == b"before [envh:OPENAI_API_KEY] after"


def test_a_longer_value_wins_over_one_it_contains() -> None:
    assert scrub_all(Scrubber(ENV), [b"url: postgres://reader:sk-openai-abc123@db/x"]) == b"url: [envh:DATABASE_URL]"


def test_short_values_are_left_alone() -> None:
    scrubber = Scrubber(ENV)
    assert scrub_all(scrubber, [b"pin 1234"]) == b"pin 1234"
    assert scrubber.hidden == set()


def test_only_an_ending_that_could_start_a_value_is_held_back() -> None:
    scrubber = Scrubber(ENV)
    assert scrubber.feed(b"Continue? [y/n] ") == b"Continue? [y/n] "
    assert scrubber.feed(b"ask sk-open") == b"ask "
    assert scrubber.flush() == b"sk-open"


def test_binary_output_passes_through_unchanged() -> None:
    data = bytes(range(256)) * 4
    assert scrub_all(Scrubber(ENV), [data[:300], data[300:]]) == data


def test_no_values_means_no_changes() -> None:
    assert Scrubber({}).feed(b"anything") == b"anything"
