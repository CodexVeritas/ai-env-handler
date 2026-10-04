import os

from envh.server.keys_view import BROWSE, PASSPHRASE, RENAME, CheckPassphrase, KeyList, KeyRow, Leave, Rename, render_keys, split_keys

ROWS = [KeyRow("ALPHA_KEY", ("alpha",), False), KeyRow("BETA_KEY", (), True), KeyRow("GAMMA_KEY", ("alpha", "gamma"), False)]
SIZE = os.terminal_size((80, 24))


def press(view: KeyList, *keys: str) -> list[object]:
    return [view.handle(key) for key in keys]


def test_split_keys_names_arrows_f2_in_every_terminal_and_drops_other_function_keys() -> None:
    assert split_keys("\x1b[A\x1bOB\x1bOQ\x1b[12~\x1b[[Bab\n\x7f\x1b") == ["up", "down", "f2", "f2", "f2", "a", "b", "enter", "backspace", "escape"]
    assert split_keys("\x1b[1;5C\x1b[[Ax\x1bOP") == ["x"]
    assert split_keys("pässword") == list("pässword")


def test_arrows_move_within_the_list() -> None:
    view = KeyList(list(ROWS))
    press(view, "up", "down", "down", "down", "down")
    assert view.selected == 2
    press(view, "home")
    assert view.selected == 0
    assert press(view, "escape") == [Leave()]


def test_f2_renames_with_the_passphrase_once_per_visit() -> None:
    view = KeyList(list(ROWS))
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


def test_invalid_or_taken_names_are_refused_before_the_passphrase() -> None:
    view = KeyList(list(ROWS))
    press(view, "f2", "clear", "9", "enter")
    assert view.mode == RENAME and "start with a letter" in view.message
    press(view, "clear", *"beta_key", "enter")
    assert view.mode == RENAME and view.message == "BETA_KEY is already taken."
    press(view, "escape")
    assert view.mode == BROWSE


def test_a_wrong_passphrase_renames_nothing_and_asks_again_next_time() -> None:
    view = KeyList(list(ROWS))
    press(view, "f2", "X", "enter", *"guess", "enter")
    view.passphrase_rejected()
    assert view.mode == BROWSE and "Wrong passphrase" in view.message
    press(view, "f2", "X", "enter")
    assert view.mode == PASSPHRASE


def test_frames_show_the_phrase_when_asking_and_never_the_typed_passphrase() -> None:
    view = KeyList(list(ROWS))
    frame = render_keys(view, "amber basil cedar", 2, SIZE)
    assert "ALPHA_KEY" in frame and "gamma" in frame and "per-run" in frame and "2 requests waiting" in frame
    press(view, "f2", "Y", "enter", *"hunter22")
    frame = render_keys(view, "amber basil cedar", 0, SIZE)
    assert "[amber basil cedar] vault passphrase" in frame and "ALPHA_KEYY" in frame and "hunter22" not in frame
    assert render_keys(KeyList([]), "phrase", 0, SIZE).count("No keys yet") == 1


def test_a_long_list_scrolls_to_keep_the_selection_on_screen() -> None:
    view = KeyList([KeyRow(f"KEY_{number:02}", (), False) for number in range(60)])
    press(view, "end")
    assert "KEY_59" in render_keys(view, "phrase", 0, SIZE) and "KEY_00" not in render_keys(view, "phrase", 0, SIZE)


def test_letters_do_nothing_while_browsing() -> None:
    view = KeyList(list(ROWS))
    assert press(view, *"jkqr-passphrase") == [None] * 15
    assert view.mode == BROWSE and view.selected == 0
