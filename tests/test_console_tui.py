from envh.server.console.tui import (
    Display,
    KeyReader,
    Paste,
    Selection,
    Span,
    TextField,
    box,
    clip,
    fit,
    line_width,
    plain,
    render,
    text_width,
    wrap,
    wrap_line,
)


def test_keys_are_named_in_every_terminal_and_unknown_function_keys_are_dropped() -> None:
    reader = KeyReader()
    assert reader.feed("\x1b[A\x1bOB\x1bOQ\x1b[12~\x1b[[B\x1b[3~\x13ab\r\x7f\t") == ["up", "down", "f2", "f2", "f2", "delete", "ctrl_s", "a", "b", "enter", "backspace", "tab"]
    assert reader.feed("\x1b[1;5C\x1b[[Ax\x1bOP\x1a") == ["x"]
    assert reader.feed("pässword　x") == list("pässword　x")


def test_a_sequence_split_across_reads_waits_for_the_rest() -> None:
    reader = KeyReader()
    assert reader.feed("a\x1b") == ["a"] and reader.waiting
    assert reader.feed("[B") == ["down"] and not reader.waiting
    assert reader.feed("\x1b[1") == [] and reader.waiting
    assert reader.feed("2~") == ["f2"]


def test_a_lone_esc_is_the_esc_key_once_nothing_more_arrives() -> None:
    reader = KeyReader()
    assert reader.feed("\x1b") == []
    assert reader.flush() == ["escape"]
    assert reader.feed("\x1bOrchid\n") == ["escape", *"Orchid", "enter"]


def test_a_paste_arrives_whole_even_across_reads() -> None:
    reader = KeyReader()
    assert reader.feed("x\x1b[200~sk-proj-") == ["x"]
    assert reader.feed("abc\nline two\x1b[20") == []
    assert reader.feed("1~y") == [Paste("sk-proj-abc\nline two"), "y"]


def test_a_text_field_edits_at_its_cursor() -> None:
    field = TextField("hello world")
    for key in ("left", "left", "left", "left", "left", "backspace", "_"):
        field.handle(key)
    assert field.text == "hello_world"
    field.handle("ctrl_w")
    assert field.text == "world"
    field.handle("end")
    field.handle(Paste("!\n"))
    assert field.text == "world!"
    field.handle("ctrl_u")
    assert field.text == "" and field.cursor == 0


def test_a_field_takes_only_what_accept_lets_through() -> None:
    field = TextField(accept=lambda char: char.upper() if char.isalpha() else "_" if char == "-" else "")
    for key in "my-key.1":
        field.handle(key)
    assert field.text == "MY_KEY"


def test_a_hidden_field_shows_neither_its_text_nor_its_length() -> None:
    field = TextField(hidden=True)
    empty = plain(field.render(40))
    field.insert("a")
    one = plain(field.render(40))
    field.insert("much-longer-passphrase")
    assert plain(field.render(40)) == one and "a" not in one.replace("hidden", "") and one != empty


def test_widths_count_wide_and_invisible_characters() -> None:
    assert text_width("abc") == 3 and text_width("日本") == 4 and text_width("é") == 1
    assert clip("abcdef", 4) == "abc…" and clip("abc", 4) == "abc" and text_width(clip("日本語の文", 5)) <= 5
    line = [Span("abc", "1"), Span("defgh")]
    assert plain(fit(line, 5)) == "abcd…" and line_width(fit(line, 5)) == 5


def test_rendering_keeps_styles_cuts_to_the_width_and_neutralizes_control_characters() -> None:
    line = [Span("ok", "32"), Span(" text\x1b[2J‮")]
    assert render(line, 80, color=True) == "\x1b[32mok\x1b[0m text�[2J�"
    assert render(line, 80, color=False) == "ok text�[2J�"
    assert render([Span("x" * 100)], 10, color=False) == "x" * 9 + "…"


def test_wrapping_breaks_at_spaces_and_inside_long_words() -> None:
    assert wrap("one two three", 7) == ["one two", "three"]
    assert wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert [plain(line) for line in wrap_line([Span("ab", "1"), Span("cdefg")], 3)] == ["abc", "def", "g"]


def test_a_box_is_as_wide_as_asked_with_its_title_in_the_top_edge() -> None:
    lines = box([[Span("hello")], [Span("x" * 100)]], 30, [Span("Title")], [Span("0:42")])
    assert all(line_width(line) == 30 for line in lines)
    assert plain(lines[0]).startswith("╭─ Title ") and plain(lines[0]).endswith(" 0:42 ─╮")
    assert plain(lines[2]).endswith("… │")


def test_a_selection_stays_on_screen_as_the_list_scrolls() -> None:
    selection = Selection()
    for _ in range(25):
        selection.move("down", 30)
    assert selection.index == 25 and 25 in selection.visible(30, 10)
    selection.move("home", 30)
    assert selection.visible(30, 10) == range(0, 10)
    assert not selection.move("x", 30)


def test_the_display_rewrites_only_rows_that_changed() -> None:
    written: list[str] = []
    display = Display(written.append)
    display.draw(["one", "two"], (20, 3))
    assert written[-1].startswith("\x1b[2J") and "one" in written[-1] and "two" in written[-1]
    display.draw(["one", "TWO"], (20, 3))
    assert "one" not in written[-1] and "\x1b[2;1H\x1b[2KTWO" in written[-1]
    count = len(written)
    display.draw(["one", "TWO"], (20, 3))
    assert len(written) == count
