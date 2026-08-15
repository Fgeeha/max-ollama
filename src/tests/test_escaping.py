"""User-controlled names must not break the HTML of a message."""
from html import escape


def test_escape_neutralizes_markup_in_a_display_name():
    hostile = 'Вася <b>жирный</b> & <script>'

    safe = escape(hostile)

    assert "<b>" not in safe
    assert "<script>" not in safe
    assert "&amp;" in safe
    # The text itself is preserved, only the markup characters are encoded
    assert "Вася" in safe and "жирный" in safe


def test_handlers_escape_names_before_sending_html():
    """Guard against the escaping being dropped during a future edit."""
    import inspect

    from bot.handlers import admin, common

    start_src = inspect.getsource(common.start)
    assert "escape(user.first_name)" in start_src

    list_users_src = inspect.getsource(admin.list_users)
    assert "escape(user.full_name)" in list_users_src
    assert "escape(user.username)" in list_users_src
