import os
import re

from envh.server.keys_view import BROWSE, PASSPHRASE, RENAME, CheckPassphrase, KeyList, KeyRow, Leave, Rename, incomplete_sequence, key_spans, render_keys, split_keys

ROWS = [KeyRow("ALPHA_KEY", ("alpha",), False), KeyRow("BETA_KEY", (), True), KeyRow("GAMMA_KEY", ("alpha", "gamma"), False)]
SIZE = os.terminal_size((80, 24))
NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def check_name(old: str, new: str) -> str | None:
    if not NAME.match(new):
        return f"{new!r} is not a valid secret name (UPPER_CASE)"
    if new in {row.name for row in ROWS}:
        return f"{new} is already taken"
    return None


def new_view(rows: list[KeyRow] = ROWS) -> KeyList:
    return KeyList(list(rows), check_name)


def press(view: KeyList, *keys: str) -> list[object]:
    return [view.handle(key) for key in keys]


def test_split_keys_names_arrows_f2_in_every_terminal_and_drops_other_function_keys() -> None:
    assert split_keys("\x1b[A\x1bOB\x1bOQ\x1b[12~\x1b[[Bab\n\x7f\x1b") == ["up", "down", "f2", "f2", "f2", "a", "b", "enter", "backspace", "escape"]
    assert split_keys("\x1b[1;5C\x1b[[Ax\x1bOP") == ["x"]
    assert split_keys("pässword") == list("pässword")
    assert key_spans("\x1b[Bn\n") == [("down", 3), ("n", 4), ("enter", 5)]


def test_a_sequence_cut_short_at_the_end_of_a_read_is_recognized() -> None:
    assert all(incomplete_sequence(text) for text in ("\x1b", "ab\x1b", "\x1b[", "\x1b[1;5", "\x1bO", "\x1b[["))
    assert not any(incomplete_sequence(text) for text in ("", "a", "\x1b[A", "\x1bOQ", "\x1b[12~", "\x1b[[B"))


def test_arrows_move_within_the_list() -> None:
    view = new_view()
    press(view, "up", "down", "down", "down", "down")
    assert view.selected == 2
    press(view, "home")
    assert view.selected == 0
    assert press(view, "escape") == [Leave()]


def test_f2_renames_with_the_passphrase_once_per_visit() -> None:
    view = new_view()
    press(view, "f2")
    assert view.mode == RENAME and view.draft == "ALPHA_KEY"
    press(view, "clear", *"my-alpha.key", "enter")
    assert view.mode == PASSPHRASE
    assert press(view, *"secret", "enter") == [None] * 6 + [CheckPassphrase("secret")]
    assert view.passphrase_accepted() == Rename("ALPHA_KEY", "MY_ALPHA_KEY")
    view.renamed([KeyRow("BETA_KEY", (), True), KeyRow("GAMMA_KEY", (), False), KeyRow("MY_ALPHA_KEY", ("alpha",), False)], "MY_ALPHA_KEY")
    assert view.selected == 2 and view.mode == BROWSE
    press(view, "up", "f2", "backspace")
    assert press(view, "enter") == [Rename("GAMMA_KEY", "GAMMA_KE")]


def test_a_refused_name_ends_the_edit_so_typing_ahead_cannot_reach_the_name_field() -> None:
    view = new_view()
    press(view, "f2", "clear", *"beta_key", "enter")
    assert view.mode == BROWSE and view.message == "BETA_KEY is already taken"
    assert press(view, *"hunter2", "enter") == [None] * 8
    assert view.mode == BROWSE and "HUNTER" not in view.draft
    press(view, "f2", "clear", "9", "enter")
    assert view.mode == BROWSE and "valid secret name" in view.message


def test_enter_and_letters_do_nothing_while_browsing() -> None:
    view = new_view()
    assert press(view, *"jkqr-passphrase", "enter", *"passphrase", "enter") == [None] * 27
    assert view.mode == BROWSE and view.selected == 0 and view.draft == ""


def test_the_passphrase_field_keeps_every_typed_character() -> None:
    view = new_view()
    press(view, "f2", "X", "enter")
    assert press(view, *"pass\tword 　x", "enter")[-1] == CheckPassphrase("pass\tword 　x")


def test_a_wrong_passphrase_renames_nothing_and_asks_again_next_time() -> None:
    view = new_view()
    press(view, "f2", "X", "enter", *"guess", "enter")
    view.passphrase_rejected()
    assert view.mode == BROWSE and "Wrong passphrase" in view.message
    press(view, "f2", "X", "enter")
    assert view.mode == PASSPHRASE


def test_frames_show_the_phrase_when_asking_and_never_the_typed_passphrase() -> None:
    view = new_view()
    frame = render_keys(view, "amber basil cedar", 2, SIZE)
    assert "ALPHA_KEY" in frame and "gamma" in frame and "per-run" in frame and "2 requests waiting" in frame
    press(view, "f2", "Y", "enter", *"hunter22")
    frame = render_keys(view, "amber basil cedar", 0, SIZE)
    assert "[amber basil cedar] vault passphrase" in frame and "ALPHA_KEYY" in frame and "hunter22" not in frame
    assert render_keys(KeyList([], check_name), "phrase", 0, SIZE).count("No keys yet") == 1


def test_a_short_screen_drops_list_rows_before_the_passphrase_prompt() -> None:
    view = new_view([KeyRow(f"KEY_{number:02}", (), False) for number in range(30)])
    press(view, "f2", "X", "enter")
    for lines in (4, 9, 10, 24):
        frame = render_keys(view, "amber basil cedar", 1, os.terminal_size((80, lines)))
        assert "[amber basil cedar] vault passphrase to rename" in frame and frame.count("\n") <= lines - 2


def test_a_long_list_scrolls_to_keep_the_selection_on_screen() -> None:
    view = new_view([KeyRow(f"KEY_{number:02}", (), False) for number in range(60)])
    press(view, "end")
    assert "KEY_59" in render_keys(view, "phrase", 0, SIZE) and "KEY_00" not in render_keys(view, "phrase", 0, SIZE)


def test_the_old_name_shown_while_renaming_fits_the_screen() -> None:
    long_name = "NEWS_BOT_PERSONAL_OPENROUTER_API_KEY"
    view = new_view([KeyRow(long_name, (), False)])
    press(view, "f2")
    for line in render_keys(view, "phrase", 0, SIZE).split("\n"):
        assert len(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)) <= 80
