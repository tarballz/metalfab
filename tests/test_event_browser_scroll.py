"""The event table's bottom-pinned follow behavior under free scrolling."""

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from metalfab.live import History, TimedEvent
from metalfab.reports import Event, Line
from metalfab.tui_views import EventBrowser

def _history(start: int, count: int) -> History:
    """Build a history of short serial lines with sequences start..start+count-1."""
    events = tuple(
        TimedEvent(sequence, float(sequence), Event(Line(f"line {sequence}")))
        for sequence in range(start, start + count)
    )
    return History(events=events)


class _BrowserApp(App[None]):
    CSS = """
    EventBrowser { height: 1fr; }
    .event-filter { height: 1; width: 24; }
    EventBrowser DataTable { height: 1fr; }
    .detail-resizer { height: 1; }
    .event-detail-scroll { height: 5; }
    .event-detail { height: auto; }
    """

    def __init__(self, follow: bool) -> None:
        super().__init__()
        self._follow = follow

    def compose(self) -> ComposeResult:
        yield EventBrowser(id="events", follow=self._follow)

    def table(self) -> DataTable:
        return self.query_one(EventBrowser).query_one(DataTable)


def _drive(app: _BrowserApp, actions) -> None:
    async def run() -> None:
        async with app.run_test(size=(100, 24)) as pilot:
            await actions(pilot, app)

    asyncio.run(run())


def test_live_browser_starts_pinned_and_follows() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 60))
        await pilot.pause()
        assert table.cursor_row == 59
        assert table.is_vertical_scroll_end
        browser.set_history(_history(1, 70))
        await pilot.pause()
        assert table.cursor_row == 69
        assert table.is_vertical_scroll_end

    _drive(app, actions)


def test_scroll_up_unpins_and_new_rows_do_not_snap() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 60))
        await pilot.pause()
        # A wheel scroll moves the viewport but leaves the cursor on the last row.
        mid = table.max_scroll_y // 2
        table.scroll_to(y=mid, animate=False)
        await pilot.pause()
        assert table.cursor_row == 59
        browser.set_history(_history(1, 70))
        await pilot.pause()
        assert table.scroll_y == mid
        assert not table.is_vertical_scroll_end

    _drive(app, actions)


def test_scrolling_back_to_bottom_repins() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 60))
        await pilot.pause()
        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        browser.set_history(_history(1, 70))
        await pilot.pause()
        assert not table.is_vertical_scroll_end
        table.scroll_to(y=table.max_scroll_y, animate=False)
        await pilot.pause()
        browser.set_history(_history(1, 80))
        await pilot.pause()
        assert table.cursor_row == 79
        assert table.is_vertical_scroll_end

    _drive(app, actions)


def test_rolling_window_removal_keeps_the_view_anchored() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 300))
        await pilot.pause()
        table.scroll_to(y=40, animate=False)
        await pilot.pause()
        # Sequences 1..5 drop out of the rolling window; the view must not jump.
        browser.set_history(_history(6, 305))
        await pilot.pause()
        assert table.row_count == 305
        assert table.scroll_y == 45

    _drive(app, actions)


def test_reset_starts_live_pinned_and_replay_at_the_top() -> None:
    live = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 60))
        await pilot.pause()
        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        browser.set_history(_history(1, 70), reset=True)
        await pilot.pause()
        assert table.cursor_row == 69
        assert table.is_vertical_scroll_end

    _drive(live, actions)

    replay = _BrowserApp(follow=False)

    async def replay_actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        table = app.table()
        browser.set_history(_history(1, 60), reset=True)
        await pilot.pause()
        assert table.cursor_row == 0

    _drive(replay, replay_actions)


def _detail_height(app: _BrowserApp) -> int:
    from textual.containers import VerticalScroll

    detail = app.query_one(EventBrowser).query_one(".event-detail-scroll", VerticalScroll)
    return detail.size.height


def test_detail_pane_drag_resizes() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        assert _detail_height(app) == 5
        resizer = browser.query_one(".detail-resizer")
        # Dragging the boundary up grows the detail pane by the same amount.
        await pilot.mouse_down(resizer, offset=(0, 0))
        await pilot.mouse_up(resizer, offset=(0, -4))
        await pilot.pause()
        assert _detail_height(app) == 9

    _drive(app, actions)


def test_detail_pane_drag_clamps_to_minimum() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        browser = app.query_one(EventBrowser)
        resizer = browser.query_one(".detail-resizer")
        # Dragging far down must not collapse the pane below its minimum.
        await pilot.mouse_down(resizer, offset=(0, 0))
        await pilot.mouse_up(resizer, offset=(0, 5))
        await pilot.pause()
        assert _detail_height(app) == 2

    _drive(app, actions)


def test_detail_pane_keybindings_resize() -> None:
    app = _BrowserApp(follow=True)

    async def actions(pilot, app):
        await pilot.pause()
        table = app.table()
        await pilot.click(table, offset=(2, 1))
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert _detail_height(app) == 7
        await pilot.press("ctrl+down", "ctrl+down")
        await pilot.pause()
        assert _detail_height(app) == 3

    _drive(app, actions)
