import applog


def test_truncate_leaves_short_line_untouched():
    line = "коротко"
    assert applog._truncate(line) == line


def test_truncate_cuts_long_line_and_notes_original_length():
    line = "а" * 1000
    result = applog._truncate(line)
    assert len(result) < len(line)
    assert result.startswith("а" * applog.MAX_LOGGED_LINE)
    assert "1000" in result


def test_truncate_boundary_exact_length_untouched():
    line = "б" * applog.MAX_LOGGED_LINE
    assert applog._truncate(line) == line


def test_log_writes_truncated_line_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "LOG_FILE", tmp_path / "test.log")
    monkeypatch.setattr(applog, "LOG_DIR", tmp_path)
    applog._logger.handlers.clear()

    long_letter = "Здравствуйте! " + "х" * 2000
    applog.log(long_letter)

    for handler in applog._logger.handlers:
        handler.flush()
    content = (tmp_path / "test.log").read_text(encoding="utf-8")
    assert "обрезано для файла" in content
    assert long_letter not in content
    applog._logger.handlers.clear()
